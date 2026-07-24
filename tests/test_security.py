from __future__ import annotations

from pathlib import Path

import pytest

from centaur_data_lens.errors import DataLensError
from centaur_data_lens.platforms import get_platform
from centaur_data_lens.security import (
    cleanup_stale_sessions,
    safe_embedded_json,
    sanitize_terminal,
    secure_write_text,
)


def test_terminal_and_json_sanitization() -> None:
    rendered = sanitize_terminal("safe\x1b]8;;https://evil.invalid\x07text\u202e")
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered
    embedded = safe_embedded_json({"value": "</script>&\u2028"})
    assert "</script>" not in embedded
    assert "\\u003c/script\\u003e" in embedded


def test_secure_writer_refuses_non_regular_and_requires_overwrite(tmp_path: Path) -> None:
    with pytest.raises(DataLensError, match="not a regular"):
        secure_write_text(tmp_path, "no", overwrite=True)
    output = tmp_path / "output"
    secure_write_text(output, "one")
    with pytest.raises(DataLensError, match="already exists"):
        secure_write_text(output, "two")
    secure_write_text(output, "two", overwrite=True)
    assert output.read_text(encoding="utf-8") == "two"


def test_registry_rejects_unknown_platform() -> None:
    with pytest.raises(DataLensError, match="Unsupported platform"):
        get_platform("unknown")


def test_stale_cleanup_only_removes_marked_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = tmp_path / "centaur-data-lens-stale"
    stale.mkdir()
    (stale / ".centaur-data-lens-session").write_text("ephemeral-v1\n", encoding="utf-8")
    unrelated = tmp_path / "centaur-data-lens-unrelated"
    unrelated.mkdir()
    monkeypatch.setattr("centaur_data_lens.security.tempfile.gettempdir", lambda: str(tmp_path))
    assert cleanup_stale_sessions(older_than_seconds=0) == 1
    assert not stale.exists()
    assert unrelated.exists()
