from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from centaur_data_lens.archive import ArchiveReader
from centaur_data_lens.errors import DataLensError
from centaur_data_lens.models import (
    ArchiveManifest,
    CalculatedFact,
    CoverageItem,
    EvidenceItem,
    ManifestEntry,
    NormalizedRecord,
    PrivacySnapshot,
    QueryPlan,
    QueryResult,
    QueryStatus,
)
from centaur_data_lens.platforms import get_platform
from centaur_data_lens.query import (
    build_manifest,
    compile_query,
    execute_query,
    manifest_entries,
)
from centaur_data_lens.security import cleanup_stale_sessions, secure_temp_directory

ProgressCallback = Callable[[str], None]
_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_QUESTION_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "activities",
        "activity",
        "all",
        "an",
        "analyse",
        "analyze",
        "and",
        "appear",
        "appears",
        "are",
        "can",
        "common",
        "commonly",
        "data",
        "describe",
        "did",
        "do",
        "does",
        "export",
        "find",
        "found",
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
_QUESTION_TOKEN_ALIASES = {
    "device": "centaurfacetdevice",
    "devices": "centaurfacetdevice",
    "hostname": "centaurfacethostname",
    "hostnames": "centaurfacethostname",
    "searched": "search",
    "searches": "search",
    "searching": "search",
    "service": "centaurfacetservice",
    "services": "centaurfacetservice",
}


@dataclass(frozen=True)
class SourceSpec:
    platform: str
    path: Path


@dataclass(frozen=True)
class QuestionContext:
    total_records: int
    matching_records: int
    selection_mode: str
    facts: tuple[CalculatedFact, ...]
    records: tuple[NormalizedRecord, ...]
    no_match_message: str | None = None


def _question_tokens(question: str) -> list[str]:
    return [
        _QUESTION_TOKEN_ALIASES.get(token, token)
        for token in _FTS_TOKEN_RE.findall(question.lower())
        if token not in _QUESTION_STOP_WORDS
    ][:20]


def _fts_query(question: str, *, require_all: bool = False) -> str | None:
    tokens = _question_tokens(question)
    if not tokens:
        return None
    operator = " AND " if require_all else " OR "
    return operator.join(f'"{token.replace(chr(34), "")}"' for token in tokens)


