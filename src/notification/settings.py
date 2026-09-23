"""Shared delivery switches for every notification transport."""

import os


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def channel_enabled(config: dict, channel: str) -> bool:
    if env_flag("SKIP_NOTIFICATIONS") or env_flag(f"SKIP_{channel.upper()}"):
        return False
    settings = (config.get("notification", {}) or {}).get(channel, {}) or {}
    default = bool(config.get("email")) if channel == "email" else bool(settings)
    return bool(settings.get("enabled", default))
