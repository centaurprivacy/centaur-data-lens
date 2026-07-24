from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from centaur_data_lens.errors import DataLensError

_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_SESSION_MARKER = ".centaur-data-lens-session"


def sanitize_terminal(value: object, *, limit: int = 2_000) -> str:
    """Remove terminal controls, bidi overrides, and Rich markup ambiguity."""
    text = str(value)
    text = _CONTROL_RE.sub("\N{REPLACEMENT CHARACTER}", text)
    return text[:limit]


def safe_embedded_json(value: Any) -> str:
    """Serialize JSON so it cannot terminate a non-executable script element."""
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        rendered.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def html_text(value: object) -> str:
    return html.escape(sanitize_terminal(value), quote=True)


def secure_write_text(path: Path, content: str, *, overwrite: bool = False) -> None:
    """Atomically create a user-only regular file without following symlinks."""
    expanded = path.expanduser()
    path = expanded.parent.resolve(strict=False) / expanded.name
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() or path.is_symlink():
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise DataLensError("Unable to inspect the output destination.") from exc
        if stat.S_ISLNK(mode):
            raise DataLensError("Refusing to write a report through a symbolic link.")
        if not overwrite:
            raise DataLensError("Output already exists; pass --overwrite to replace it.")
        if not stat.S_ISREG(mode):
            raise DataLensError("Output destination is not a regular file.")

    temp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    temp_path = path.parent / temp_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        with suppress(OSError):
            os.chmod(path, 0o600)
    except OSError as exc:
        raise DataLensError("Unable to write the requested output securely.") from exc
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)


def secure_temp_directory(prefix: str = "centaur-data-lens-") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    with suppress(OSError):
        os.chmod(path, 0o700)
    secure_write_text(path / _SESSION_MARKER, "ephemeral-v1\n")
    return path


def cleanup_stale_sessions(*, older_than_seconds: int = 24 * 60 * 60) -> int:
    """Remove only old, user-owned temporary directories carrying our marker."""
    base = Path(tempfile.gettempdir()).resolve()
    now = time.time()
    removed = 0
    for candidate in base.glob("centaur-data-lens-*"):
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(base)
            marker = resolved / _SESSION_MARKER
            if not marker.is_file() or marker.is_symlink():
                continue
            if hasattr(os, "getuid") and resolved.stat().st_uid != os.getuid():
                continue
            if now - marker.stat().st_mtime < older_than_seconds:
                continue
            if marker.read_text(encoding="utf-8") != "ephemeral-v1\n":
                continue
            shutil.rmtree(resolved)
            removed += 1
        except (OSError, UnicodeError, ValueError):
            continue
    return removed
