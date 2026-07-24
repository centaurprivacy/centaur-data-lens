from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

from centaur_data_lens.errors import ArchiveSafetyError

_NESTED_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".gz", ".7z", ".rar"}


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 1_000_000
    max_structured_file_bytes: int = 2 * 1024**3
    max_structured_total_bytes: int = 10 * 1024**3
    max_compression_ratio: float = 1_000.0


@dataclass(frozen=True)
class ArchiveEntry:
    source_id: str
    path: str
    size: int
    compressed_size: int
    encrypted: bool = False
    symlink: bool = False
    nested_archive: bool = False
    _source_index: int = field(repr=False, default=0)
    _member_name: str = field(repr=False, default="")


class _ArchiveSource:
    def entries(self, source_index: int) -> Iterator[ArchiveEntry]:
        raise NotImplementedError

    @contextmanager
    def open(self, entry: ArchiveEntry) -> Iterator[IO[bytes]]:
        raise NotImplementedError

    def close(self) -> None:
        return


def _source_id(path: Path) -> str:
    stat_result = path.stat()
    identity = f"{path.name}\0{stat_result.st_size}\0{stat_result.st_mtime_ns}".encode()
    return hashlib.sha256(identity).hexdigest()[:12]


def _safe_member_path(raw: str) -> str:
    normalized = raw.replace("\\", "/")
    if "\x00" in normalized:
        raise ArchiveSafetyError("Archive contains an invalid path.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ArchiveSafetyError("Archive contains a path-traversal entry.")
    if any(ord(char) < 32 for char in normalized):
        raise ArchiveSafetyError("Archive contains a path with control characters.")
    return str(path)


class _ZipSource(_ArchiveSource):
    def __init__(self, path: Path) -> None:
        try:
            self._zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveSafetyError("Unable to open a ZIP archive.") from exc
        self._id = _source_id(path)

    def entries(self, source_index: int) -> Iterator[ArchiveEntry]:
        for info in self._zip.infolist():
            if info.is_dir():
                continue
            safe_path = _safe_member_path(info.filename)
            mode = info.external_attr >> 16
            yield ArchiveEntry(
                source_id=self._id,
                path=safe_path,
                size=info.file_size,
                compressed_size=info.compress_size,
                encrypted=bool(info.flag_bits & 0x1),
                symlink=stat.S_ISLNK(mode),
                nested_archive=Path(safe_path).suffix.lower() in _NESTED_ARCHIVE_SUFFIXES,
                _source_index=source_index,
                _member_name=info.filename,
            )

    @contextmanager
    def open(self, entry: ArchiveEntry) -> Iterator[IO[bytes]]:
        try:
            with self._zip.open(entry._member_name, "r") as handle:
                yield handle
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ArchiveSafetyError("Unable to read an archive entry safely.") from exc

    def close(self) -> None:
        self._zip.close()


class _DirectorySource(_ArchiveSource):
    def __init__(self, path: Path) -> None:
        self._root = path.resolve(strict=True)
        self._id = _source_id(path)

    def entries(self, source_index: int) -> Iterator[ArchiveEntry]:
        for root, directories, filenames in os.walk(self._root, followlinks=False):
            root_path = Path(root)
            directories[:] = [name for name in directories if not (root_path / name).is_symlink()]
            for filename in filenames:
                absolute = root_path / filename
                relative = _safe_member_path(absolute.relative_to(self._root).as_posix())
                stat_result = absolute.lstat()
                yield ArchiveEntry(
                    source_id=self._id,
                    path=relative,
                    size=stat_result.st_size,
                    compressed_size=stat_result.st_size,
                    symlink=stat.S_ISLNK(stat_result.st_mode),
                    nested_archive=absolute.suffix.lower() in _NESTED_ARCHIVE_SUFFIXES,
                    _source_index=source_index,
                    _member_name=relative,
                )

    @contextmanager
    def open(self, entry: ArchiveEntry) -> Iterator[IO[bytes]]:
        target = (self._root / entry._member_name).resolve(strict=True)
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ArchiveSafetyError("Directory entry escaped the selected export.") from exc
        if target.is_symlink() or not target.is_file():
            raise ArchiveSafetyError("Refusing to read a symbolic link or non-regular file.")
        with target.open("rb") as handle:
            yield handle


class ArchiveReader:
    """Safe, non-extracting view over one or more user-selected exports."""

    def __init__(
        self,
        paths: Sequence[Path],
        *,
        limits: ArchiveLimits | None = None,
        allow_large_archive: bool = False,
    ) -> None:
        if not paths:
            raise ArchiveSafetyError("Select at least one export path.")
        self.limits = limits or ArchiveLimits()
        self.allow_large_archive = allow_large_archive
        self._sources: list[_ArchiveSource] = []
        self._entries: list[ArchiveEntry] | None = None
        for supplied in paths:
            path = supplied.expanduser()
            if not path.exists():
                self.close()
                raise ArchiveSafetyError("A selected export path does not exist.")
            if path.is_symlink():
                self.close()
                raise ArchiveSafetyError("Refusing to open an export through a symbolic link.")
            if path.is_dir():
                self._sources.append(_DirectorySource(path))
            elif path.is_file() and path.suffix.lower() == ".zip":
                self._sources.append(_ZipSource(path))
            else:
                self.close()
                raise ArchiveSafetyError("Exports must be ZIP files or extracted directories.")

    @property
    def entries(self) -> list[ArchiveEntry]:
        if self._entries is None:
            gathered: list[ArchiveEntry] = []
            for index, source in enumerate(self._sources):
                for entry in source.entries(index):
                    gathered.append(entry)
                    if not self.allow_large_archive and len(gathered) > self.limits.max_entries:
                        raise ArchiveSafetyError("Archive contains too many entries.")
            self._entries = gathered
        return self._entries

    @contextmanager
    def open_entry(self, entry: ArchiveEntry) -> Iterator[IO[bytes]]:
        if entry.symlink:
            raise ArchiveSafetyError("Refusing to read a symbolic link from an export.")
        if entry.encrypted:
            raise ArchiveSafetyError("Encrypted ZIP entries are not supported.")
        if entry.nested_archive:
            raise ArchiveSafetyError("Nested archives are not supported.")
        if not self.allow_large_archive:
            if entry.size > self.limits.max_structured_file_bytes:
                raise ArchiveSafetyError("A structured export file exceeds the safe size limit.")
            if entry.compressed_size == 0 and entry.size:
                raise ArchiveSafetyError("Archive entry has an unsafe compression ratio.")
            if entry.compressed_size and (
                entry.size / entry.compressed_size > self.limits.max_compression_ratio
            ):
                raise ArchiveSafetyError("Archive entry has an unsafe compression ratio.")
        with self._sources[entry._source_index].open(entry) as handle:
            yield handle

    def json_entries(self, predicate: Callable[[str], bool]) -> list[ArchiveEntry]:
        selected = [
            entry
            for entry in self.entries
            if entry.path.lower().endswith(".json")
            and not entry.nested_archive
            and predicate(entry.path)
        ]
        if not self.allow_large_archive:
            total = sum(entry.size for entry in selected)
            if total > self.limits.max_structured_total_bytes:
                raise ArchiveSafetyError("Recognized structured data exceeds the safe size limit.")
        return selected

    def close(self) -> None:
        for source in self._sources:
            source.close()
        self._sources.clear()

    def __enter__(self) -> ArchiveReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
