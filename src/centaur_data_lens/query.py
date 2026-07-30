from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from centaur_data_lens.archive import ArchiveEntry
from centaur_data_lens.models import (
    ArchiveManifest,
    CalculatedFact,
    CoverageNote,
    ManifestEntry,
    ManifestGroup,
    NormalizedRecord,
    QueryAssumption,
    QueryFacet,
    QueryIntent,
    QueryOperation,
    QueryPlan,
    QueryResult,
    QueryScope,
    QueryStatus,
)
from centaur_data_lens.platforms.base import PlatformParser

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_DATE_RE = re.compile(
    r"\b(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_RECORD_ID_RE = re.compile(r"\b[0-9a-f]{24}\b", re.IGNORECASE)
_MAX_TEXT_TERMS = 20
_MAX_RECORD_IDS = 100
_GENERIC_PRODUCTS = frozenset({"takeout", "your_facebook_activity"})
_TRANSMITTABLE_EXTENSIONS = frozenset(
    {".csv", ".html", ".jpeg", ".jpg", ".json", ".mp4", ".png", ".txt", ".xml", ".zip"}
)
_TEXT_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "activities",
        "activity",
        "all",
        "an",
        "analyze",
        "and",
        "appear",
        "appears",
        "are",
        "can",
        "common",
        "data",
        "describe",
        "did",
        "do",
        "does",
        "export",
        "find",
        "for",
        "frequent",
        "frequently",
        "from",
        "give",
        "happened",
        "have",
        "i",
        "in",
        "information",
        "is",
        "know",
        "me",
        "mean",
        "might",
        "most",
        "my",
        "of",
        "often",
        "on",
        "overview",
        "please",
        "present",
        "record",
        "records",
        "related",
        "show",
        "summarise",
        "summarize",
        "summary",
        "tell",
        "the",
        "there",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)
_TEXT_ALIASES = {
    "searched": "search",
    "searches": "search",
    "searching": "search",
}
_FACET_SQL = {
    QueryFacet.SERVICE: "service",
    QueryFacet.DEVICE: "device",
    QueryFacet.HOSTNAME: "hostname",
    QueryFacet.ACTIVITY_TYPE: "activity_type",
}
_CATEGORY_PHRASES = {
    "account activity": "account_activity",
    "advertising": "advertising",
    "app installs": "app_installs",
    "browser history": "browser_history",
    "connected apps": "connected_apps",
    "connections": "connections",
    "devices and sessions": "devices_and_sessions",
    "off platform activity": "off_platform_activity",
    "search history": "search_history",
    "youtube history": "youtube_history",
}


def _slug(value: str) -> str:
    rendered = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return rendered[:80] or "other"


def _safe_extension(value: str) -> str:
    normalized = value.lower()
    if normalized in _TRANSMITTABLE_EXTENSIONS:
        return normalized
    if not normalized or normalized == "[no extension]":
        return "[no extension]"
    return "[other]"


def _product_for(entry: ArchiveEntry, platform: str, parser: PlatformParser) -> str:
    if parser.supported_path(entry.path):
        return parser.category_for(entry.path)
    parts = [
        part
        for part in PurePosixPath(entry.path).parts[:-1]
        if part.lower() not in _GENERIC_PRODUCTS
    ]
    if parts:
        return _slug(parts[0])
    stem = PurePosixPath(entry.path).stem
    return _slug(stem)


def manifest_entries(
    *,
    platform: str,
    entries: Iterable[ArchiveEntry],
    parser: PlatformParser,
) -> tuple[ManifestEntry, ...]:
    """Inventory metadata only; this function never opens an archive member."""

    inventoried = []
    for entry in entries:
        supported = (
            not entry.encrypted
            and not entry.symlink
            and not entry.nested_archive
            and parser.supported_path(entry.path)
        )
        suffix = PurePosixPath(entry.path).suffix.lower()
        extension = _safe_extension(suffix)
        inventoried.append(
            ManifestEntry(
                source_id=entry.source_id,
                platform=platform,
                internal_path=entry.path,
                product=_product_for(entry, platform, parser),
                extension=extension,
                compressed_size=entry.compressed_size,
                uncompressed_size=entry.size,
                nested_archive=entry.nested_archive,
                parser_supported=supported,
            )
        )
    return tuple(
        sorted(
            inventoried,
            key=lambda item: (item.platform, item.source_id, item.internal_path),
        )
    )


