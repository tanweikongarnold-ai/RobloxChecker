"""/database: see everything the bot has saved for this server."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from .. import embeds
from ..export import build_workbook
from ..permissions import moderator_only

if TYPE_CHECKING:
    from ..bot import RobloxCheckerBot

log = logging.getLogger(__name__)

PER_PAGE = 15
# Discord's upload limit for servers without boosts is 10 MB; stay under it.
MAX_UPLOAD = 9_500_000
USER_ID_COLUMNS = ("last_checked_by", "last_scanned_by", "opened_by", "claimed_by", "resolved_by", "author_id", "added_by", "actor_id")


class Database(commands.Cog):
    database = app_commands.Group(name="database", description="See everything the bot has saved for this server", guild_only=True)

    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot

    @database.command(name="summary", description="How much the bot has saved: players, groups, cases and more.")
    @moderator_only()
    async def summary(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        counts = await self.bot.db.database_counts(interaction.guild.id)
        await interaction.response.send_message(
            embed=embeds.database_summary_embed(interaction.guild.name, counts), ephemeral=True
        )

    @database.command(name="players", description="Browse every Roblox player the bot has checked.")
    @app_commands.describe(
        show="Which players to list",
        search="Part of a username or display name, or a Roblox ID",
        page="Page number (15 per page)",
    )
    @moderator_only()
    async def players(
        self,
        interaction: discord.Interaction,
        show: Literal["all", "flagged", "nsfw", "alts"] = "all",
        search: app_commands.Range[str, 1, 40] | None = None,
        page: app_commands.Range[int, 1, 10000] = 1,
    ) -> None:
        assert interaction.guild is not None
        total = await self.bot.db.count_players(interaction.guild.id, show=show, search=search)
        pages = max(1, -(-total // PER_PAGE))
        page = min(page, pages)
        rows = await self.bot.db.list_players(
            interaction.guild.id, show=show, search=search, limit=PER_PAGE, offset=(page - 1) * PER_PAGE
        )
        await interaction.response.send_message(
            embed=embeds.players_list_embed(rows, show=show, search=search, page=page, pages=pages, total=total),
            ephemeral=True,
        )

    @database.command(name="groups", description="Browse every Roblox group scanned with /groupscan.")
    @app_commands.describe(page="Page number (15 per page)")
    @moderator_only()
    async def groups(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 10000] = 1) -> None:
        assert interaction.guild is not None
        total = (await self.bot.db.database_counts(interaction.guild.id))["groups"]
        pages = max(1, -(-total // PER_PAGE))
        page = min(page, pages)
        rows = await self.bot.db.list_groups(interaction.guild.id, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
        await interaction.response.send_message(
            embed=embeds.groups_list_embed(rows, page=page, pages=pages, total=total), ephemeral=True
        )

    @database.command(name="export", description="Download everything saved for this server as an Excel file.")
    @moderator_only()
    async def export(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)

        tables = await self.bot.db.export_tables(guild.id)
        names = await self._names(guild, tables)
        note = ""
        data = build_workbook(guild.name, tables, names)
        if len(data) > MAX_UPLOAD:
            note = "Too big for one Discord upload, so players seen only as someone's friend (and not flagged) were left out."
            tables = await self.bot.db.export_tables(guild.id, players="important")
            data = build_workbook(guild.name, tables, names, note=note)
        if len(data) > MAX_UPLOAD:
            await interaction.followup.send(
                "The export is bigger than Discord allows (10 MB). Use `/database players` to browse instead, "
                "or open `data/robloxchecker.db` on the bot's computer with DB Browser for SQLite.",
                ephemeral=True,
            )
            return

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        safe_server = "".join(ch for ch in guild.name if ch.isalnum() or ch in " -_").strip().replace(" ", "-")[:40] or "server"
        file = discord.File(BytesIO(data), filename=f"robloxchecker-{safe_server}-{stamp}.xlsx")
        counts = f"{len(tables['players']):,} players, {len(tables['groups']):,} groups, {len(tables['cases']):,} cases"
        await interaction.followup.send(
            f"📊 Here's everything saved for this server ({counts}). Open it in Excel or Google Sheets; each tab is one kind of data."
            + (f"\n⚠️ {note}" if note else ""),
            file=file,
            ephemeral=True,
        )
        await self.bot.db.log(guild.id, interaction.user.id, "export", "database", counts)

    async def _names(self, guild: discord.Guild, tables: dict[str, list]):
        """Turn the Discord user IDs in the tables into names people recognise."""
        ids = {row[col] for rows in tables.values() for row in rows for col in USER_ID_COLUMNS if col in row.keys() and row[col]}
        names: dict[int, str] = {}
        for user_id in list(ids)[:100]:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    member = None
            user = member or self.bot.get_user(user_id)
            if user is not None:
                shown = getattr(user, "display_name", user.name)
                names[user_id] = f"{shown} (@{user.name})" if shown != user.name else f"@{user.name}"

        def name(user_id: int | None) -> str:
            if not user_id:
                return ""
            return names.get(user_id, f"User {user_id}")

        return name


async def setup(bot: "RobloxCheckerBot") -> None:
    await bot.add_cog(Database(bot))
