"""Glue between the Roblox client, the analysers and per-server settings."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from . import alt_detector
from .alt_detector import AltReport
from .content_filter import ContentFilter, NsfwReport, TermOverride
from .db import Database, GuildConfig
from .models import AccountData
from .roblox import RobloxClient


@dataclass(slots=True)
class ScanResult:
    account: AccountData
    alt: AltReport
    nsfw: NsfwReport
    trusted_by: int | None = None
    trusted_reason: str | None = None

    @property
    def trusted(self) -> bool:
        return self.trusted_by is not None

    def flag_reasons(self, config: GuildConfig) -> list[str]:
        reasons = []
        if self.alt.score >= config.alt_threshold:
            reasons.append("alt")
        if self.nsfw.flagged:
            reasons.append("nsfw")
        return reasons

    def case_details(self) -> dict[str, Any]:
        """Snapshot stored with a case so moderators see what triggered it."""
        account = self.account
        return {
            "display_name": account.display_name,
            "avatar_url": account.avatar_url,
            "alt_verdict": self.alt.verdict,
            "badge_count": None if account.badges_hidden else len(account.badges),
            "badges_truncated": account.badges_truncated,
            "account_age_days": account.age_days(),
            "is_banned": account.is_banned,
            "signals": [[s.points, s.label] for s in self.alt.signals[:8]],
            "nsfw_hits": [[h.weight, h.label, h.field, h.matched] for h in self.nsfw.hits[:8]],
        }


class FilterRegistry:
    """One :class:`ContentFilter` per server, rebuilt when its custom terms change."""

    def __init__(self, db: Database):
        self.db = db
        self._filters: dict[int, ContentFilter] = {}

    async def get(self, guild_id: int) -> ContentFilter:
        if guild_id not in self._filters:
            rows = await self.db.list_terms(guild_id)
            overrides = [TermOverride(r["term"], r["weight"], r["match"], r["category"]) for r in rows]
            self._filters[guild_id] = ContentFilter.load(overrides)
        return self._filters[guild_id]

    def invalidate(self, guild_id: int) -> None:
        self._filters.pop(guild_id, None)


class Scanner:
    """Fetches accounts with a short cache, sharing in-flight requests.

    If three moderators run ``/check`` on the same player at once, Roblox is
    only queried once and all three get the same data.
    """

    def __init__(self, roblox: RobloxClient, db: Database, filters: FilterRegistry, cache_ttl: float = 120.0):
        self.roblox = roblox
        self.db = db
        self.filters = filters
        self.cache_ttl = cache_ttl
        self._cache: dict[int, tuple[float, AccountData]] = {}
        self._inflight: dict[int, asyncio.Task[AccountData]] = {}

    async def fetch(self, user_id: int, *, fresh: bool = False) -> AccountData:
        cached = self._cache.get(user_id)
        if cached and not fresh and cached[0] > time.monotonic():
            return cached[1]
        task = self._inflight.get(user_id)
        if task is None:
            task = asyncio.create_task(self.roblox.fetch_account(user_id))
            self._inflight[user_id] = task
            task.add_done_callback(lambda _: self._inflight.pop(user_id, None))
        account = await asyncio.shield(task)
        self._cache[user_id] = (time.monotonic() + self.cache_ttl, account)
        return account

    async def analyse(self, guild_id: int, account: AccountData, config: GuildConfig | None = None) -> ScanResult:
        config = config or await self.db.get_config(guild_id)
        alt = alt_detector.analyse(
            account,
            alt_threshold=config.alt_threshold,
            suspicious_threshold=config.suspicious_threshold,
        )
        content_filter = await self.filters.get(guild_id)
        nsfw = content_filter.scan_account(account, threshold=config.nsfw_threshold)
        result = ScanResult(account=account, alt=alt, nsfw=nsfw)
        trusted = await self.db.get_trusted(guild_id, account.user_id)
        if trusted:
            result.trusted_by, result.trusted_reason = trusted["added_by"], trusted["reason"]
        return result

    async def scan(self, guild_id: int, query: str | int, *, fresh: bool = False) -> ScanResult:
        user_id = query if isinstance(query, int) else await self.roblox.resolve_user_id(query)
        account = await self.fetch(user_id, fresh=fresh)
        return await self.analyse(guild_id, account)