def _groups(
    entries: Sequence[ManifestEntry], field: Literal["product", "extension"]
) -> tuple[ManifestGroup, ...]:
    grouped: dict[str, list[ManifestEntry]] = {}
    for entry in entries:
        name = getattr(entry, field)
        if field == "extension":
            name = _safe_extension(name)
        grouped.setdefault(name, []).append(entry)
    return tuple(
        ManifestGroup(
            name=name,
            entry_count=len(items),
            compressed_size=sum(item.compressed_size for item in items),
            uncompressed_size=sum(item.uncompressed_size for item in items),
            parser_supported_entries=sum(item.parser_supported for item in items),
        )
        for name, items in sorted(grouped.items())
    )


def build_manifest(entries: Sequence[ManifestEntry]) -> ArchiveManifest:
    ordered = tuple(
        sorted(entries, key=lambda item: (item.platform, item.source_id, item.internal_path))
    )
    supported = sum(entry.parser_supported for entry in ordered)
    return ArchiveManifest(
        entries=ordered,
        products=_groups(ordered, "product"),
        formats=_groups(ordered, "extension"),
        entry_count=len(ordered),
        compressed_size=sum(entry.compressed_size for entry in ordered),
        uncompressed_size=sum(entry.uncompressed_size for entry in ordered),
        nested_archive_count=sum(entry.nested_archive for entry in ordered),
        parser_supported_entries=supported,
        parser_unsupported_entries=len(ordered) - supported,
    )


def _timezone_name(value: str | tzinfo | None) -> tuple[str, tzinfo]:
    if isinstance(value, str):
        try:
            return value, ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
    if value is not None:
        name = getattr(value, "key", None) or str(value)
        return str(name), value

    configured = os.environ.get("TZ")
    if configured:
        try:
            return configured, ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
    localtime = Path("/etc/localtime")
    try:
        resolved = localtime.resolve()
        marker = "/zoneinfo/"
        if marker in str(resolved):
            name = str(resolved).split(marker, 1)[1]
            return name, ZoneInfo(name)
    except OSError:
        pass
    local = datetime.now().astimezone().tzinfo or UTC
    return str(local), local


def _date_from_question(question: str) -> date | None:
    match = _DATE_RE.search(question)
    if match:
        raw = f"{match['month']} {match['day']} {match['year']}"
        for format_string in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(raw, format_string).date()
            except ValueError:
                continue
        return None
    match = _ISO_DATE_RE.search(question)
    if not match:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None


def _text_terms(question: str) -> tuple[str, ...]:
    return tuple(
        _TEXT_ALIASES.get(token, token)[:100]
        for token in _WORD_RE.findall(question.lower())
        if token not in _TEXT_STOP_WORDS
    )[:_MAX_TEXT_TERMS]


