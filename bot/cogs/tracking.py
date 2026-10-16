"""/track: a shared watchlist whose badges are rescanned in the background."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import embeds
from ..db import WatchEntry, parse_ts
from ..models import utcnow
from ..permissions import moderator_only
from ..roblox import RobloxError, UserNotFound

if TYPE_CHECKING:
    from ..bot import RobloxCheckerBot

log = logging.getLogger(__name__)

HIDDEN = -1  # stored as last_badge_count while a player's badges are private
PER_TICK = 10


class Tracking(commands.Cog):
    track = app_commands.Group(name="track", description="Watch players and get told when their badges change", guild_only=True)

    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.tracker.start()

    async def cog_unload(self) -> None:
        self.tracker.cancel()

    # ------------------------------------------------------------- background

    @tasks.loop(minutes=2)
    async def tracker(self) -> None:
        interval = timedelta(minutes=self.bot.settings.track_interval_minutes)
        due = await self.bot.db.due_watches(utcnow() - interval, PER_TICK, (g.id for g in self.bot.guilds))
        for entry in due:
            guild = self.bot.get_guild(entry.guild_id)
            if guild is None:
                continue
            try:
                await self.rescan(guild, entry)
            except UserNotFound:
                await self._post(guild, f"⚠️ Tracked player `{entry.roblox_name}` ({entry.roblox_id}) no longer exists.")
                await self.bot.db.remove_watch(guild.id, entry.roblox_id)
            except RobloxError as exc:
                log.warning("Tracking scan of %s failed: %s", entry.roblox_id, exc)
                break  # Roblox is struggling; try again next tick
            except Exception:
                log.exception("Tracking scan of %s crashed", entry.roblox_id)
            await asyncio.sleep(1)

    @tracker.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _post(self, guild: discord.Guild, content: str | None = None, embed: discord.Embed | None = None) -> None:
        config = await self.bot.db.get_config(guild.id)
        channel_id = config.tracking_channel_id or config.review_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                log.warning("Could not post tracking update in %s: %s", guild.id, exc)

    async def rescan(self, guild: discord.Guild, entry: WatchEntry, *, announce: bool = True) -> list[str]:
        """Rescan one watched player, record a snapshot and announce what changed."""
        async with self._lock:  # don't let a manual /track now race the background loop
            account = await self.bot.scanner.fetch(entry.roblox_id, fresh=True)
            result = await self.bot.scanner.analyse(guild.id, account)
            config = await self.bot.db.get_config(guild.id)
            await self.bot.db.save_players(
                guild.id, [result.record(config, depth="full", found_via="tracking", checked_by=None)]
            )
            first_scan = entry.last_checked_at is None
            was_hidden = entry.last_badge_count == HIDDEN

            new_badges = []
            if not account.badges_hidden:
                known = await self.bot.db.known_badges(guild.id, entry.roblox_id)
                if not first_scan and not was_hidden:
                    new_badges = [b for b in account.badges if b.id not in known]
                await self.bot.db.add_known_badges(guild.id, entry.roblox_id, [b.id for b in account.badges])

            notes: list[str] = []
            if not first_scan:
                if account.username.lower() != entry.roblox_name.lower():
                    notes.append(f"✏️ Username changed: **{entry.roblox_name}** → **{account.username}**")
                if was_hidden and not account.badges_hidden:
                    notes.append("🔓 Badges are now public")
                elif account.badges_hidden and not was_hidden:
                    notes.append("🔒 Badges were made private")
                if entry.last_alt_score is not None and abs(result.alt.score - entry.last_alt_score) >= 10:
                    notes.append(f"🕵️ Alt score {entry.last_alt_score} → **{result.alt.score}** ({result.alt.verdict})")
                if entry.last_nsfw_score is not None and result.nsfw.score > entry.last_nsfw_score:
                    notes.append(f"🔞 NSFW score rose {entry.last_nsfw_score} → **{result.nsfw.score}**")

            await self.bot.db.record_watch_scan(
                guild.id,
                entry.roblox_id,
                account.username,
                HIDDEN if account.badges_hidden else len(account.badges),
                len(new_badges),
                result.alt.score,
                result.nsfw.score,
            )

        config = await self.bot.db.get_config(guild.id)
        if config.auto_flag and result.flag_reasons(config) and not result.trusted:
            case, created = await self.bot.cases.open_from_scan(guild, result, opened_by=None)
            if created and case:
                notes.append(f"📂 Auto-opened **case #{case.id}**")

        if announce and (new_badges or notes):
            embed = embeds.watch_update_embed(entry, result, [b.name for b in new_badges], len(new_badges), notes)
            await self._post(guild, embed=embed)
        return notes

    async def start_tracking(
        self, guild: discord.Guild, user: discord.abc.User, roblox_id: int, note: str | None
    ) -> str:
        try:
            account = await self.bot.scanner.fetch(roblox_id)
        except RobloxError as exc:
            return f"⚠️ {exc}"
        name = discord.utils.escape_markdown(account.username)
        added = await self.bot.db.add_watch(guild.id, roblox_id, account.username, user.id, note)
        if not added:
            return f"📈 **{name}** is already being tracked."
        await self.bot.cases.audit(guild, user.id, "watch.add", account.username, note or "")
        entry = await self.bot.db.get_watch(guild.id, roblox_id)
        if entry:
            try:
                await self.rescan(guild, entry, announce=False)  # sets the badge baseline
            except RobloxError as exc:
                log.warning("Baseline scan for %s failed: %s", roblox_id, exc)
        badges = "private" if account.badges_hidden else str(len(account.badges))
        hours = self.bot.settings.track_interval_minutes / 60
        return (
            f"📈 Now tracking **{name}** (badges: {badges}). "
            f"They'll be rescanned about every {hours:g}h and changes posted to the tracking channel."
        )

    # ---------------------------------------------------------------- commands

    async def watched(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id is None:
            return []
        entries = await self.bot.db.list_watch(interaction.guild_id, limit=200)
        current = current.lower()
        return [
            app_commands.Choice(name=f"{e.roblox_name} ({e.roblox_id})", value=str(e.roblox_id))
            for e in entries
            if current in e.roblox_name.lower() or current in str(e.roblox_id)
        ][:25]

    async def _resolve(self, interaction: discord.Interaction, user: str) -> int:
        assert interaction.guild_id is not None
        if user.isdigit() and await self.bot.db.get_watch(interaction.guild_id, int(user)):
            return int(user)  # picked from autocomplete
        return await self.bot.roblox.resolve_user_id(user)

    @track.command(name="add", description="Start tracking a player's badges.")
    @app_commands.describe(user="Roblox username, user ID or profile link", note="Why you're watching them")
    @moderator_only()
    async def add(self, interaction: discord.Interaction, user: str, note: app_commands.Range[str, 1, 300] | None = None) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self.bot.roblox.resolve_user_id(user)
        message = await self.start_tracking(interaction.guild, interaction.user, roblox_id, note)
        await interaction.followup.send(message, ephemeral=True)

    @track.command(name="remove", description="Stop tracking a player.")
    @app_commands.autocomplete(user=watched)
    @moderator_only()
    async def remove(self, interaction: discord.Interaction, user: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self._resolve(interaction, user)
        entry = await self.bot.db.get_watch(interaction.guild.id, roblox_id)
        if entry is None or not await self.bot.db.remove_watch(interaction.guild.id, roblox_id):
            await interaction.followup.send("That player isn't being tracked.", ephemeral=True)
            return
        await self.bot.cases.audit(interaction.guild, interaction.user.id, "watch.remove", entry.roblox_name)
        await interaction.followup.send(f"📉 Stopped tracking **{discord.utils.escape_markdown(entry.roblox_name)}**.", ephemeral=True)

    @track.command(name="list", description="Everyone this server is tracking.")
    @moderator_only()
    async def list_watch(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1) -> None:
        assert interaction.guild is not None
        per_page = 20
        entries = await self.bot.db.list_watch(interaction.guild.id, limit=per_page, offset=(page - 1) * per_page)
        total = await self.bot.db.count_watch(interaction.guild.id)
        lines = []
        for e in entries:
            badges = "?" if e.last_badge_count is None else ("🔒" if e.last_badge_count == HIDDEN else e.last_badge_count)
            lines.append(
                f"[{discord.utils.escape_markdown(e.roblox_name)}](https://www.roblox.com/users/{e.roblox_id}/profile)"
                f" · 🏅 {badges} · 🕵️ {e.last_alt_score if e.last_alt_score is not None else '?'}"
                f" · by {embeds.mention(e.added_by)} · checked {embeds.ts(e.last_checked_at)}"
            )
        embed = discord.Embed(
            title=f"Tracked players ({total}) · page {page}",
            description=embeds.clip("\n".join(lines) or "Nobody is being tracked yet. Use `/track add`.", 4000),
            colour=embeds.BLUE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @track.command(name="history", description="Badge count and score history for a tracked player.")
    @app_commands.autocomplete(user=watched)
    @moderator_only()
    async def history(self, interaction: discord.Interaction, user: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self._resolve(interaction, user)
        entry = await self.bot.db.get_watch(interaction.guild.id, roblox_id)
        rows = await self.bot.db.snapshots(interaction.guild.id, roblox_id, limit=15)
        if entry is None or not rows:
            await interaction.followup.send("No tracking history for that player yet.", ephemeral=True)
            return
        lines = []
        for row in rows:
            badges = "🔒 hidden" if row["badge_count"] == HIDDEN else f"🏅 {row['badge_count']}"
            new = f" (+{row['new_badges']})" if row["new_badges"] else ""
            lines.append(
                f"{embeds.ts(parse_ts(row['taken_at']), 'f')} · {badges}{new} · 🕵️ {row['alt_score']} · 🔞 {row['nsfw_score']}"
            )
        embed = discord.Embed(
            title=f"Tracking history · {entry.roblox_name}",
            url=f"https://www.roblox.com/users/{roblox_id}/profile",
            description="\n".join(lines),
            colour=embeds.BLUE,
        )
        if entry.note:
            embed.add_field(name="Note", value=embeds.clip(entry.note, 1024))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @track.command(name="now", description="Rescan a tracked player right now.")
    @app_commands.autocomplete(user=watched)
    @moderator_only()
    async def now(self, interaction: discord.Interaction, user: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self._resolve(interaction, user)
        entry = await self.bot.db.get_watch(interaction.guild.id, roblox_id)
        if entry is None:
            await interaction.followup.send("That player isn't tracked. Use `/track add` first.", ephemeral=True)
            return
        notes = await self.rescan(interaction.guild, entry)
        fresh = await self.bot.db.get_watch(interaction.guild.id, roblox_id)
        summary = "\n".join(notes) or "No changes since the last scan."
        if fresh and fresh.last_badge_count is not None:
            badges = "private" if fresh.last_badge_count == HIDDEN else fresh.last_badge_count
            summary += f"\nBadges: **{badges}** · Alt score **{fresh.last_alt_score}** · NSFW **{fresh.last_nsfw_score}**"
        await interaction.followup.send(summary, ephemeral=True)


async def setup(bot: "RobloxCheckerBot") -> None:
    await bot.add_cog(Tracking(bot))
