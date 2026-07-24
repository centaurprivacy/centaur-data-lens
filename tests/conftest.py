from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest


def write_zip(path: Path, members: Mapping[str, Any]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            if isinstance(value, bytes):
                archive.writestr(name, value)
            else:
                archive.writestr(name, json.dumps(value))
    return path


@pytest.fixture
def google_export(tmp_path: Path) -> Path:
    return write_zip(
        tmp_path / "google.zip",
        {
            "Takeout/My Activity/Search/MyActivity.json": [
                {
                    "header": "Search",
                    "title": "Searched for privacy tools",
                    "titleUrl": "https://shared.example/search?q=privacy",
                    "time": "2025-01-02T03:04:05Z",
                    "products": ["Search"],
                },
                {
                    "header": "Maps",
                    "title": "Viewed a map",
                    "titleUrl": "https://maps.example/place",
                    "time": "2025-02-02T03:04:05Z",
                },
            ],
            "Takeout/Chrome/BrowserHistory.json": {
                "Browser History": [
                    {
                        "title": "Example",
                        "url": "https://example.org/page",
                        "time_usec": 1_735_786_800_000_000,
                    }
                ]
            },
            "Takeout/YouTube and YouTube Music/history/watch-history.json": [
                {
                    "header": "YouTube",
                    "title": "Watched a synthetic video",
                    "titleUrl": "https://youtube.example/watch?v=fake",
                    "time": "2025-03-01T00:00:00Z",
                }
            ],
            "Takeout/Google Play Store/Installs.json": {
                "installs": [
                    {
                        "title": "Installed Synthetic App",
                        "timestamp": "2025-04-01T00:00:00Z",
                        "device_name": "Synthetic Phone",
                    }
                ]
            },
            "Takeout/Google Photos/photo.jpg": b"not-read",
        },
    )


@pytest.fixture
def meta_export(tmp_path: Path) -> Path:
    return write_zip(
        tmp_path / "meta.zip",
        {
            "your_facebook_activity/search/your_search_history.json": {
                "searches_v2": [
                    {
                        "title": "Searched Meta for privacy",
                        "timestamp": 1_735_786_800,
                        "url": "https://shared.example/meta",
                    }
                ]
            },
            "ads_information/ad_interests.json": {
                "topics": [
                    {
                        "name": "Open source software",
                        "timestamp": 1_735_786_900,
                    }
                ]
            },
            "apps_and_websites_off_of_facebook/your_activity_off_meta_technologies.json": [
                {
                    "name": "shared.example",
                    "timestamp": 1_735_787_000,
                    "url": "https://shared.example/purchase",
                }
            ],
            "security_and_login_information/devices.json": {
                "devices": [
                    {
                        "name": "Synthetic Browser",
                        "timestamp": 1_735_787_100,
                        "device_name": "Synthetic Phone",
                        "user_agent": "Synthetic/1.0",
                    }
                ]
            },
            "messages/inbox/private/message_1.json": {
                "messages": [{"content": "must never be parsed", "timestamp": 1_735_787_200}]
            },
        },
    )
