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
QUICK_PARALLEL = 6  # step 1: 2 requests per member
# Rest between the quick check and the full check so Roblox's request limit can reset.
# Set to 0 to turn the break off.
BREAK_SECONDS = 20
BREAK_UPDATE_EVERY = 5  # how often the countdown in Discord updates
FULL_PARALLEL = 3  # step 2: ~8 more requests per member
# A quick check doesn't know friends (+10 if none) or groups (+6 if none), so anyone within
# this many points of "suspicious" gets the full check.
CLOSER_LOOK_MARGIN = 16

# (group, stage, done, total). Stage "quick" and "full" count members; "break" counts down seconds left.
Progress = Callable[[GroupInfo, str, int, int], Awaitable[None]]


@dataclass(slots=True)
class MemberScan:
    member: GroupMember
    result: ScanResult | None = None
    error: str | None = None
    depth: str = "quick"  # "full" once it has had the full check


def needs_closer_look(result: ScanResult, config: GuildConfig) -> bool:
    """After a quick check: could the full check flag this account?"""
    return bool(result.nsfw.hits) or result.alt.score >= max(0, config.suspicious_threshold - CLOSER_LOOK_MARGIN)


@dataclass(slots=True)
class GroupScanReport:
    group: GroupInfo
    config: GuildConfig
    group_nsfw: NsfwReport
    scans: list[MemberScan] = field(default_factory=list)
    thorough: bool = False

    @property
    def full_checks(self) -> int:
        return sum(1 for s in self.scans if s.depth == "full")

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
            s.result.record(self.config, depth=s.depth, found_via=found_via, checked_by=scanned_by)
            for s in self.scans if s.result is not None
        ]

    @property
    def clean_count(self) -> int:
        noted = {s.member.user_id for s in self.flagged + self.suspicious + self.trusted}
        return sum(1 for s in self._ok() if s.member.user_id not in noted)


async def scan_group(
    bot: "RobloxCheckerBot",
    guild_id: int,
    query: str,
    limit: int,
    progress: Progress | None = None,
    *,
    thorough: bool = False,
) -> GroupScanReport:
    """Two steps: a quick check on every member, then the full check only where it could matter.

    ``thorough`` gives everyone the full check (slower; also catches NSFW past
    usernames and group names on otherwise clean-looking accounts).
    """
    roblox = bot.roblox
    group_id = await roblox.resolve_group_id(query)
    group = await roblox.get_group(group_id)
    members = await roblox.get_group_members(group_id, limit)
    config = await bot.db.get_config(guild_id)
    content_filter = await bot.filters.get(guild_id)
    group_nsfw = content_filter.scan_fields(
        [("display name", group.name), ("description", group.description)], config.nsfw_threshold
    )
    report = GroupScanReport(group=group, config=config, group_nsfw=group_nsfw, thorough=thorough)

    async def tell(stage: str, done: int, total: int) -> None:
        if progress:
            await progress(group, stage, done, total)

    # Step 1: profile, bio, age and first badge page for everyone; avatars 100 at a time.
    try:
        headshots = await roblox.get_headshots([m.user_id for m in members])
    except RobloxError:
        headshots = {}
    gate = asyncio.Semaphore(QUICK_PARALLEL)
    done = 0
    await tell("quick", 0, len(members))

    async def quick(member: GroupMember) -> MemberScan:
        nonlocal done
        async with gate:
            try:
                account = await roblox.fetch_brief(member.user_id)
                account.avatar_url, account.avatar_state = headshots.get(member.user_id, (None, None))
                scan = MemberScan(member, await bot.scanner.analyse(guild_id, account, config))
            except UserNotFound:
                scan = MemberScan(member, error="account no longer exists")
            except RobloxError as exc:
                scan = MemberScan(member, error=str(exc))
        done += 1
        await tell("quick", done, len(members))
        return scan

    report.scans = list(await asyncio.gather(*(quick(m) for m in members)))

    # Step 2: the full check (friends, groups, past names, badge dates) where it could change the answer.
    closer = [s for s in report.scans if s.result and (thorough or needs_closer_look(s.result, config))]
    if closer and BREAK_SECONDS > 0:
        remaining = BREAK_SECONDS
        while remaining > 0:
            await tell("break", remaining, BREAK_SECONDS)
            step = min(BREAK_UPDATE_EVERY, remaining)
            await asyncio.sleep(step)
            remaining -= step
    gate = asyncio.Semaphore(FULL_PARALLEL)
    done = 0
    if closer:
        await tell("full", 0, len(closer))

    async def full(scan: MemberScan) -> None:
        nonlocal done
        async with gate:
            try:
                account = await roblox.fetch_account(
                    scan.member.user_id, max_badges=GROUP_SCAN_BADGES, headshot=headshots.get(scan.member.user_id)
                )
                scan.result = await bot.scanner.analyse(guild_id, account, config)
                scan.depth = "full"
            except RobloxError:
                pass  # keep the quick result rather than lose the member
        done += 1
        await tell("full", done, len(closer))

    await asyncio.gather(*(full(s) for s in closer))
    return report
