from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources
from centaur_data_lens.models import NormalizedRecord, SourceReference


def test_cross_platform_analysis_and_ephemeral_cleanup(
    google_export: Path, meta_export: Path
) -> None:
    session = AnalysisSession()
    database_path = session.database_path
    counts = analyze_sources(
        session,
        [
            SourceSpec("google", google_export),
            SourceSpec("meta", meta_export),
        ],
    )
    snapshot = session.snapshot()
    assert counts == {"google": 5, "meta": 4}
    assert snapshot.total_records == 9
    assert snapshot.platforms == ["google", "meta"]
    assert "shared.example" in snapshot.overlapping_hostnames
    assert "Synthetic Phone" in snapshot.overlapping_devices
    assert all("must never be parsed" not in item.title for item in snapshot.evidence)
    session.close()
    assert not database_path.exists()


def test_search_returns_relevant_record(google_export: Path) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        results = session.search("privacy tools")
        assert results
        assert "privacy tools" in (results[0].title or "")


def test_context_manager_removes_session_after_exception() -> None:
    database_path: Path | None = None
    with pytest.raises(RuntimeError, match="synthetic failure"), AnalysisSession() as session:
        database_path = session.database_path
        raise RuntimeError("synthetic failure")
    assert database_path is not None
    assert not database_path.exists()


def test_diagnostics_contain_counts_not_values(google_export: Path) -> None:
    with AnalysisSession() as session:
        analyze_sources(session, [SourceSpec("google", google_export)])
        diagnostics = session.diagnostics()
    rendered = str(diagnostics)
    assert "privacy tools" not in rendered
    assert diagnostics["values_included"] is False


def _add_large_synthetic_dataset(session: AnalysisSession) -> None:
    for index in range(240):
        is_privacy = index < 180
        platform = "google" if index % 2 == 0 else "meta"
        category = ("search_history", "browser_history", "advertising")[index % 3]
        year = 2021 + (index % 5)
        session.add_record(
            NormalizedRecord(
                record_id=f"synthetic-{index:03d}",
                platform=platform,
                category=category,
                activity_type=category.replace("_", " "),
                service=f"Synthetic Service {index % 6}",
                timestamp=datetime(year, (index % 12) + 1, 1, tzinfo=UTC),
                timestamp_precision="provided",
                title=f"{'privacy' if is_privacy else 'video'} synthetic activity {index}",
                hostname=f"group-{index % 8}.example",
                device=f"Synthetic Device {index % 4}",
                attributes={"synthetic_index": index},
                sensitivity_tags={"browsing"},
                sources=(
                    SourceReference(
                        archive_id="synthetic-archive",
                        internal_path="synthetic/activity.json",
                        pointer=f"/{index}",
                    ),
                ),
            )
        )
    session.commit()


def test_question_context_uses_full_population_and_diversified_evidence() -> None:
    with AnalysisSession() as session:
        _add_large_synthetic_dataset(session)
        context = session.question_context("privacy")

    assert context.total_records == 240
    assert context.matching_records == 180
    assert len(context.records) == 100
    assert {record.platform for record in context.records} == {"google", "meta"}
    assert len({record.category for record in context.records}) == 3
    assert len({record.timestamp.year for record in context.records if record.timestamp}) == 5

    archive_total = next(
        fact
        for fact in context.facts
        if fact.scope == "archive" and fact.metric == "record_count" and not fact.dimensions
    )
    matching_total = next(
        fact
        for fact in context.facts
        if fact.scope == "matching" and fact.metric == "record_count" and not fact.dimensions
    )
    assert archive_total.value == 240
    assert matching_total.value == 180
    assert matching_total.scope_definition.startswith("full_text_query_sha256:")


def test_question_context_handles_broad_and_no_match_questions() -> None:
    with AnalysisSession() as session:
        _add_large_synthetic_dataset(session)
        broad = session.question_context("Give me an overview of my data export")
        privacy_related = session.question_context("privacy-related activity")
        incompatible = session.question_context("privacy video")
        late_meaningful_term = session.question_context(" ".join(["what"] * 25 + ["privacy"]))
        missing = session.question_context("quantum zebras")
        repeated = session.question_context("quantum zebras")
        different = session.question_context("privacy")

    assert broad.selection_mode == "archive"
    assert broad.matching_records == 240
    assert len(broad.records) == 100
    assert privacy_related.matching_records == 180
    assert privacy_related.matching_records == different.matching_records
    assert incompatible.matching_records == 0
    assert incompatible.records == ()
    assert len(incompatible.facts) == 1
    assert incompatible.facts[0].scope == "matching"
    assert late_meaningful_term.matching_records == 180
    assert missing.matching_records == 0
    assert missing.records == ()
    missing_ids = [fact.fact_id for fact in missing.facts if fact.scope == "matching"]
    assert missing_ids == [fact.fact_id for fact in repeated.facts if fact.scope == "matching"]
    assert missing_ids != [fact.fact_id for fact in different.facts if fact.scope == "matching"]


def test_natural_question_modifiers_preserve_search_and_facet_intent() -> None:
    with AnalysisSession() as session:
        _add_large_synthetic_dataset(session)
        searches = session.question_context("What did I search for?")
        devices = session.question_context("Which devices are present?")
        services = session.question_context("What services appear most often?")

    assert searches.selection_mode == "full_text_all_terms"
    assert searches.matching_records == 80
    assert searches.records
    assert {record.category for record in searches.records} == {"search_history"}
    for context in (devices, services):
        assert context.selection_mode == "full_text_all_terms"
        assert context.matching_records == 240
        assert len(context.records) == 100


def test_analysis_reports_unique_indexed_and_parsed_counts(tmp_path: Path) -> None:
    duplicate = {
        "header": "Search",
        "title": "Synthetic duplicate",
        "titleUrl": "https://example.invalid/search",
        "time": "2025-01-02T03:04:05Z",
    }
    export = tmp_path / "duplicates.zip"
    with zipfile.ZipFile(export, "w") as archive:
        archive.writestr(
            "Takeout/My Activity/Search/MyActivity.json",
            json.dumps([duplicate, duplicate]),
        )

    progress: list[str] = []
    with AnalysisSession() as session:
        counts = analyze_sources(
            session,
            [SourceSpec("google", export)],
            progress=progress.append,
        )
        total_records = session.snapshot().total_records

    assert counts == {"google": 1}
    assert total_records == 1
    assert progress[-1] == "Indexed 1 unique Google records from 2 parsed entries."