class AnalysisSession:
    """Ephemeral normalized index shared by wizard and direct commands."""

    def __init__(self) -> None:
        cleanup_stale_sessions()
        self._directory = secure_temp_directory()
        self._database_path = self._directory / "analysis.sqlite3"
        self._connection = sqlite3.connect(self._database_path)
        with suppress(OSError):
            os.chmod(self._database_path, 0o600)
        self._connection.execute("PRAGMA secure_delete = ON")
        self._connection.execute("PRAGMA journal_mode = DELETE")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE records (
                record_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                category TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                service TEXT,
                timestamp TEXT,
                hostname TEXT,
                device TEXT,
                title TEXT,
                payload TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE records_fts USING fts5(
                record_id UNINDEXED,
                title,
                service,
                hostname,
                attributes,
                tokenize = 'unicode61'
            );
            """
        )
        self._manifest_entries: list[ManifestEntry] = []
        self._closed = False

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def manifest(self) -> ArchiveManifest:
        return build_manifest(self._manifest_entries)

    def add_manifest_entries(self, entries: Sequence[ManifestEntry]) -> None:
        self._manifest_entries.extend(entries)

    def compile_query(
        self,
        question: str,
        *,
        timezone: str | tzinfo | None = None,
    ) -> QueryPlan:
        return compile_query(question, timezone=timezone)

    def execute_query(
        self,
        plan: QueryPlan,
        *,
        candidate_limit: int = 1_000,
        evidence_limit: int = 100,
    ) -> QueryResult:
        return execute_query(
            self._connection,
            self.manifest,
            plan,
            candidate_limit=candidate_limit,
            evidence_limit=evidence_limit,
        )

    def query(
        self,
        question: str,
        *,
        timezone: str | tzinfo | None = None,
        candidate_limit: int = 1_000,
        evidence_limit: int = 100,
    ) -> QueryResult:
        return self.execute_query(
            self.compile_query(question, timezone=timezone),
            candidate_limit=candidate_limit,
            evidence_limit=evidence_limit,
        )

    def add_record(self, record: NormalizedRecord) -> bool:
        existing_row = self._connection.execute(
            "SELECT payload FROM records WHERE record_id = ?", (record.record_id,)
        ).fetchone()
        if existing_row:
            existing = NormalizedRecord.model_validate_json(existing_row[0])
            merged_sources = tuple(dict.fromkeys((*existing.sources, *record.sources)))
            merged = existing.model_copy(update={"sources": merged_sources})
            self._connection.execute(
                "UPDATE records SET payload = ? WHERE record_id = ?",
                (merged.model_dump_json(), record.record_id),
            )
            return False

        fts_values = [
            *(str(value) for value in record.attributes.values()),
            record.platform,
            record.category,
            record.activity_type,
        ]
        if record.device:
            fts_values.extend((record.device, "centaurfacetdevice"))
        if record.hostname:
            fts_values.append("centaurfacethostname")
        if record.service:
            fts_values.append("centaurfacetservice")
        attributes = " ".join(fts_values)
        self._connection.execute(
            """
            INSERT INTO records(
                record_id, platform, category, activity_type, service, timestamp,
                hostname, device, title, payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.platform,
                record.category,
                record.activity_type,
                record.service,
                record.timestamp.isoformat() if record.timestamp else None,
                record.hostname,
                record.device,
                record.title,
                record.model_dump_json(),
            ),
        )
        self._connection.execute(
            """
            INSERT INTO records_fts(record_id, title, service, hostname, attributes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.title or "",
                record.service or "",
                record.hostname or "",
                attributes,
            ),
        )
        return True

    def commit(self) -> None:
        self._connection.commit()

    def records(self) -> Iterator[NormalizedRecord]:
        cursor = self._connection.execute(
            "SELECT payload FROM records ORDER BY platform, category, timestamp, record_id"
        )
        for (payload,) in cursor:
            yield NormalizedRecord.model_validate_json(payload)

    def search(self, question: str, *, limit: int = 100) -> list[NormalizedRecord]:
        query = _fts_query(question)
        if query is None:
            return list(self.records())[:limit]
        rows = self._connection.execute(
            """
            SELECT records.payload
            FROM records_fts
            JOIN records ON records.record_id = records_fts.record_id
            WHERE records_fts MATCH ?
            ORDER BY bm25(records_fts)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        if not rows:
            rows = self._connection.execute(
                "SELECT payload FROM records ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [NormalizedRecord.model_validate_json(row[0]) for row in rows]

    def question_context(
        self,
        question: str,
        *,
        candidate_limit: int = 1_000,
        evidence_limit: int = 100,
    ) -> QuestionContext:
        result = self.query(
            question,
            candidate_limit=candidate_limit,
            evidence_limit=evidence_limit,
        )
        no_match = result.status != QueryStatus.OK
        return QuestionContext(
            total_records=result.total_records,
            matching_records=result.matching_records,
            selection_mode=(
                "archive"
                if result.plan.operation.value == "archive_overview"
                else "full_text_all_terms"
                if not no_match
                else "no_match"
            ),
            facts=result.facts,
            records=result.evidence,
            no_match_message=result.message,
        )

    def snapshot(self) -> PrivacySnapshot:
        coverage_rows = self._connection.execute(
            """
            SELECT platform, category, COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM records
            GROUP BY platform, category
            ORDER BY platform, category
            """
        ).fetchall()
        coverage = [
            CoverageItem(
                platform=row[0],
                category=row[1],
                record_count=row[2],
                earliest=datetime.fromisoformat(row[3]) if row[3] else None,
                latest=datetime.fromisoformat(row[4]) if row[4] else None,
            )
            for row in coverage_rows
        ]
        host_rows = self._connection.execute(
            """
            SELECT hostname, COUNT(*)
            FROM records
            WHERE hostname IS NOT NULL
            GROUP BY hostname
            ORDER BY COUNT(*) DESC, hostname
            LIMIT 25
            """
        ).fetchall()
        overlap_rows = self._connection.execute(
            """
            SELECT hostname
            FROM records
            WHERE hostname IS NOT NULL
            GROUP BY hostname
            HAVING COUNT(DISTINCT platform) > 1
            ORDER BY hostname
            LIMIT 100
            """
        ).fetchall()
        device_rows = self._connection.execute(
            """
            SELECT json_extract(payload, '$.device')
            FROM records
            WHERE json_extract(payload, '$.device') IS NOT NULL
            GROUP BY json_extract(payload, '$.device')
            HAVING COUNT(DISTINCT platform) > 1
            ORDER BY json_extract(payload, '$.device')
            LIMIT 100
            """
        ).fetchall()
        service_rows = self._connection.execute(
            """
            SELECT json_extract(payload, '$.service')
            FROM records
            WHERE json_extract(payload, '$.service') IS NOT NULL
            GROUP BY lower(json_extract(payload, '$.service'))
            HAVING COUNT(DISTINCT platform) > 1
            ORDER BY lower(json_extract(payload, '$.service'))
            LIMIT 100
            """
        ).fetchall()
        platforms = [
            row[0]
            for row in self._connection.execute(
                "SELECT DISTINCT platform FROM records ORDER BY platform"
            )
        ]
        total = self._connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]

        evidence: list[EvidenceItem] = []
        per_category: defaultdict[tuple[str, str], int] = defaultdict(int)
        for record in self.records():
            key = (record.platform, record.category)
            if per_category[key] >= 3 or not record.sources:
                continue
            evidence.append(
                EvidenceItem(
                    record_id=record.record_id,
                    platform=record.platform,
                    category=record.category,
                    title=record.title or record.activity_type,
                    timestamp=record.timestamp,
                    source=record.sources[0].label,
                )
            )
            per_category[key] += 1
            if len(evidence) >= 100:
                break

        omissions = {
            platform_id: list(get_platform(platform_id).definition.excluded)
            for platform_id in platforms
        }
        return PrivacySnapshot(
            generated_at=datetime.now(UTC),
            platforms=platforms,
            total_records=total,
            coverage=coverage,
            common_hostnames=[(row[0], row[1]) for row in host_rows],
            overlapping_hostnames=[row[0] for row in overlap_rows],
            overlapping_devices=[row[0] for row in device_rows],
            overlapping_services=[row[0] for row in service_rows],
            evidence=evidence,
            omissions=omissions,
        )

    def diagnostics(self) -> dict[str, object]:
        rows = self._connection.execute(
            "SELECT platform, category, COUNT(*) FROM records GROUP BY platform, category"
        ).fetchall()
        return {
            "schema_version": "1",
            "counts": [
                {"platform": platform, "category": category, "records": count}
                for platform, category, count in rows
            ],
            "values_included": False,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            with suppress(OSError):
                shutil.rmtree(self._directory)

    def __enter__(self) -> AnalysisSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def parse_source_values(values: Sequence[str]) -> list[SourceSpec]:
    specs: list[SourceSpec] = []
    for value in values:
        if "=" not in value:
            raise DataLensError("Sources must use PLATFORM=PATH, for example google=takeout.zip.")
        platform, raw_path = value.split("=", 1)
        get_platform(platform)
        if not raw_path.strip():
            raise DataLensError("A source path cannot be empty.")
        specs.append(SourceSpec(platform=platform.lower(), path=Path(raw_path).expanduser()))
    if not specs:
        raise DataLensError("Select at least one source.")
    return specs


def analyze_sources(
    session: AnalysisSession,
    specs: Sequence[SourceSpec],
    *,
    allow_large_archive: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    grouped: defaultdict[str, list[Path]] = defaultdict(list)
    for spec in specs:
        grouped[spec.platform].append(spec.path)

    counts: dict[str, int] = {}
    for platform_id, paths in grouped.items():
        parser = get_platform(platform_id)
        if progress:
            progress(f"Validating {parser.definition.display_name} export…")
        parsed_count = 0
        indexed_count = 0
        with ArchiveReader(paths, allow_large_archive=allow_large_archive) as reader:
            session.add_manifest_entries(
                manifest_entries(
                    platform=platform_id,
                    entries=reader.entries,
                    parser=parser,
                )
            )
            parser.validate(reader)
            for record in parser.iter_records(reader):
                parsed_count += 1
                if session.add_record(record):
                    indexed_count += 1
        counts[platform_id] = indexed_count
        if progress:
            progress(
                f"Indexed {indexed_count:,} unique {parser.definition.display_name} records "
                f"from {parsed_count:,} parsed entries."
            )
    session.commit()
    return counts
