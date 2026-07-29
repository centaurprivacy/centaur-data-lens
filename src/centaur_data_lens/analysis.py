from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from centaur_data_lens.archive import ArchiveReader
from centaur_data_lens.errors import DataLensError
from centaur_data_lens.models import (
    CalculatedFact,
    CoverageItem,
    EvidenceItem,
    NormalizedRecord,
    PrivacySnapshot,
)
from centaur_data_lens.platforms import get_platform
from centaur_data_lens.security import cleanup_stale_sessions, secure_temp_directory

ProgressCallback = Callable[[str], None]
_FTS_TOKEN_RE = re.compile(r"[\w.-]+", re.UNICODE)
_QUESTION_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "are",
        "can",
        "data",
        "do",
        "does",
        "export",
        "from",
        "give",
        "have",
        "i",
        "in",
        "is",
        "me",
        "my",
        "of",
        "on",
        "overview",
        "please",
        "show",
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
_TOP_VALUE_QUERIES = {
    "service": (
        """
        SELECT service, COUNT(*)
        FROM records
        WHERE service IS NOT NULL
        GROUP BY service
        ORDER BY COUNT(*) DESC, service
        LIMIT 10
        """,
        """
        SELECT records.service, COUNT(*)
        FROM records_fts
        JOIN records ON records.record_id = records_fts.record_id
        WHERE records_fts MATCH ? AND records.service IS NOT NULL
        GROUP BY records.service
        ORDER BY COUNT(*) DESC, records.service
        LIMIT 10
        """,
    ),
    "hostname": (
        """
        SELECT hostname, COUNT(*)
        FROM records
        WHERE hostname IS NOT NULL
        GROUP BY hostname
        ORDER BY COUNT(*) DESC, hostname
        LIMIT 10
        """,
        """
        SELECT records.hostname, COUNT(*)
        FROM records_fts
        JOIN records ON records.record_id = records_fts.record_id
        WHERE records_fts MATCH ? AND records.hostname IS NOT NULL
        GROUP BY records.hostname
        ORDER BY COUNT(*) DESC, records.hostname
        LIMIT 10
        """,
    ),
    "device": (
        """
        SELECT device, COUNT(*)
        FROM records
        WHERE device IS NOT NULL
        GROUP BY device
        ORDER BY COUNT(*) DESC, device
        LIMIT 10
        """,
        """
        SELECT records.device, COUNT(*)
        FROM records_fts
        JOIN records ON records.record_id = records_fts.record_id
        WHERE records_fts MATCH ? AND records.device IS NOT NULL
        GROUP BY records.device
        ORDER BY COUNT(*) DESC, records.device
        LIMIT 10
        """,
    ),
    "activity_type": (
        """
        SELECT activity_type, COUNT(*)
        FROM records
        GROUP BY activity_type
        ORDER BY COUNT(*) DESC, activity_type
        LIMIT 10
        """,
        """
        SELECT records.activity_type, COUNT(*)
        FROM records_fts
        JOIN records ON records.record_id = records_fts.record_id
        WHERE records_fts MATCH ?
        GROUP BY records.activity_type
        ORDER BY COUNT(*) DESC, records.activity_type
        LIMIT 10
        """,
    ),
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


def _fts_query(question: str) -> str | None:
    tokens = [
        token
        for token in _FTS_TOKEN_RE.findall(question.lower())[:20]
        if token not in _QUESTION_STOP_WORDS
    ]
    if not tokens:
        return None
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def _fact(
    *,
    scope: Literal["archive", "matching"],
    scope_definition: str,
    metric: str,
    value: str | int | float,
    dimensions: dict[str, str] | None = None,
) -> CalculatedFact:
    actual_dimensions = dimensions or {}
    identity = json.dumps(
        {
            "scope": scope,
            "scope_definition": scope_definition,
            "metric": metric,
            "dimensions": actual_dimensions,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
    provenance = (
        "Local SQLite aggregate over every supported normalized record."
        if scope_definition == "all_supported_records"
        else "Local SQLite aggregate over every full-text record match."
    )
    return CalculatedFact(
        fact_id=f"fact-{digest}",
        scope=scope,
        scope_definition=scope_definition,
        metric=metric,
        value=value,
        dimensions=actual_dimensions,
        provenance=provenance,
    )


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
        self._closed = False

    @property
    def database_path(self) -> Path:
        return self._database_path

    def add_record(self, record: NormalizedRecord) -> None:
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
            return

        attributes = " ".join(str(value) for value in record.attributes.values())
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

    def _matching_count(self, query: str | None) -> int:
        if query is None:
            return int(self._connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?",
                (query,),
            ).fetchone()[0]
        )

    def _coverage_rows(self, query: str | None) -> list[tuple[object, ...]]:
        if query is None:
            return self._connection.execute(
                """
                SELECT platform, category, COUNT(*), MIN(timestamp), MAX(timestamp)
                FROM records
                GROUP BY platform, category
                ORDER BY platform, category
                """
            ).fetchall()
        return self._connection.execute(
            """
            SELECT records.platform, records.category, COUNT(*),
                   MIN(records.timestamp), MAX(records.timestamp)
            FROM records_fts
            JOIN records ON records.record_id = records_fts.record_id
            WHERE records_fts MATCH ?
            GROUP BY records.platform, records.category
            ORDER BY records.platform, records.category
            """,
            (query,),
        ).fetchall()

    def _date_row(self, query: str | None) -> tuple[str | None, str | None]:
        if query is None:
            row = self._connection.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM records"
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT MIN(records.timestamp), MAX(records.timestamp)
                FROM records_fts
                JOIN records ON records.record_id = records_fts.record_id
                WHERE records_fts MATCH ?
                """,
                (query,),
            ).fetchone()
        return row[0], row[1]

    def _time_rows(self, query: str | None) -> list[tuple[str, int]]:
        if query is None:
            return self._connection.execute(
                """
                SELECT substr(timestamp, 1, 4), COUNT(*)
                FROM records
                WHERE timestamp IS NOT NULL
                GROUP BY substr(timestamp, 1, 4)
                ORDER BY substr(timestamp, 1, 4)
                LIMIT 25
                """
            ).fetchall()
        return self._connection.execute(
            """
            SELECT substr(records.timestamp, 1, 4), COUNT(*)
            FROM records_fts
            JOIN records ON records.record_id = records_fts.record_id
            WHERE records_fts MATCH ? AND records.timestamp IS NOT NULL
            GROUP BY substr(records.timestamp, 1, 4)
            ORDER BY substr(records.timestamp, 1, 4)
            LIMIT 25
            """,
            (query,),
        ).fetchall()

    def _top_value_rows(self, field: str, query: str | None) -> list[tuple[str, int]]:
        archive_sql, matching_sql = _TOP_VALUE_QUERIES[field]
        if query is None:
            return self._connection.execute(archive_sql).fetchall()
        return self._connection.execute(matching_sql, (query,)).fetchall()

    def _scope_facts(
        self,
        *,
        scope: Literal["archive", "matching"],
        query: str | None,
        record_count: int,
    ) -> list[CalculatedFact]:
        scope_definition = (
            "all_supported_records"
            if query is None
            else f"full_text_query_sha256:{sha256(query.encode('utf-8')).hexdigest()}"
        )
        facts = [
            _fact(
                scope=scope,
                scope_definition=scope_definition,
                metric="record_count",
                value=record_count,
            )
        ]
        earliest, latest = self._date_row(query)
        if earliest:
            facts.append(
                _fact(
                    scope=scope,
                    scope_definition=scope_definition,
                    metric="earliest_timestamp",
                    value=earliest,
                )
            )
        if latest:
            facts.append(
                _fact(
                    scope=scope,
                    scope_definition=scope_definition,
                    metric="latest_timestamp",
                    value=latest,
                )
            )

        for platform, category, count, category_earliest, category_latest in self._coverage_rows(
            query
        ):
            dimensions = {"platform": str(platform), "category": str(category)}
            facts.append(
                _fact(
                    scope=scope,
                    scope_definition=scope_definition,
                    metric="record_count",
                    value=int(str(count)),
                    dimensions=dimensions,
                )
            )
            if category_earliest:
                facts.append(
                    _fact(
                        scope=scope,
                        scope_definition=scope_definition,
                        metric="earliest_timestamp",
                        value=str(category_earliest),
                        dimensions=dimensions,
                    )
                )
            if category_latest:
                facts.append(
                    _fact(
                        scope=scope,
                        scope_definition=scope_definition,
                        metric="latest_timestamp",
                        value=str(category_latest),
                        dimensions=dimensions,
                    )
                )

        for year, count in self._time_rows(query):
            facts.append(
                _fact(
                    scope=scope,
                    scope_definition=scope_definition,
                    metric="record_count",
                    value=count,
                    dimensions={"year": year},
                )
            )
        for field in ("service", "hostname", "device", "activity_type"):
            for raw_value, count in self._top_value_rows(field, query):
                facts.append(
                    _fact(
                        scope=scope,
                        scope_definition=scope_definition,
                        metric="record_count",
                        value=count,
                        dimensions={field: str(raw_value)},
                    )
                )
        return facts

    def _candidate_records(
        self,
        query: str | None,
        *,
        limit: int,
    ) -> list[NormalizedRecord]:
        if query is None:
            rows = self._connection.execute(
                """
                WITH ranked AS (
                    SELECT payload, platform, category, timestamp, record_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY platform, category,
                                            COALESCE(substr(timestamp, 1, 4), 'unknown')
                               ORDER BY timestamp DESC, record_id
                           ) AS stratum_rank
                    FROM records
                )
                SELECT payload
                FROM ranked
                ORDER BY stratum_rank, platform, category, timestamp DESC, record_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                WITH matches AS (
                    SELECT records.payload, records.platform, records.category,
                           records.timestamp, records.record_id,
                           bm25(records_fts) AS relevance
                    FROM records_fts
                    JOIN records ON records.record_id = records_fts.record_id
                    WHERE records_fts MATCH ?
                ),
                ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY platform, category,
                                            COALESCE(substr(timestamp, 1, 4), 'unknown')
                               ORDER BY relevance, record_id
                           ) AS stratum_rank
                    FROM matches
                )
                SELECT payload
                FROM ranked
                ORDER BY stratum_rank, relevance, record_id
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [NormalizedRecord.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _diversify(
        candidates: Sequence[NormalizedRecord],
        *,
        limit: int,
    ) -> tuple[NormalizedRecord, ...]:
        if len(candidates) <= limit:
            return tuple(candidates)

        group_counts: Counter[tuple[str, str]] = Counter()
        for record in candidates:
            for field, value in (
                ("service", record.service),
                ("hostname", record.hostname),
                ("device", record.device),
                ("activity_type", record.activity_type),
            ):
                if value:
                    group_counts[(field, value)] += 1

        selected: list[NormalizedRecord] = []
        selected_ids: set[str] = set()
        seen_groups: set[tuple[object, ...]] = set()
        indexed = list(enumerate(candidates))
        while len(selected) < limit:
            best: tuple[int, int, NormalizedRecord, tuple[tuple[object, ...], ...]] | None = None
            for index, record in indexed:
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
                    if value and group_counts[(field, value)] > 1:
                        groups.append((field, value))
                novelty = sum(group not in seen_groups for group in groups)
                candidate = (novelty, -index, record, tuple(groups))
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
            if best is None:
                break
            _, _, record, selected_groups = best
            selected.append(record)
            selected_ids.add(record.record_id)
            seen_groups.update(selected_groups)
        return tuple(selected)

    def question_context(
        self,
        question: str,
        *,
        candidate_limit: int = 1_000,
        evidence_limit: int = 100,
    ) -> QuestionContext:
        total_records = self._matching_count(None)
        query = _fts_query(question)
        matching_records = self._matching_count(query)
        archive_facts = self._scope_facts(
            scope="archive",
            query=None,
            record_count=total_records,
        )
        if query is None:
            facts = [
                archive_facts[0],
                _fact(
                    scope="matching",
                    scope_definition="all_supported_records",
                    metric="record_count",
                    value=matching_records,
                ),
            ]
            facts.extend(archive_facts[1:])
        else:
            matching_facts = self._scope_facts(
                scope="matching",
                query=query,
                record_count=matching_records,
            )
            facts = [archive_facts[0], matching_facts[0]]
            facts.extend(matching_facts[1:])
            facts.extend(archive_facts[1:])
        candidates = self._candidate_records(query, limit=candidate_limit)
        return QuestionContext(
            total_records=total_records,
            matching_records=matching_records,
            selection_mode="archive" if query is None else "full_text",
            facts=tuple(facts),
            records=self._diversify(candidates, limit=evidence_limit),
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
        count = 0
        with ArchiveReader(paths, allow_large_archive=allow_large_archive) as reader:
            parser.validate(reader)
            for record in parser.iter_records(reader):
                session.add_record(record)
                count += 1
        counts[platform_id] = count
        if progress:
            progress(f"Indexed {count:,} {parser.definition.display_name} records.")
    session.commit()
    return counts
