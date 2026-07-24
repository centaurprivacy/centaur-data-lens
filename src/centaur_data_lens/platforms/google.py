from __future__ import annotations

from centaur_data_lens.platforms.base import PlatformDefinition, PlatformParser


class GoogleParser(PlatformParser):
    definition = PlatformDefinition(
        platform_id="google",
        display_name="Google",
        last_verified="2026-07-24",
        official_url="https://takeout.google.com/",
        supported=(
            "My Activity JSON",
            "Chrome browser history",
            "YouTube and YouTube Music history",
            "Google Play installation history",
        ),
        excluded=(
            "Gmail",
            "Drive",
            "Keep",
            "Photos and media",
            "Unsupported location formats",
        ),
        guide=(
            "Open Google Takeout and choose Deselect all.",
            "Select My Activity, Chrome, YouTube and YouTube Music, and Google Play Store.",
            "Choose JSON wherever the product offers a format choice.",
            "Exclude uploaded videos, music, Photos, Gmail, Drive, and Keep for this release.",
            "Create the export, download every ZIP part, and keep the files protected.",
        ),
    )

    def supported_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return (
            ("my activity/" in normalized and normalized.endswith(".json"))
            or normalized.endswith("browserhistory.json")
            or (
                ("youtube" in normalized or "youtube and youtube music" in normalized)
                and "history" in normalized
                and normalized.endswith(".json")
            )
            or (
                "google play" in normalized
                and "install" in normalized
                and normalized.endswith(".json")
            )
        )

    def category_for(self, path: str) -> str:
        normalized = path.lower()
        if "browserhistory" in normalized:
            return "browser_history"
        if "youtube" in normalized:
            return "youtube_history"
        if "google play" in normalized:
            return "app_installs"
        return "account_activity"
