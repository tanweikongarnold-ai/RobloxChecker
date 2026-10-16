"""/groupscan: check a Roblox group's members for alts and NSFW accounts."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..embeds import group_scan_embed
from ..group_scan import GroupScanReport, scan_group
from ..models import GroupInfo
from ..permissions import moderator_only
from ..views import _moderator_gate

if TYPE_CHECKING:
    from ..bot import RobloxCheckerBot

log = logging.getLogger(__name__)


class GroupScanView(discord.ui.View):
    """Buttons under a scan result. Lives as long as Discord lets the bot edit the reply (~15 min)."""

    def __init__(self, bot: "RobloxCheckerBot", report: GroupScanReport, interaction: discord.Interaction):
        super().__init__(timeout=840)
        self.bot = bot
        self.report = report
        self.source = interaction
        self.open_button: discord.ui.Button | None = None
        flagged = len(report.flagged)
        if flagged:
            self.open_button = discord.ui.Button(
                style=discord.ButtonStyle.danger, emoji="📂", label=f"Open cases for {flagged} flagged"
            )
            self.open_button.callback = self.open_cases
            self.add_item(self.open_button)
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Group on Roblox", url=report.group.url))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _moderator_gate(interaction)

    async def open_cases(self, interaction: discord.Interaction) -> None:
        assert self.open_button is not None and interaction.guild is not None
        self.open_button.disabled = True
        self.open_button.label = "Opening cases…"
        await interaction.response.edit_message(view=self)

        group = self.report.group
        note = f"Found by a scan of the Roblox group “{group.name}” ({group.url})."
        open_cases: dict[int, int] = {}
        created = existing = 0
        for scan in self.report.flagged:
            case, was_created = await self.bot.cases.open_from_scan(
                interaction.guild, scan.result, opened_by=interaction.user.id, note=note, announce=False
            )
            if case:
                open_cases[scan.member.user_id] = case.id
                created += was_created
                existing += not was_created
        await self.bot.cases.audit(
            interaction.guild, interaction.user.id, "groupscan.cases", group.name,
            f"{created} new case(s), {existing} already open",
        )

        self.open_button.label = f"Opened {created} new case(s)"
        self.open_button.style = discord.ButtonStyle.secondary
        await interaction.edit_original_response(embed=group_scan_embed(self.report, open_cases), view=self)
        extra = f" {existing} already had an open case." if existing else ""
        await interaction.followup.send(
            f"📂 Opened {created} new case(s) in the review channel.{extra}", ephemeral=True
        )

    async def on_timeout(self) -> None:
        if self.open_button and not self.open_button.disabled:
            self.open_button.disabled = True
            self.open_button.label = "Expired. Run /groupscan again"
            try:
                await self.source.edit_original_response(view=self)
            except discord.HTTPException:
                pass


class Groups(commands.Cog):
    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot
        # One scan at a time across all servers: a scan makes hundreds of Roblox requests.
        self._lock = asyncio.Lock()

    @app_commands.command(name="groupscan", description="Check a Roblox group's newest members for alts and NSFW accounts.")
    @app_commands.describe(
        group="Roblox group link, group ID or exact group name",
        members="How many of the newest members to check (5-200, default 50)",
        public="Post the result in this channel for everyone (default: only you can see it)",
        thorough="Full check on every member: slower, but also catches NSFW past usernames and groups",
    )
    @app_commands.guild_only()
    @moderator_only()
    async def groupscan(
        self,
        interaction: discord.Interaction,
        group: str,
        members: app_commands.Range[int, 5, 200] = 50,
        public: bool = False,
        thorough: bool = False,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=not public, thinking=True)

        async def say(text: str) -> None:
            try:
                await interaction.edit_original_response(content=text)
            except discord.HTTPException:
                pass

        if self._lock.locked():
            await say("⏳ Another group scan is running. Yours starts as soon as it finishes…")
        async with self._lock:
            last_update = 0.0

            async def progress(info: GroupInfo, stage: str, done: int, total: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if done in (0, total) or stage == "break" or now - last_update >= 2:
                    last_update = now
                    name = discord.utils.escape_markdown(info.name)
                    if stage == "quick":
                        await say(f"🔎 **{name}**: quick check on every member… {done}/{total}")
                    elif stage == "break":
                        await say(
                            f"⏸️ **{name}**: quick check done. Taking a {total}-second break so Roblox doesn't "
                            f"slow us down, then the full check… {done}s"
                        )
                    else:
                        await say(f"🔎 **{name}**: full check on the {total} that looked off… {done}/{total}")

            await say("🔎 Looking up the group…")
            report = await scan_group(self.bot, interaction.guild.id, group, members, progress, thorough=thorough)

        await self.bot.db.save_players(interaction.guild.id, report.player_records(interaction.user.id))
        await self.bot.db.save_group(interaction.guild.id, report.group_record(interaction.user.id))
        if not report.scans:
            await say(f"**{discord.utils.escape_markdown(report.group.name)}** has no members to check.")
            return
        open_cases = {c.roblox_id: c.id for c in await self.bot.db.list_cases(interaction.guild.id, status="open", limit=1000)}
        try:
            await interaction.edit_original_response(
                content=None, embed=group_scan_embed(report, open_cases), view=GroupScanView(self.bot, report, interaction)
            )
        except discord.HTTPException as exc:
            log.warning("Couldn't show group scan result: %s", exc)
        await self.bot.cases.audit(
            interaction.guild, interaction.user.id, "groupscan", report.group.name,
            f"{len(report.scans)} members checked, {len(report.flagged)} flagged",
        )


async def setup(bot: "RobloxCheckerBot") -> None:
    await bot.add_cog(Groups(bot))
