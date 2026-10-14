"""Plain data containers shared between the Roblox client, analysers and the bot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_FRACTION = re.compile(r"\.(\d+)")


def parse_roblox_time(value: str | None) -> datetime | None:
    """Parse the ISO-8601 timestamps Roblox returns (e.g. ``2006-02-27T21:06:40.3Z``).

    Roblox uses a variable number of fractional digits, which older Pythons'
    ``fromisoformat`` rejects, so the fraction is normalised to 6 digits first.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Badge:
    id: int
    name: str
    awarder_id: int | None = None
    awarded_at: datetime | None = None


@dataclass(slots=True)
class Group:
    id: int
    name: str
    role: str | None = None
    rank: int | None = None
    member_count: int | None = None


@dataclass(slots=True)
class AccountData:
    """Everything fetched about one Roblox account. ``None`` means "could not fetch"."""

    user_id: int
    username: str
    display_name: str
    description: str = ""
    created: datetime | None = None
    is_banned: bool = False
    verified: bool = False

    badges: list[Badge] = field(default_factory=list)
    badges_hidden: bool = False
    badges_truncated: bool = False

    legacy_badges: list[str] = field(default_factory=list)
    friends: int | None = None
    followers: int | None = None
    following: int | None = None
    groups: list[Group] | None = None
    username_history: list[str] = field(default_factory=list)

    avatar_url: str | None = None
    avatar_state: str | None = None
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def profile_url(self) -> str:
        return f"https://www.roblox.com/users/{self.user_id}/profile"

    def age_days(self, now: datetime | None = None) -> int | None:
        if self.created is None:
            return None
        return max(0, ((now or utcnow()) - self.created).days)


@dataclass(slots=True)
class GroupInfo:
    id: int
    name: str
    description: str = ""
    member_count: int | None = None
    owner_name: str | None = None
    verified: bool = False

    @property
    def url(self) -> str:
        return f"https://www.roblox.com/communities/{self.id}"


@dataclass(slots=True)
class GroupMember:
    user_id: int
    username: str
    display_name: str
    role: str | None = None
