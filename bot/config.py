"""Settings read from environment variables (or a ``.env`` file)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _int(name: str, default: int | None) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {value!r}") from None


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    dev_guild_id: int | None
    database_path: str
    track_interval_minutes: int
    max_badges: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")
        return cls(
            token=token,
            dev_guild_id=_int("DEV_GUILD_ID", None),
            database_path=os.getenv("DATABASE_PATH", "data/robloxchecker.db"),
            track_interval_minutes=max(10, _int("TRACK_INTERVAL_MINUTES", 360) or 360),
            max_badges=min(5000, max(100, _int("MAX_BADGES", 500) or 500)),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
