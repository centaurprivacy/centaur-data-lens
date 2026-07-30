from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import ijson  # type: ignore[import-untyped]

from centaur_data_lens.archive import ArchiveEntry, ArchiveReader
from centaur_data_lens.errors import ArchiveSafetyError, UnsupportedExportError
from centaur_data_lens.models import NormalizedRecord, SourceReference
from centaur_data_lens.security import sanitize_terminal

_MAX_OBJECT_DOCUMENT = 128 * 1024**2
_TIMESTAMP_KEYS = (
    "time",
    "timestamp",
    "timestamp_usec",
    "time_usec",
    "date",
    "creation_timestamp",
    "creation_time",
)
_TITLE_KEYS = ("title", "name", "label", "activity", "query", "value")
_URL_KEYS = ("titleUrl", "title_url", "url", "href", "link")
_DEVICE_KEYS = ("device", "device_name", "user_agent", "userAgent")
_ATTRIBUTE_DENY_RE = re.compile(
    r"(password|passwd|secret|token|credential|message|body|content|contact|phone|email)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlatformDefinition:
    platform_id: str
    display_name: str
    last_verified: str
    official_url: str
    supported: tuple[str, ...]
    excluded: tuple[str, ...]
    guide: tuple[str, ...]


class PlatformParser(ABC):
    definition: PlatformDefinition

    @abstractmethod
    def supported_path(self, path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def category_for(self, path: str) -> str:
        raise NotImplementedError

    def validate(self, reader: ArchiveReader) -> list[ArchiveEntry]:
        selected = reader.json_entries(self.supported_path)
        if not selected:
            raise UnsupportedExportError(
                f"No supported {self.definition.display_name} JSON categories were found. "
                f"Run 'centaur-data-lens guide {self.definition.platform_id}' for export settings."
            )
        return selected

    def iter_records(self, reader: ArchiveReader) -> Iterator[NormalizedRecord]:
        for entry in self.validate(reader):
            category = self.category_for(entry.path)
            for pointer, value in iter_json_records(reader, entry):
                yield normalize_record(
                    platform=self.definition.platform_id,
                    category=category,
                    path=entry.path,
                    source_id=entry.source_id,
                    pointer=pointer,
                    value=value,
                )


def _first_non_whitespace(reader: ArchiveReader, entry: ArchiveEntry) -> bytes:
    with reader.open_entry(entry) as stream:
        while True:
            char = stream.read(1)
            if not char or not char.isspace():
                return bytes(char)


def iter_json_records(
    reader: ArchiveReader, entry: ArchiveEntry
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    try:
        yield from _iter_json_records(reader, entry)
    except ArchiveSafetyError:
        raise
    except (ijson.JSONError, UnicodeError, ValueError, RecursionError) as exc:
        raise ArchiveSafetyError("A selected JSON export file is malformed.") from exc


def _iter_json_records(
    reader: ArchiveReader, entry: ArchiveEntry
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    first = _first_non_whitespace(reader, entry)
    if first == b"[":
        with reader.open_entry(entry) as stream:
            try:
                for index, item in enumerate(ijson.items(stream, "item")):
                    yield from _walk_candidates(item, f"/{index}")
            except (ijson.JSONError, UnicodeError, ValueError) as exc:
                raise ArchiveSafetyError("A selected JSON export file is malformed.") from exc
        return
    if first == b"{":
        if entry.size > _MAX_OBJECT_DOCUMENT:
            yield from _iter_large_object_arrays(reader, entry)
            return
        with reader.open_entry(entry) as stream:
            try:
                document = json.load(stream)
            except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                raise ArchiveSafetyError("A selected JSON export file is malformed.") from exc
        yield from _walk_candidates(document, "")
        return
    raise ArchiveSafetyError("A selected JSON file does not contain an object or array.")


def _iter_large_object_arrays(
    reader: ArchiveReader, entry: ArchiveEntry
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    root_key: str | None = None
    active_array_key: str | None = None
    item_builder: Any | None = None
    item_depth = 0
    item_index = 0
    found_array = False
    with reader.open_entry(entry) as stream:
        try:
            for prefix, event, value in ijson.parse(stream):
                if active_array_key is None:
                    if prefix == "" and event == "map_key":
                        root_key = str(value)
                        continue
                    if root_key is None:
                        continue
                    if event == "start_array":
                        active_array_key = root_key
                        root_key = None
                        item_index = 0
                        found_array = True
                    else:
                        root_key = None
                    continue

                if item_builder is None:
                    if event == "end_array":
                        active_array_key = None
                        continue
                    item_builder = ijson.ObjectBuilder()
                    item_builder.event(event, value)
                    if event in {"start_array", "start_map"}:
                        item_depth = 1
                        continue
                    item = item_builder.value
                    yield from _walk_candidates(
                        item,
                        f"/{_escape_pointer(active_array_key)}/{item_index}",
                    )
                    item_index += 1
                    item_builder = None
                    continue

                item_builder.event(event, value)
                if event in {"start_array", "start_map"}:
                    item_depth += 1
                elif event in {"end_array", "end_map"}:
                    item_depth -= 1
                if item_depth == 0:
                    item = item_builder.value
                    yield from _walk_candidates(
                        item,
                        f"/{_escape_pointer(active_array_key)}/{item_index}",
                    )
                    item_index += 1
                    item_builder = None
        except (ijson.JSONError, UnicodeError, ValueError) as exc:
            raise ArchiveSafetyError("A selected JSON export file is malformed.") from exc
    if not found_array:
        raise ArchiveSafetyError("A large JSON object has no streamable top-level record array.")


def _looks_like_record(value: Mapping[str, Any]) -> bool:
    keys = {str(key) for key in value}
    has_identity = bool(keys.intersection(_TITLE_KEYS + _TIMESTAMP_KEYS + _URL_KEYS))
    scalar_count = sum(
        isinstance(item, (str, int, float, bool)) or item is None for item in value.values()
    )
    return has_identity and scalar_count > 0


def _escape_pointer(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _walk_candidates(value: Any, pointer: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        if _looks_like_record(value):
            yield pointer or "/", value
            return
        for key, child in value.items():
            yield from _walk_candidates(child, f"{pointer}/{_escape_pointer(key)}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            yield from _walk_candidates(child, f"{pointer}/{index}")


def _nested_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return sanitize_terminal(value, limit=1_000)
    if isinstance(value, Mapping):
        for key in ("value", "href", "name", "text"):
            nested = value.get(key)
            if isinstance(nested, (str, int, float, bool)) or nested is None:
                return _nested_scalar(nested)
    return None


def _first_scalar(value: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        if key not in value:
            continue
        scalar = _nested_scalar(value[key])
        if scalar is not None:
            rendered = sanitize_terminal(scalar, limit=1_000).strip()
            if rendered:
                return rendered
    return None


def _parse_timestamp(value: Mapping[str, Any]) -> tuple[datetime | None, str | None]:
    for key in _TIMESTAMP_KEYS:
        raw = value.get(key)
        if isinstance(raw, Mapping):
            raw = raw.get("timestamp") or raw.get("value")
        if isinstance(raw, (int, float)):
            try:
                numeric = float(raw)
            except OverflowError:
                continue
            precision = "second"
            if numeric > 100_000_000_000_000:
                numeric /= 1_000_000
                precision = "microsecond"
            elif numeric > 100_000_000_000:
                numeric /= 1_000
                precision = "millisecond"
            try:
                return datetime.fromtimestamp(numeric, tz=UTC), precision
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(raw, str):
            candidate = raw.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC), "provided"
            except ValueError:
                continue
    return None, None


def _service(value: Mapping[str, Any], path: str) -> str | None:
    header = _first_scalar(value, ("header", "product", "service"))
    if header:
        return header
    products = value.get("products")
    if isinstance(products, list) and products and isinstance(products[0], str):
        return sanitize_terminal(products[0], limit=200)
    parts = Path(path).parts
    return sanitize_terminal(parts[-2], limit=200) if len(parts) > 1 else None


def _hostname(value: Mapping[str, Any]) -> str | None:
    url = _first_scalar(value, _URL_KEYS)
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None
        hostname = parsed.hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


def _sensitivity(category: str, title: str | None) -> set[str]:
    combined = f"{category} {title or ''}".lower()
    tags: set[str] = set()
    mapping = {
        "advertising": ("ad", "advertis", "interest"),
        "browsing": ("search", "browser", "activity", "youtube"),
        "device": ("device", "login", "session"),
        "location": ("location", "place", "map"),
        "identity": ("profile", "account"),
    }
    for tag, needles in mapping.items():
        if any(needle in combined for needle in needles):
            tags.add(tag)
    return tags


def normalize_record(
    *,
    platform: str,
    category: str,
    path: str,
    source_id: str,
    pointer: str,
    value: Mapping[str, Any],
) -> NormalizedRecord:
    timestamp, precision = _parse_timestamp(value)
    title = _first_scalar(value, _TITLE_KEYS)
    attributes: dict[str, str | int | float | bool | None] = {}
    for key, raw in value.items():
        key_text = sanitize_terminal(key, limit=100)
        if _ATTRIBUTE_DENY_RE.search(key_text):
            continue
        scalar = _nested_scalar(raw)
        if scalar is not None and len(attributes) < 16:
            attributes[key_text] = scalar

    source = SourceReference(
        archive_id=source_id,
        internal_path=sanitize_terminal(path, limit=1_000),
        pointer=pointer,
    )
    identity = json.dumps(
        {
            "platform": platform,
            "category": category,
            "timestamp": timestamp.isoformat() if timestamp else None,
            "title": title,
            "hostname": _hostname(value),
            "device": _first_scalar(value, _DEVICE_KEYS),
            "attributes": attributes,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return NormalizedRecord(
        record_id=digest,
        platform=platform,
        category=category,
        activity_type=category.replace("_", " "),
        service=_service(value, path),
        timestamp=timestamp,
        timestamp_precision=precision,
        title=title,
        hostname=_hostname(value),
        device=_first_scalar(value, _DEVICE_KEYS),
        attributes=attributes,
        sensitivity_tags=_sensitivity(category, title),
        sources=(source,),
    )
