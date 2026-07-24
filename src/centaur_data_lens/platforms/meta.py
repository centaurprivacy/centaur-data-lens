from __future__ import annotations

from centaur_data_lens.platforms.base import PlatformDefinition, PlatformParser

_EXCLUDED = (
    "message",
    "contact",
    "your_posts",
    "comments",
    "photos",
    "videos",
    "media",
    "facial",
)


class MetaParser(PlatformParser):
    definition = PlatformDefinition(
        platform_id="meta",
        display_name="Meta (Facebook and Instagram)",
        last_verified="2026-07-24",
        official_url="https://accountscenter.facebook.com/info_and_permissions/",
        supported=(
            "Account and profile metadata",
            "Search and activity history",
            "Advertising interests and activity",
            "Off-Meta activity",
            "Devices, sessions, and login history",
            "Connected applications and websites",
            "Connection counts and date ranges",
        ),
        excluded=(
            "Message bodies",
            "Contacts",
            "Post and comment bodies",
            "Photos, videos, and other media",
            "Facial-recognition assets",
        ),
        guide=(
            "Open Accounts Center, then Your information and permissions.",
            "Choose Export your information and export to your device.",
            "Choose JSON, an appropriate date range, and low media quality.",
            "Include profile, activity, ads, off-Meta activity, security/login, "
            "apps, and connections.",
            "Message bodies, contacts, posts, comments, and media are intentionally excluded.",
        ),
    )

    def supported_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        if not normalized.endswith(".json") or any(item in normalized for item in _EXCLUDED):
            return False
        keywords = (
            "profile",
            "personal_information",
            "account_information",
            "search",
            "activity",
            "advertis",
            "ads_",
            "off_facebook",
            "off_meta",
            "device",
            "login",
            "session",
            "apps_and_websites",
            "connections",
            "friends",
            "followers",
        )
        return any(keyword in normalized for keyword in keywords)

    def category_for(self, path: str) -> str:
        normalized = path.lower()
        if "advertis" in normalized or "ads_" in normalized:
            return "advertising"
        if "off_facebook" in normalized or "off_meta" in normalized:
            return "off_platform_activity"
        if any(item in normalized for item in ("device", "login", "session")):
            return "devices_and_sessions"
        if "apps_and_websites" in normalized:
            return "connected_apps"
        if any(item in normalized for item in ("connections", "friends", "followers")):
            return "connections"
        if "search" in normalized:
            return "search_history"
        if any(item in normalized for item in ("profile", "personal_", "account_")):
            return "profile"
        return "account_activity"
