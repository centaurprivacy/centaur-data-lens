from __future__ import annotations

from centaur_data_lens.errors import DataLensError
from centaur_data_lens.platforms.base import PlatformParser
from centaur_data_lens.platforms.google import GoogleParser
from centaur_data_lens.platforms.meta import MetaParser

_PLATFORMS: dict[str, PlatformParser] = {
    "google": GoogleParser(),
    "meta": MetaParser(),
}


def list_platforms() -> list[PlatformParser]:
    return list(_PLATFORMS.values())


def get_platform(platform_id: str) -> PlatformParser:
    try:
        return _PLATFORMS[platform_id.lower()]
    except KeyError as exc:
        supported = ", ".join(sorted(_PLATFORMS))
        raise DataLensError(f"Unsupported platform. Choose one of: {supported}.") from exc
