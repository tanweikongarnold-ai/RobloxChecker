"""Quick checks on everyone a player is friends with (page 2 of /check)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import alt_detector
from .db import GuildConfig
from .models import AccountData
from .roblox import RobloxClient, RobloxError, UserNotFound
from .scanner import ScanResult

if TYPE_CHECKING:
    from .bot import RobloxCheckerBot

log = logging.getLogger(__name__)

FRIEND_CHECK_LIMIT = 200  # friends beyond this are listed by name only
PARALLEL_FRIENDS = 4
CACHE_SECONDS = 600
NEW_ACCOUNT_DAYS = 30


@dataclass(slots=True)
class FriendsData:
    """What Roblox told us, before any server's settings are applied."""

    user_id: int
    total: int
    accounts: list[AccountData] = field(default_factory=list)
    failed: int = 0
    unchecked: list[tuple[int, str, str]] = field(default_factory=list)  # (id, username, display name)


@dataclass(slots=True)
class FriendCheck:
    result: ScanResult
    kind: str  # nsfw | alt | suspicious | new | ok | trusted
    open_case: int | None = None

    @property
    def account(self) -> AccountData:
        return self.result.account


@dataclass(slots=True)
class FriendsReport:
    data: FriendsData
    config: GuildConfig
    checks: list[FriendCheck] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.checks if c.kind == kind)

    @property
    def flagged(self) -> list[FriendCheck]:
        return [c for c in self.checks if c.kind in ("nsfw", "alt")]

    @property
    def alt_share(self) -> float:
        judged = [c for c in self.checks if c.kind != "trusted"]
        return sum(1 for c in judged if c.result.alt.score >= self.config.alt_threshold) / len(judged) if judged else 0.0


class FriendChecker:
    """Fetches and quick-checks a player's friends, sharing work between moderators."""

    def __init__(self, roblox: RobloxClient):
        self.roblox = roblox
        self._cache: dict[int, tuple[float, FriendsData]] = {}
        self._inflight: dict[int, asyncio.Task[FriendsData]] = {}
        self._progress: dict[int, tuple[int, int]] = {}

    def progress(self, user_id: int) -> tuple[int, int] | None:
        """``(done, total)`` while a check is running, else ``None``."""
        return self._progress.get(user_id)

    async def fetch(self, user_id: int) -> FriendsData:
        cached = self._cache.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        task = self._inflight.get(user_id)
        if task is None:
            task = asyncio.create_task(self._fetch(user_id))
            self._inflight[user_id] = task
            task.add_done_callback(lambda _: self._inflight.pop(user_id, None))
        data = await asyncio.shield(task)
        self._cache[user_id] = (time.monotonic() + CACHE_SECONDS, data)
        return data

    async def _fetch(self, user_id: int) -> FriendsData:
        try:
            ids = await self.roblox.get_friend_ids(user_id)
            data = FriendsData(user_id=user_id, total=len(ids))
            to_check, rest = ids[:FRIEND_CHECK_LIMIT], ids[FRIEND_CHECK_LIMIT:]
            self._progress[user_id] = (0, len(to_check))
            gate = asyncio.Semaphore(PARALLEL_FRIENDS)
            done = 0

            async def check(friend_id: int) -> AccountData | None:
                nonlocal done
                async with gate:
                    try:
                        account = await self.roblox.fetch_brief(friend_id)
                    except UserNotFound:
                        account = None  # deleted since they became friends
                    except RobloxError as exc:
                        log.info("Quick check of friend %s failed: %s", friend_id, exc)
                        account = None
                done += 1
                self._progress[user_id] = (done, len(to_check))
                return account

            results = await asyncio.gather(*(check(fid) for fid in to_check))
            data.accounts = [a for a in results if a is not None]
            data.failed = sum(1 for a in results if a is None)
            if rest:
                try:
                    names = await self.roblox.get_users_basic(rest)
                except RobloxError:
                    names = {}
                data.unchecked = [(fid, *names.get(fid, (str(fid), ""))) for fid in rest]
            return data
        finally:
            self._progress.pop(user_id, None)


def _kind(result: ScanResult, config: GuildConfig) -> str:
    if result.trusted:
        return "trusted"
    if result.nsfw.flagged:
        return "nsfw"
    if result.alt.score >= config.alt_threshold:
        return "alt"
    if result.alt.score >= config.suspicious_threshold or result.nsfw.hits:
        return "suspicious"
    age = result.account.age_days()
    if age is not None and age < NEW_ACCOUNT_DAYS:
        return "new"
    return "ok"


KIND_ORDER = {"nsfw": 0, "alt": 1, "suspicious": 2, "new": 3, "ok": 4, "trusted": 5}


async def analyse_friends(bot: "RobloxCheckerBot", guild_id: int, data: FriendsData) -> FriendsReport:
    """Score every friend with this server's thresholds, filter words and trusted list."""
    config = await bot.db.get_config(guild_id)
    content_filter = await bot.filters.get(guild_id)
    trusted = {row["roblox_id"]: (row["added_by"], row["reason"]) for row in await bot.db.list_trusted(guild_id, limit=10000)}
    open_cases = {c.roblox_id: c.id for c in await bot.db.list_cases(guild_id, status="open", limit=1000)}

    report = FriendsReport(data=data, config=config)
    for account in data.accounts:
        result = ScanResult(
            account=account,
            alt=alt_detector.analyse(
                account, alt_threshold=config.alt_threshold, suspicious_threshold=config.suspicious_threshold
            ),
            nsfw=content_filter.scan_account(account, threshold=config.nsfw_threshold),
        )
        if account.user_id in trusted:
            result.trusted_by, result.trusted_reason = trusted[account.user_id]
        report.checks.append(FriendCheck(result, _kind(result, config), open_cases.get(account.user_id)))
    report.checks.sort(
        key=lambda c: (KIND_ORDER[c.kind], -max(c.result.nsfw.score, c.result.alt.score), c.account.username.lower())
    )
    return report
