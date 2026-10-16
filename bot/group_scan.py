"""Scan a Roblox group's members for alts and NSFW accounts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from .content_filter import NsfwReport
from .db import GuildConfig
from .models import GroupInfo, GroupMember
from .roblox import RobloxError, UserNotFound
from .scanner import ScanResult

if TYPE_CHECKING:
    from .bot import RobloxCheckerBot

# Badge pages are the slowest part of a scan; one page (100) is plenty to judge an account in bulk.
GROUP_SCAN_BADGES = 100
PARALLEL_MEMBERS = 3

Progress = Callable[[GroupInfo, int, int], Awaitable[None]]  # (group, done, total)


@dataclass(slots=True)
class MemberScan:
    member: GroupMember
    result: ScanResult | None = None
    error: str | None = None


@dataclass(slots=True)
class GroupScanReport:
    group: GroupInfo
    config: GuildConfig
    group_nsfw: NsfwReport
    scans: list[MemberScan] = field(default_factory=list)

    def _ok(self) -> list[MemberScan]:
        return [s for s in self.scans if s.result is not None]

    @property
    def nsfw(self) -> list[MemberScan]:
        hits = [s for s in self._ok() if s.result.nsfw.flagged and not s.result.trusted]
        return sorted(hits, key=lambda s: (-s.result.nsfw.score, -s.result.alt.score))

    @property
    def alts(self) -> list[MemberScan]:
        """Likely alts that aren't already listed as NSFW."""
        hits = [
            s for s in self._ok()
            if s.result.alt.score >= self.config.alt_threshold and not s.result.nsfw.flagged and not s.result.trusted
        ]
        return sorted(hits, key=lambda s: -s.result.alt.score)

    @property
    def flagged(self) -> list[MemberScan]:
        return self.nsfw + self.alts

    @property
    def suspicious(self) -> list[MemberScan]:
        flagged = {s.member.user_id for s in self.flagged}
        hits = [
            s for s in self._ok()
            if s.member.user_id not in flagged and not s.result.trusted
            and (s.result.alt.score >= self.config.suspicious_threshold or s.result.nsfw.hits)
        ]
        return sorted(hits, key=lambda s: -(s.result.alt.score + s.result.nsfw.score))

    @property
    def trusted(self) -> list[MemberScan]:
        return [s for s in self._ok() if s.result.trusted]

    @property
    def failed(self) -> list[MemberScan]:
        return [s for s in self.scans if s.result is None]

    def group_record(self, scanned_by: int | None) -> dict:
        """A row for the saved-groups table (see ``Database.save_group``)."""
        group = self.group
        return {
            "group_id": group.id,
            "name": group.name,
            "member_count": group.member_count,
            "owner": group.owner_name,
            "nsfw_score": self.group_nsfw.score,
            "nsfw_matches": ", ".join(hit.label for hit in self.group_nsfw.hits[:8]),
            "members_checked": len(self.scans),
            "nsfw_members": len(self.nsfw),
            "alt_members": len(self.alts),
            "suspicious_members": len(self.suspicious),
            "scanned_by": scanned_by,
        }

    def player_records(self, scanned_by: int | None) -> list[dict]:
        found_via = f"group scan: {self.group.name}"
        return [
            s.result.record(self.config, depth="full", found_via=found_via, checked_by=scanned_by)
            for s in self.scans if s.result is not None
        ]

    @property
    def clean_count(self) -> int:
        noted = {s.member.user_id for s in self.flagged + self.suspicious + self.trusted}
        return sum(1 for s in self._ok() if s.member.user_id not in noted)


async def scan_group(
    bot: "RobloxCheckerBot", guild_id: int, query: str, limit: int, progress: Progress | None = None
) -> GroupScanReport:
    roblox = bot.roblox
    group_id = await roblox.resolve_group_id(query)
    group = await roblox.get_group(group_id)
    members = await roblox.get_group_members(group_id, limit)
    config = await bot.db.get_config(guild_id)
    content_filter = await bot.filters.get(guild_id)
    group_nsfw = content_filter.scan_fields(
        [("display name", group.name), ("description", group.description)], config.nsfw_threshold
    )
    report = GroupScanReport(group=group, config=config, group_nsfw=group_nsfw)

    gate = asyncio.Semaphore(PARALLEL_MEMBERS)
    done = 0
    if progress:
        await progress(group, 0, len(members))

    async def scan_one(member: GroupMember) -> MemberScan:
        nonlocal done
        async with gate:
            try:
                account = await roblox.fetch_account(member.user_id, max_badges=GROUP_SCAN_BADGES)
                scan = MemberScan(member, await bot.scanner.analyse(guild_id, account, config))
            except UserNotFound:
                scan = MemberScan(member, error="account no longer exists")
            except RobloxError as exc:
                scan = MemberScan(member, error=str(exc))
        done += 1
        if progress:
            await progress(group, done, len(members))
        return scan

    report.scans = list(await asyncio.gather(*(scan_one(m) for m in members)))
    return report
