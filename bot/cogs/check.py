"""/check, /badges and /audit."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import embeds
from ..db import parse_ts
from ..permissions import moderator_only
from ..views import profile_view

if TYPE_CHECKING:
    from ..bot import RobloxCheckerBot


class Check(commands.Cog):
    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot

    @app_commands.command(name="check", description="Scan a Roblox player for alt-account and NSFW signals.")
    @app_commands.describe(
        user="Roblox username, user ID or profile link",
        public="Post the result in this channel for everyone (default: only you can see it)",
    )
    @app_commands.guild_only()
    @moderator_only()
    async def check(self, interaction: discord.Interaction, user: str, public: bool = False) -> None:
        await interaction.response.defer(ephemeral=not public, thinking=True)
        guild = interaction.guild
        assert guild is not None
        result = await self.bot.scanner.scan(guild.id, user)
        account = result.account
        config = await self.bot.db.get_config(guild.id)

        notice = None
        if config.auto_flag and result.flag_reasons(config) and not result.trusted:
            case, created = await self.bot.cases.open_from_scan(guild, result, opened_by=interaction.user.id)
            notice = self.bot.cases.case_opened_text(guild, case, created)
        else:
            case = await self.bot.db.find_open_case(guild.id, account.user_id)

        watching = await self.bot.db.get_watch(guild.id, account.user_id) is not None
        view = profile_view(
            account.user_id,
            show_case=case is None,
            show_track=not watching,
            show_trust=not result.trusted,
        )
        await interaction.followup.send(
            content=notice,
            embed=embeds.scan_embed(result, open_case=case),
            view=view,
            ephemeral=not public,
        )
        await self.bot.db.log(
            guild.id,
            interaction.user.id,
            "check",
            account.username,
            f"alt {result.alt.score}, nsfw {result.nsfw.score}",
        )

    @app_commands.command(name="badges", description="List a Roblox player's badges with the dates they were earned.")
    @app_commands.describe(user="Roblox username, user ID or profile link", page="Page number (15 badges per page)")
    @app_commands.guild_only()
    @moderator_only()
    async def badges(
        self, interaction: discord.Interaction, user: str, page: app_commands.Range[int, 1, 100] = 1
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = await self.bot.roblox.resolve_user_id(user)
        account = await self.bot.scanner.fetch(user_id)
        await interaction.followup.send(embed=embeds.badges_embed(account, page), ephemeral=True)

    @app_commands.command(name="audit", description="Show what moderators have done recently.")
    @app_commands.describe(moderator="Only show actions by this moderator", limit="How many entries (max 25)")
    @app_commands.guild_only()
    @moderator_only()
    async def audit(
        self,
        interaction: discord.Interaction,
        moderator: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        assert interaction.guild is not None
        rows = await self.bot.db.recent_audit(
            interaction.guild.id, limit=limit, actor_id=moderator.id if moderator else None
        )
        lines = []
        for row in rows:
            when = embeds.ts(parse_ts(row["created_at"]))
            actor = embeds.mention(row["actor_id"], "🤖 auto")
            target = discord.utils.escape_markdown(row["target"] or "")
            details = f": {embeds.clip(row['details'], 80)}" if row["details"] else ""
            lines.append(f"{when} {actor} `{row['action']}` {target}{details}")
        embed = discord.Embed(
            title="Audit log" + (f" · {moderator.display_name}" if moderator else ""),
            description=embeds.clip("\n".join(lines) or "No actions recorded yet.", 4000),
            colour=embeds.BLUE,
        )
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: "RobloxCheckerBot") -> None:
    await bot.add_cog(Check(bot))