def _plan(
    *,
    question: str,
    intent: QueryIntent,
    operation: QueryOperation,
    scope: QueryScope | None = None,
    assumptions: tuple[QueryAssumption, ...] = (),
    clarification: str | None = None,
) -> QueryPlan:
    actual_scope = scope or QueryScope()
    identity = json.dumps(
        {
            "question": question,
            "intent": intent,
            "operation": operation,
            "scope": actual_scope.model_dump(mode="json"),
            "assumptions": [item.model_dump(mode="json") for item in assumptions],
            "clarification": clarification,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return QueryPlan(
        plan_id=f"plan-{sha256(identity.encode()).hexdigest()[:24]}",
        question=question,
        intent=intent,
        operation=operation,
        scope=actual_scope,
        assumptions=assumptions,
        clarification=clarification,
    )


def compile_query(question: str, *, timezone: str | tzinfo | None = None) -> QueryPlan:
    """Compile allowlisted common question forms without a model or network call."""

    normalized = " ".join(question.strip().split())
    lowered = normalized.lower()
    if not normalized:
        return _plan(
            question=normalized,
            intent=QueryIntent.CLARIFICATION,
            operation=QueryOperation.COVERAGE_ONLY,
            clarification="Ask a question about the archive, a date, a facet, a trend, or records.",
        )

    explicit_ids = tuple(dict.fromkeys(_RECORD_ID_RE.findall(lowered)))[:_MAX_RECORD_IDS]
    if explicit_ids and any(word in lowered for word in ("detail", "record", "result")):
        return _plan(
            question=normalized,
            intent=QueryIntent.RECORD_DETAIL,
            operation=QueryOperation.RECORD_BY_ID,
            scope=QueryScope(record_ids=explicit_ids),
        )

    requested_date = _date_from_question(normalized)
    if requested_date is not None:
        timezone_name, timezone_value = _timezone_name(timezone)
        local_start = datetime.combine(requested_date, time.min, tzinfo=timezone_value)
        local_end = local_start + timedelta(days=1)
        assumption = QueryAssumption(
            code="local_timezone",
            message=f"Interpreted the requested calendar date in {timezone_name}.",
        )
        return _plan(
            question=normalized,
            intent=QueryIntent.DATE_LOOKUP,
            operation=QueryOperation.DATE_RANGE,
            scope=QueryScope(
                start_utc=local_start.astimezone(UTC),
                end_utc=local_end.astimezone(UTC),
                timezone=timezone_name,
            ),
            assumptions=(assumption,),
        )
    if _DATE_RE.search(normalized) or _ISO_DATE_RE.search(normalized):
        return _plan(
            question=normalized,
            intent=QueryIntent.CLARIFICATION,
            operation=QueryOperation.COVERAGE_ONLY,
            clarification="Provide a valid calendar date, including a four-digit year.",
        )

    if any(phrase in lowered for phrase in ("over time", "trend", "change over")):
        return _plan(
            question=normalized,
            intent=QueryIntent.TREND,
            operation=QueryOperation.TIME_BUCKETS,
        )

    if "compare" in lowered:
        platforms = tuple(
            platform for platform in ("google", "meta") if platform in _WORD_RE.findall(lowered)
        )
        if len(platforms) >= 2:
            return _plan(
                question=normalized,
                intent=QueryIntent.COMPARISON,
                operation=QueryOperation.PLATFORM_COMPARISON,
                scope=QueryScope(platforms=platforms),
            )
        categories = tuple(
            category for phrase, category in _CATEGORY_PHRASES.items() if phrase in lowered
        )
        if len(categories) >= 2:
            return _plan(
                question=normalized,
                intent=QueryIntent.COMPARISON,
                operation=QueryOperation.CATEGORY_COMPARISON,
                scope=QueryScope(categories=categories),
            )
        return _plan(
            question=normalized,
            intent=QueryIntent.CLARIFICATION,
            operation=QueryOperation.COVERAGE_ONLY,
            clarification="Name at least two supported platforms or categories to compare.",
        )

    facet_phrases = (
        (QueryFacet.DEVICE, ("device", "devices")),
        (QueryFacet.HOSTNAME, ("hostname", "hostnames", "sites", "domains")),
        (QueryFacet.SERVICE, ("service", "services")),
        (
            QueryFacet.ACTIVITY_TYPE,
            (
                "activity type",
                "activity types",
                "activities",
                "what activity",
                "which activity",
            ),
        ),
    )
    for facet, phrases in facet_phrases:
        if any(phrase in lowered for phrase in phrases):
            return _plan(
                question=normalized,
                intent=QueryIntent.FACET,
                operation=QueryOperation.FACET_COUNTS,
                scope=QueryScope(facet=facet),
            )

    if any(phrase in lowered for phrase in ("summarize", "summarise", "overview", "summary")):
        return _plan(
            question=normalized,
            intent=QueryIntent.ARCHIVE_OVERVIEW,
            operation=QueryOperation.ARCHIVE_OVERVIEW,
        )

    if any(phrase in lowered for phrase in ("what did i search", "search history", "searched for")):
        return _plan(
            question=normalized,
            intent=QueryIntent.FACET,
            operation=QueryOperation.FACET_COUNTS,
            scope=QueryScope(
                categories=("search_history",),
                facet=QueryFacet.ACTIVITY_TYPE,
            ),
        )

    if lowered in {"show me data", "tell me about my data", "help", "what is here"}:
        return _plan(
            question=normalized,
            intent=QueryIntent.CLARIFICATION,
            operation=QueryOperation.COVERAGE_ONLY,
            clarification=(
                "Be more specific: request an overview, date, facet, trend, or text lookup."
            ),
        )
    if any(
        word in _WORD_RE.findall(lowered)
        for word in ("cause", "causal", "causality", "predict", "prediction", "recommend")
    ):
        return _plan(
            question=normalized,
            intent=QueryIntent.UNSUPPORTED,
            operation=QueryOperation.COVERAGE_ONLY,
            clarification=(
                "Causal analysis, predictions, and recommendations are not supported by the "
                "deterministic local query engine."
            ),
        )
    terms = _text_terms(normalized)
    if terms:
        return _plan(
            question=normalized,
            intent=QueryIntent.FULL_TEXT,
            operation=QueryOperation.FULL_TEXT_MATCH,
            scope=QueryScope(text_terms=terms),
        )
    if lowered in {"what happened?", "what happened", "what might this mean?"}:
        return _plan(
            question=normalized,
            intent=QueryIntent.ARCHIVE_OVERVIEW,
            operation=QueryOperation.ARCHIVE_OVERVIEW,
        )
    return _plan(
        question=normalized,
        intent=QueryIntent.UNSUPPORTED,
        operation=QueryOperation.COVERAGE_ONLY,
        clarification=(
            "This local compiler does not support that question form. Try an overview, date, "
            "facet, trend, platform comparison, text lookup, or explicit record ID."
        ),
    )


def _fact(
    *,
    plan: QueryPlan,
    scope: Literal["archive", "matching"],
    metric: str,
    value: str | int | float,
    dimensions: dict[str, str] | None = None,
    scope_definition: str | None = None,
    provenance: str = "Parameterized local SQLite query over every matching normalized record.",
    transmittable: bool = True,
) -> CalculatedFact:
    actual_dimensions = dimensions or {}
    definition = scope_definition or f"query_plan:{plan.plan_id}"
    identity = json.dumps(
        {
            "scope": scope,
            "scope_definition": definition,
            "metric": metric,
            "dimensions": actual_dimensions,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return CalculatedFact(
        fact_id=f"fact-{sha256(identity.encode()).hexdigest()[:24]}",
        scope=scope,
        scope_definition=definition,
        metric=metric,
        value=value,
        dimensions=actual_dimensions,
        provenance=provenance,
        transmittable=transmittable,
    )


def _manifest_facts(plan: QueryPlan, manifest: ArchiveManifest) -> list[CalculatedFact]:
    provenance = "Local metadata inventory; archive members were not extracted or opened."
    facts = [
        _fact(
            plan=plan,
            scope="archive",
            scope_definition="all_safe_archive_entries",
            metric="archive_entry_count",
            value=manifest.entry_count,
            provenance=provenance,
        ),
        _fact(
            plan=plan,
            scope="archive",
            scope_definition="all_safe_archive_entries",
            metric="archive_uncompressed_bytes",
            value=manifest.uncompressed_size,
            provenance=provenance,
        ),
        _fact(
            plan=plan,
            scope="archive",
            scope_definition="all_safe_archive_entries",
            metric="archive_compressed_bytes",
            value=manifest.compressed_size,
            provenance=provenance,
        ),
        _fact(
            plan=plan,
            scope="archive",
            scope_definition="all_safe_archive_entries",
            metric="parser_supported_entry_count",
            value=manifest.parser_supported_entries,
            provenance=provenance,
        ),
        _fact(
            plan=plan,
            scope="archive",
            scope_definition="all_safe_archive_entries",
            metric="parser_unsupported_entry_count",
            value=manifest.parser_unsupported_entries,
            provenance=provenance,
        ),
    ]
    for group_type, groups in (("product", manifest.products), ("format", manifest.formats)):
        for group in groups:
            transmittable = group_type != "product"
            facts.append(
                _fact(
                    plan=plan,
                    scope="archive",
                    scope_definition="all_safe_archive_entries",
                    metric="archive_entry_count",
                    value=group.entry_count,
                    dimensions={group_type: group.name},
                    provenance=provenance,
                    transmittable=transmittable,
                )
            )
            facts.append(
                _fact(
                    plan=plan,
                    scope="archive",
                    scope_definition="all_safe_archive_entries",
                    metric="archive_uncompressed_bytes",
                    value=group.uncompressed_size,
                    dimensions={group_type: group.name},
                    provenance=provenance,
                    transmittable=transmittable,
                )
            )
    return facts


def _record_rows(
    connection: sqlite3.Connection,
    where: str = "",
    parameters: Sequence[object] = (),
    *,
    limit: int = 1_000,
) -> list[NormalizedRecord]:
    rows = connection.execute(
        f"""
        SELECT payload
        FROM records
        {where}
        ORDER BY platform, category, timestamp DESC, record_id
        LIMIT ?
        """,  # noqa: S608 - where clauses are internal constants selected by operation.
        (*parameters, limit),
    ).fetchall()
    return [NormalizedRecord.model_validate_json(row[0]) for row in rows]


def _diversify(
    candidates: Sequence[NormalizedRecord], *, limit: int
) -> tuple[NormalizedRecord, ...]:
    if len(candidates) <= limit:
        return tuple(candidates)
    counts: Counter[tuple[str, str]] = Counter()
    for record in candidates:
        for field, value in (
            ("service", record.service),
            ("hostname", record.hostname),
            ("device", record.device),
            ("activity_type", record.activity_type),
        ):
            if value:
                counts[(field, value)] += 1
    selected: list[NormalizedRecord] = []
    selected_ids: set[str] = set()
    seen: set[tuple[object, ...]] = set()
    while len(selected) < limit:
        best: tuple[int, int, NormalizedRecord, tuple[tuple[object, ...], ...]] | None = None
        for index, record in enumerate(candidates):
            if record.record_id in selected_ids:
                continue
            year = str(record.timestamp.year) if record.timestamp else "unknown"
            groups: list[tuple[object, ...]] = [
                ("stratum", record.platform, record.category, year),
                ("category", record.platform, record.category),
            ]
            for field, value in (
                ("service", record.service),
                ("hostname", record.hostname),
                ("device", record.device),
                ("activity_type", record.activity_type),
            ):
                if value and counts[(field, value)] > 1:
                    groups.append((field, value))
            candidate = (sum(group not in seen for group in groups), -index, record, tuple(groups))
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None:
            break
        _, _, record, selected_groups = best
        selected.append(record)
        selected_ids.add(record.record_id)
        seen.update(selected_groups)
    return tuple(selected)


def _platform_coverage(
    connection: sqlite3.Connection,
    manifest: ArchiveManifest,
    platforms: Sequence[str],
) -> tuple[QueryStatus | None, tuple[CoverageNote, ...]]:
    notes = []
    status: QueryStatus | None = None
    manifest_platforms = {entry.platform for entry in manifest.entries}
    record_platforms = {
        str(row[0])
        for row in connection.execute("SELECT DISTINCT platform FROM records").fetchall()
    }
    for platform in platforms:
        if platform not in manifest_platforms:
            status = QueryStatus.NOT_PRESENT
            notes.append(
                CoverageNote(
                    code="platform_not_present",
                    message=f"No {platform} product was present in the selected sources.",
                    platform=platform,
                )
            )
        elif platform not in record_platforms:
            if status != QueryStatus.NOT_PRESENT:
                status = QueryStatus.PRODUCT_UNSUPPORTED
            notes.append(
                CoverageNote(
                    code="product_present_but_unsupported",
                    message=(
                        f"{platform} entries were present, but no supported normalized records "
                        "were available."
                    ),
                    platform=platform,
                )
            )
    return status, tuple(notes)


def _category_coverage(
    manifest: ArchiveManifest,
    category: str,
) -> tuple[QueryStatus, CoverageNote]:
    entries = tuple(entry for entry in manifest.entries if entry.product == category)
    if not entries:
        message = (
            "No supported search-history records were found in this export."
            if category == "search_history"
            else f"The {category} category is not present in the selected sources."
        )
        return (
            QueryStatus.NOT_PRESENT,
            CoverageNote(
                code="category_not_present",
                message=message,
                category=category,
            ),
        )
    if not any(entry.parser_supported for entry in entries):
        return (
            QueryStatus.PRODUCT_UNSUPPORTED,
            CoverageNote(
                code="product_present_but_unsupported",
                message=(
                    f"The {category} product is present, but Centaur has no supported parser "
                    "data for it."
                ),
                category=category,
                product=category,
            ),
        )
    message = (
        "No supported search-history records were found in this export."
        if category == "search_history"
        else f"The {category} category is present but contains no matching data."
    )
    return (
        QueryStatus.MATCHING_DATA_ABSENT,
        CoverageNote(
            code="category_data_absent",
            message=message,
            category=category,
        ),
    )


def _stronger_coverage_status(current: QueryStatus, candidate: QueryStatus) -> QueryStatus:
    priority = {
        QueryStatus.OK: 0,
        QueryStatus.MATCHING_DATA_ABSENT: 1,
        QueryStatus.PRODUCT_UNSUPPORTED: 2,
        QueryStatus.NOT_PRESENT: 3,
    }
    return candidate if priority[candidate] > priority[current] else current


def _fts_expression(terms: Sequence[str]) -> str:
    return " AND ".join(f'"{term.replace(chr(34), "")}"' for term in terms)


def _result(
    *,
    plan: QueryPlan,
    status: QueryStatus,
    total: int,
    matching: int,
    facts: Sequence[CalculatedFact],
    records: Sequence[NormalizedRecord] = (),
    notes: Sequence[CoverageNote] = (),
    message: str | None = None,
    evidence_limit: int,
) -> QueryResult:
    return QueryResult(
        plan=plan,
        status=status,
        total_records=total,
        matching_records=matching,
        facts=tuple(facts),
        evidence=_diversify(records, limit=evidence_limit),
        coverage_notes=tuple(notes),
        assumptions=plan.assumptions,
        message=message,
    )


def execute_query(
    connection: sqlite3.Connection,
    manifest: ArchiveManifest,
    plan: QueryPlan,
    *,
    candidate_limit: int = 1_000,
    evidence_limit: int = 100,
) -> QueryResult:
    """Execute one allowlisted plan with parameters; no plan can supply SQL."""

    total = int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    if plan.operation == QueryOperation.COVERAGE_ONLY:
        status = (
            QueryStatus.CLARIFICATION_REQUIRED
            if plan.intent == QueryIntent.CLARIFICATION
            else QueryStatus.UNSUPPORTED
        )
        fact = _fact(
            plan=plan,
            scope="matching",
            metric="coverage_status",
            value=status.value,
            provenance="Deterministic local question-compiler coverage result.",
        )
        return _result(
            plan=plan,
            status=status,
            total=total,
            matching=0,
            facts=(fact,),
            message=plan.clarification,
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.ARCHIVE_OVERVIEW:
        facts = [
            _fact(
                plan=plan,
                scope="archive",
                scope_definition="all_supported_records",
                metric="record_count",
                value=total,
            ),
            *_manifest_facts(plan, manifest),
        ]
        rows = connection.execute(
            """
            SELECT platform, category, COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM records
            GROUP BY platform, category
            ORDER BY platform, category
            """
        ).fetchall()
        for platform, category, count, earliest, latest in rows:
            dimensions = {"platform": str(platform), "category": str(category)}
            facts.append(
                _fact(
                    plan=plan,
                    scope="archive",
                    scope_definition="all_supported_records",
                    metric="record_count",
                    value=int(count),
                    dimensions=dimensions,
                )
            )
            for metric, value in (("earliest_timestamp", earliest), ("latest_timestamp", latest)):
                if value:
                    facts.append(
                        _fact(
                            plan=plan,
                            scope="archive",
                            scope_definition="all_supported_records",
                            metric=metric,
                            value=str(value),
                            dimensions=dimensions,
                        )
                    )
        candidates = _record_rows(connection, limit=candidate_limit)
        overview_notes: tuple[CoverageNote, ...] = ()
        status = QueryStatus.OK
        message = None
        if total == 0 and manifest.parser_supported_entries:
            status = QueryStatus.MATCHING_DATA_ABSENT
            message = "Supported archive products are present but contain no normalized records."
        elif total == 0 and manifest.parser_unsupported_entries:
            status = QueryStatus.PRODUCT_UNSUPPORTED
            message = "Archive products are present, but none have supported normalized records."
            overview_notes = (
                CoverageNote(
                    code="product_present_but_unsupported",
                    message=message,
                ),
            )
        elif total == 0 and manifest.entry_count == 0:
            status = QueryStatus.NOT_PRESENT
            message = "No archive entries or supported records are present."
        return _result(
            plan=plan,
            status=status,
            total=total,
            matching=total,
            facts=facts,
            records=candidates,
            notes=overview_notes,
            message=message,
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.DATE_RANGE:
        assert plan.scope.start_utc is not None and plan.scope.end_utc is not None
        date_parameters = (
            plan.scope.start_utc.isoformat(),
            plan.scope.end_utc.isoformat(),
        )
        where = "WHERE datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)"
        matching = int(
            connection.execute(
                f"SELECT COUNT(*) FROM records {where}",  # noqa: S608
                date_parameters,
            ).fetchone()[0]
        )
        facts = [
            _fact(
                plan=plan,
                scope="archive",
                scope_definition="all_supported_records",
                metric="record_count",
                value=total,
            ),
            _fact(
                plan=plan,
                scope="matching",
                metric="record_count",
                value=matching,
            ),
        ]
        candidates = _record_rows(
            connection,
            where,
            date_parameters,
            limit=candidate_limit,
        )
        return _result(
            plan=plan,
            status=QueryStatus.OK if matching else QueryStatus.NO_MATCHING_RECORDS,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            message=None if matching else "No records matched that local calendar date.",
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.FACET_COUNTS:
        assert plan.scope.facet is not None
        column = _FACET_SQL[plan.scope.facet]
        where_parts = [f"{column} IS NOT NULL"]
        facet_parameters: list[object] = []
        if plan.scope.categories:
            placeholders = ",".join("?" for _ in plan.scope.categories)
            where_parts.append(f"category IN ({placeholders})")
            facet_parameters.extend(plan.scope.categories)
        where = f"WHERE {' AND '.join(where_parts)}"
        rows = connection.execute(
            f"""
            SELECT {column}, COUNT(*)
            FROM records
            {where}
            GROUP BY {column}
            ORDER BY COUNT(*) DESC, {column}
            LIMIT 100
            """,  # noqa: S608 - identifiers and clauses come only from allowlists above.
            facet_parameters,
        ).fetchall()
        matching = int(
            connection.execute(
                f"SELECT COUNT(*) FROM records {where}",  # noqa: S608
                facet_parameters,
            ).fetchone()[0]
        )
        facts = [
            _fact(
                plan=plan,
                scope="archive",
                scope_definition="all_supported_records",
                metric="record_count",
                value=total,
            ),
            _fact(plan=plan, scope="matching", metric="record_count", value=matching),
        ]
        facts.extend(
            _fact(
                plan=plan,
                scope="matching",
                metric="record_count",
                value=int(count),
                dimensions={plan.scope.facet.value: str(value)},
            )
            for value, count in rows
        )
        if plan.scope.categories:
            available_categories = {
                str(row[0])
                for row in connection.execute("SELECT DISTINCT category FROM records").fetchall()
            }
            missing = [
                category
                for category in plan.scope.categories
                if category not in available_categories
            ]
            if missing:
                category = missing[0]
                status, note = _category_coverage(manifest, category)
                return _result(
                    plan=plan,
                    status=status,
                    total=total,
                    matching=0,
                    facts=facts,
                    notes=(note,),
                    message=note.message,
                    evidence_limit=evidence_limit,
                )
        candidates = _record_rows(
            connection,
            where,
            facet_parameters,
            limit=candidate_limit,
        )
        status = QueryStatus.OK if matching else QueryStatus.MATCHING_DATA_ABSENT
        missing_messages = {
            QueryFacet.DEVICE: (
                "No device values were found in the supported records from this export."
            ),
            QueryFacet.HOSTNAME: (
                "No hostname values were found in the supported records from this export."
            ),
            QueryFacet.SERVICE: (
                "No service values were found in the supported records from this export."
            ),
            QueryFacet.ACTIVITY_TYPE: (
                "No activity values were found in the supported records from this export."
            ),
        }
        message = None if matching else missing_messages[plan.scope.facet]
        return _result(
            plan=plan,
            status=status,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            message=message,
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.TIME_BUCKETS:
        rows = connection.execute(
            """
            SELECT substr(timestamp, 1, 7), COUNT(*)
            FROM records
            WHERE timestamp IS NOT NULL
            GROUP BY substr(timestamp, 1, 7)
            ORDER BY substr(timestamp, 1, 7)
            """
        ).fetchall()
        matching = sum(int(row[1]) for row in rows)
        facts = [
            _fact(
                plan=plan,
                scope="archive",
                scope_definition="all_supported_records",
                metric="record_count",
                value=total,
            ),
            _fact(plan=plan, scope="matching", metric="timestamped_record_count", value=matching),
        ]
        facts.extend(
            _fact(
                plan=plan,
                scope="matching",
                metric="record_count",
                value=int(count),
                dimensions={"month_utc": str(month)},
            )
            for month, count in rows
        )
        candidates = _record_rows(
            connection,
            "WHERE timestamp IS NOT NULL",
            limit=candidate_limit,
        )
        return _result(
            plan=plan,
            status=QueryStatus.OK if matching else QueryStatus.MATCHING_DATA_ABSENT,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            message=None if matching else "No timestamps are available for a trend.",
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.PLATFORM_COMPARISON:
        coverage_status, coverage_notes = _platform_coverage(
            connection,
            manifest,
            plan.scope.platforms,
        )
        placeholders = ",".join("?" for _ in plan.scope.platforms)
        rows = connection.execute(
            f"""
            SELECT platform, category, COUNT(*)
            FROM records
            WHERE platform IN ({placeholders})
            GROUP BY platform, category
            ORDER BY platform, category
            """,  # noqa: S608 - placeholder count only; all values are parameters.
            plan.scope.platforms,
        ).fetchall()
        matching = sum(int(row[2]) for row in rows)
        matching_fact = _fact(
            plan=plan,
            scope="matching",
            metric="record_count",
            value=matching,
        )
        facts = (
            [
                _fact(
                    plan=plan,
                    scope="archive",
                    scope_definition="all_supported_records",
                    metric="record_count",
                    value=total,
                ),
                matching_fact,
            ]
            if matching
            else [matching_fact]
        )
        facts.extend(
            _fact(
                plan=plan,
                scope="matching",
                metric="record_count",
                value=int(count),
                dimensions={"platform": str(platform), "category": str(category)},
            )
            for platform, category, count in rows
        )
        candidates = _record_rows(
            connection,
            f"WHERE platform IN ({placeholders})",
            plan.scope.platforms,
            limit=candidate_limit,
        )
        status = coverage_status or (
            QueryStatus.OK if matching else QueryStatus.NO_MATCHING_RECORDS
        )
        return _result(
            plan=plan,
            status=status,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            notes=coverage_notes,
            message=coverage_notes[0].message if coverage_notes else None,
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.CATEGORY_COMPARISON:
        placeholders = ",".join("?" for _ in plan.scope.categories)
        rows = connection.execute(
            f"""
            SELECT platform, category, COUNT(*)
            FROM records
            WHERE category IN ({placeholders})
            GROUP BY platform, category
            ORDER BY category, platform
            """,  # noqa: S608 - placeholder count only; all values are parameters.
            plan.scope.categories,
        ).fetchall()
        matching = sum(int(row[2]) for row in rows)
        available = {
            str(row[0])
            for row in connection.execute("SELECT DISTINCT category FROM records").fetchall()
        }
        notes = []
        status = QueryStatus.OK
        for category in plan.scope.categories:
            if category in available:
                continue
            category_status, note = _category_coverage(manifest, category)
            status = _stronger_coverage_status(status, category_status)
            notes.append(note)
        facts = [
            _fact(
                plan=plan,
                scope="archive",
                scope_definition="all_supported_records",
                metric="record_count",
                value=total,
            ),
            _fact(plan=plan, scope="matching", metric="record_count", value=matching),
        ]
        facts.extend(
            _fact(
                plan=plan,
                scope="matching",
                metric="record_count",
                value=int(count),
                dimensions={"platform": str(platform), "category": str(category)},
            )
            for platform, category, count in rows
        )
        candidates = _record_rows(
            connection,
            f"WHERE category IN ({placeholders})",
            plan.scope.categories,
            limit=candidate_limit,
        )
        return _result(
            plan=plan,
            status=status,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            notes=notes,
            message=notes[0].message if notes else None,
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.FULL_TEXT_MATCH:
        expression = _fts_expression(plan.scope.text_terms)
        matching = int(
            connection.execute(
                "SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?",
                (expression,),
            ).fetchone()[0]
        )
        matching_fact = _fact(
            plan=plan,
            scope="matching",
            metric="record_count",
            value=matching,
            scope_definition=(f"full_text_query_sha256:{sha256(expression.encode()).hexdigest()}"),
        )
        facts = (
            [
                _fact(
                    plan=plan,
                    scope="archive",
                    scope_definition="all_supported_records",
                    metric="record_count",
                    value=total,
                ),
                matching_fact,
            ]
            if matching
            else [matching_fact]
        )
        rows = connection.execute(
            """
            SELECT records.payload
            FROM records_fts
            JOIN records ON records.record_id = records_fts.record_id
            WHERE records_fts MATCH ?
            ORDER BY bm25(records_fts), records.record_id
            LIMIT ?
            """,
            (expression, candidate_limit),
        ).fetchall()
        candidates = [NormalizedRecord.model_validate_json(row[0]) for row in rows]
        return _result(
            plan=plan,
            status=QueryStatus.OK if matching else QueryStatus.NO_MATCHING_RECORDS,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            message=None if matching else "No matching records were found for this question.",
            evidence_limit=evidence_limit,
        )

    if plan.operation == QueryOperation.RECORD_BY_ID:
        placeholders = ",".join("?" for _ in plan.scope.record_ids)
        candidates = _record_rows(
            connection,
            f"WHERE record_id IN ({placeholders})",
            plan.scope.record_ids,
            limit=min(candidate_limit, _MAX_RECORD_IDS),
        )
        matching = len(candidates)
        facts = [
            _fact(
                plan=plan,
                scope="archive",
                scope_definition="all_supported_records",
                metric="record_count",
                value=total,
            ),
            _fact(plan=plan, scope="matching", metric="record_count", value=matching),
        ]
        return _result(
            plan=plan,
            status=QueryStatus.OK if matching else QueryStatus.NO_MATCHING_RECORDS,
            total=total,
            matching=matching,
            facts=facts,
            records=candidates,
            message=None if matching else "No records matched the explicit result IDs.",
            evidence_limit=evidence_limit,
        )

    raise AssertionError(f"Unhandled allowlisted query operation: {plan.operation}")
