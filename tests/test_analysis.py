from __future__ import annotations

from pathlib import Path

import pytest

from centaur_data_lens.analysis import AnalysisSession, SourceSpec, analyze_sources


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
