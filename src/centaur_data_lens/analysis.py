from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from centaur_data_lens.archive import ArchiveReader
from centaur_data_lens.errors import DataLensError
from centaur_data_lens.models import (
    CoverageItem,
    EvidenceItem,
    NormalizedRecord,
    PrivacySnapshot,
)
from centaur_data_lens.platforms import get_platform
from centaur_data_lens.security import cleanup_stale_sessions, secure_temp_directory

ProgressCallback = Callable[[str], None]
_FTS_TOKEN_RE = re.compile(r"[\w.-]+", re.UNICODE)


@dataclass(frozen=True)
class SourceSpec:
    platform: str
    path: Path


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
                timestamp TEXT,
                hostname TEXT,
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
            INSERT INTO records(record_id, platform, category, timestamp, hostname, title, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.platform,
                record.category,
                record.timestamp.isoformat() if record.timestamp else None,
                record.hostname,
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
        tokens = _FTS_TOKEN_RE.findall(question)[:20]
        if not tokens:
            return list(self.records())[:limit]
        query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
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
