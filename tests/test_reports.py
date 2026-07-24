from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from centaur_data_lens.errors import DataLensError
from centaur_data_lens.models import (
    ClaimKind,
    EvidenceItem,
    PrivacySnapshot,
)
from centaur_data_lens.reports import render_html, render_report, write_report
from centaur_data_lens.security import secure_write_text


def malicious_snapshot() -> PrivacySnapshot:
    return PrivacySnapshot(
        generated_at=datetime.now(UTC),
        platforms=["google"],
        total_records=1,
        coverage=[],
        common_hostnames=[],
        overlapping_hostnames=[],
        evidence=[
            EvidenceItem(
                record_id="abc",
                platform="google",
                category="activity",
                title='</script><img src=x onerror="alert(1)">\x1b]8;;https://evil.invalid\x07',
                source="../../secret",
                claim_kind=ClaimKind.OBSERVED,
            )
        ],
        omissions={"google": ["Gmail"]},
    )


def test_html_is_offline_and_escapes_untrusted_data() -> None:
    rendered = render_html(malicious_snapshot())
    assert "connect-src 'none'" in rendered
    assert "default-src 'none'" in rendered
    assert "\\u003c/script\\u003e" in rendered
    assert '<img src=x onerror="alert(1)">' not in rendered
    assert "innerHTML" not in rendered
    assert "fetch(" not in rendered
    assert "http://evil.invalid" not in rendered


def test_secure_report_permissions(tmp_path: Path) -> None:
    output = tmp_path / "report.html"
    write_report(
        malicious_snapshot(),
        report_format="html",
        output=output,
    )
    assert output.exists()
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600


def test_refuses_symlink_output(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("safe", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(DataLensError, match="symbolic link"):
        secure_write_text(link, "unsafe", overwrite=True)
    assert target.read_text(encoding="utf-8") == "safe"


def test_all_report_formats_and_overwrite(tmp_path: Path) -> None:
    snapshot = malicious_snapshot()
    assert "# Centaur Data Lens" in render_report(snapshot, "markdown")
    assert '"total_records": 1' in render_report(snapshot, "json")
    output = tmp_path / "report.md"
    write_report(snapshot, report_format="markdown", output=output)
    with pytest.raises(DataLensError, match="already exists"):
        write_report(snapshot, report_format="markdown", output=output)
    write_report(snapshot, report_format="markdown", output=output, overwrite=True)
    assert "Cited evidence" in output.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Only HTML"):
        write_report(
            snapshot,
            report_format="markdown",
            output=tmp_path / "not-created.md",
            open_report=True,
        )
    assert not (tmp_path / "not-created.md").exists()
