from __future__ import annotations

import json
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

import centaur_data_lens.platforms.base as platform_base
from centaur_data_lens.archive import ArchiveLimits, ArchiveReader
from centaur_data_lens.errors import ArchiveSafetyError, UnsupportedExportError
from centaur_data_lens.platforms import get_platform
from centaur_data_lens.platforms.base import normalize_record


def test_rejects_path_traversal(tmp_path: Path) -> None:
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../Takeout/My Activity/bad.json", "[]")
    with ArchiveReader([path]) as reader, pytest.raises(ArchiveSafetyError):
        _ = reader.entries


def test_rejects_duplicate_normalized_zip_member_paths(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.zip"
    member = "Takeout/My Activity/Search/MyActivity.json"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, "[]")
            archive.writestr(member, b"x" * 202)
    with (
        ArchiveReader(
            [path],
            limits=ArchiveLimits(max_structured_file_bytes=10),
        ) as reader,
        pytest.raises(ArchiveSafetyError, match="duplicate"),
    ):
        _ = reader.entries


def test_rejects_symlink_entry_on_read(tmp_path: Path) -> None:
    path = tmp_path / "link.zip"
    info = zipfile.ZipInfo("Takeout/My Activity/Search/MyActivity.json")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "target")
    with ArchiveReader([path]) as reader, pytest.raises(ArchiveSafetyError):
        list(get_platform("google").iter_records(reader))


def test_ignores_media_without_reading(google_export: Path) -> None:
    with ArchiveReader([google_export]) as reader:
        selected = get_platform("google").validate(reader)
    assert all(entry.path.endswith(".json") for entry in selected)
    assert all("Photos" not in entry.path for entry in selected)


def test_explicit_platform_mismatch(meta_export: Path) -> None:
    with (
        ArchiveReader([meta_export]) as reader,
        pytest.raises(UnsupportedExportError, match="Google"),
    ):
        get_platform("google").validate(reader)


def test_rejects_missing_symlink_and_wrong_extension(tmp_path: Path) -> None:
    with pytest.raises(ArchiveSafetyError, match="does not exist"):
        ArchiveReader([tmp_path / "missing.zip"])
    text = tmp_path / "export.txt"
    text.write_text("not an export", encoding="utf-8")
    with pytest.raises(ArchiveSafetyError, match="ZIP"):
        ArchiveReader([text])
    link = tmp_path / "export-link"
    try:
        link.symlink_to(text)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ArchiveSafetyError, match="symbolic link"):
        ArchiveReader([link])


def test_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "malformed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Takeout/My Activity/Search/MyActivity.json", "[not-json")
    with ArchiveReader([path]) as reader, pytest.raises(ArchiveSafetyError, match="malformed"):
        list(get_platform("google").iter_records(reader))


def test_rejects_corrupt_zip_member_crc_safely(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.zip"
    member = "Takeout/My Activity/Search/MyActivity.json"
    payload = b'[ {"title":"safe"} ]'
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(member, payload)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)

    with path.open("r+b") as handle:
        handle.seek(info.header_offset + 26)
        name_length = int.from_bytes(handle.read(2), "little")
        extra_length = int.from_bytes(handle.read(2), "little")
        data_offset = info.header_offset + 30 + name_length + extra_length
        handle.seek(data_offset + 1)
        assert handle.read(1) == b" "
        handle.seek(data_offset + 1)
        handle.write(b"\t")

    with ArchiveReader([path]) as reader, pytest.raises(ArchiveSafetyError, match="safely"):
        list(get_platform("google").iter_records(reader))


def test_enforces_configurable_entry_limit(google_export: Path) -> None:
    with (
        ArchiveReader(
            [google_export],
            limits=ArchiveLimits(max_entries=1),
        ) as reader,
        pytest.raises(ArchiveSafetyError, match="too many entries"),
    ):
        _ = reader.entries


def test_reads_extracted_directory(google_export: Path, tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(google_export) as archive:
        archive.extractall(extracted)
    with ArchiveReader([extracted]) as reader:
        records = list(get_platform("google").iter_records(reader))
    assert len(records) == 5


def test_streams_large_object_shaped_json(
    monkeypatch: pytest.MonkeyPatch, google_export: Path
) -> None:
    monkeypatch.setattr(platform_base, "_MAX_OBJECT_DOCUMENT", 1)
    with ArchiveReader([google_export]) as reader:
        records = list(get_platform("google").iter_records(reader))
    assert len(records) == 5


def test_streams_every_top_level_record_array_after_scalar_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(platform_base, "_MAX_OBJECT_DOCUMENT", 1)
    path = tmp_path / "large-object.zip"
    document = {
        "metadata": "v1",
        "records": [{"title": "first", "time": "2026-01-01T00:00:00Z"}],
        "additional": [{"title": "second", "time": "2026-01-02T00:00:00Z"}],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Takeout/My Activity/Search/MyActivity.json",
            json.dumps(document),
        )
    with ArchiveReader([path]) as reader:
        records = list(get_platform("google").iter_records(reader))
    assert [record.title for record in records] == ["first", "second"]
    assert [record.sources[0].pointer for record in records] == [
        "/records/0",
        "/additional/0",
    ]


def test_malformed_url_has_no_hostname() -> None:
    record = normalize_record(
        platform="google",
        category="activity",
        path="Takeout/My Activity/Search/MyActivity.json",
        source_id="synthetic",
        pointer="/0",
        value={"title": "malformed URL", "titleUrl": "http://["},
    )
    assert record.hostname is None


def test_deeply_nested_json_fails_centrally_for_zip_and_directory(tmp_path: Path) -> None:
    root = tmp_path / "extracted"
    activity = root / "Takeout" / "My Activity" / "Search"
    activity.mkdir(parents=True)
    depth = 2_000
    document = "[" + '{"nested":' * depth + '{"title":"deep"}' + "}" * depth + "]"
    (activity / "MyActivity.json").write_text(document, encoding="utf-8")
    archive_path = tmp_path / "deep.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Takeout/My Activity/Search/MyActivity.json", document)

    for source in (root, archive_path):
        with (
            ArchiveReader([source]) as reader,
            pytest.raises(ArchiveSafetyError, match="malformed"),
        ):
            list(get_platform("google").iter_records(reader))
