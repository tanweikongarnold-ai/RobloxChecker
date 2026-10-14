"""Settings read from environment variables (or a ``.env`` file)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(".env")
TOKEN_HELP = "https://discord.com/developers/applications  >  your app  >  Bot  >  Reset Token"


def _int(name: str, default: int | None) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {value!r}") from None


def save_env_value(name: str, value: str, path: Path = ENV_FILE) -> None:
    """Set ``name=value`` in the .env file, replacing an existing line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def forget_env_value(name: str, path: Path = ENV_FILE) -> None:
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(l for l in lines if not l.strip().startswith(f"{name}=")) + "\n", encoding="utf-8")
    os.environ.pop(name, None)


def looks_like_token(token: str) -> bool:
    return token.count(".") >= 2 and len(token) >= 50 and " " not in token


def ask_for_token() -> str:
    """First run: ask for the bot token in the window and remember it in .env."""
    print()
    print("RobloxChecker needs your Discord bot token (you only do this once).")
    print(f"Get it here:  {TOKEN_HELP}")
    while True:
        token = input("Paste the token here and press Enter: ").strip().strip('"').strip("'")
        if looks_like_token(token):
            break
        print("That doesn't look like a bot token. It's a long code with two dots in it. Try again.")
    save_env_value("DISCORD_TOKEN", token)
    print("Saved. Starting the bot...\n")
    return token


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
        load_dotenv(ENV_FILE)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            if not sys.stdin or not sys.stdin.isatty():
                raise SystemExit(f"DISCORD_TOKEN is not set. Put your bot token in .env (get it at {TOKEN_HELP}).")
            token = ask_for_token()
        return cls(
            token=token,
            dev_guild_id=_int("DEV_GUILD_ID", None),
            database_path=os.getenv("DATABASE_PATH", "data/robloxchecker.db"),
            track_interval_minutes=max(10, _int("TRACK_INTERVAL_MINUTES", 360) or 360),
            max_badges=min(5000, max(100, _int("MAX_BADGES", 500) or 500)),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
