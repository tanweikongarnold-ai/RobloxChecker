"""/case: the shared review queue."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from .. import embeds
from ..models import utcnow
from ..permissions import moderator_only

if TYPE_CHECKING:
    from ..bot import RobloxCheckerBot


class Cases(commands.Cog):
    case = app_commands.Group(name="case", description="Shared review queue for flagged accounts", guild_only=True)

    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot

    # ------------------------------------------------------------ autocomplete

    async def _case_choices(self, interaction: discord.Interaction, current: str, open_only: bool):
        if interaction.guild_id is None:
            return []
        cases = await self.bot.db.search_case_ids(interaction.guild_id, current.lstrip("#"), open_only=open_only)
        choices = []
        for case in cases:
            state = case.status
            if case.is_open:
                state = "claimed" if case.claimed_by else "unclaimed"
            reasons = "/".join(case.reasons)
            choices.append(
                app_commands.Choice(name=embeds.clip(f"#{case.id} · {case.roblox_name} · {reasons} · {state}", 100), value=case.id)
            )
        return choices

    async def any_case(self, interaction: discord.Interaction, current: str):
        return await self._case_choices(interaction, current, open_only=False)

    async def open_case(self, interaction: discord.Interaction, current: str):
        return await self._case_choices(interaction, current, open_only=True)

    # ---------------------------------------------------------------- helpers

    async def _reply_with_case(self, interaction: discord.Interaction, case_id: int, text: str | None = None) -> None:
        assert interaction.guild is not None
        case = await self.bot.db.get_case(interaction.guild.id, case_id)
        if case is None:
            await interaction.followup.send(text or f"There's no case #{case_id}.", ephemeral=True)
            return
        embed, view = await self.bot.cases.render(case)
        await interaction.followup.send(content=text, embed=embed, view=view, ephemeral=True)

    async def _run(self, interaction: discord.Interaction, action: str, case_id: int, done: str) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.cases.act(interaction.guild, interaction.user, action, case_id)
        if result.case is None:
            await interaction.followup.send(result.error or "Case not found.", ephemeral=True)
            return
        await self._reply_with_case(interaction, case_id, result.error or done)

    # --------------------------------------------------------------- commands

    @case.command(name="view", description="Show a case with its notes and buttons.")
    @app_commands.describe(case_id="Case number")
    @app_commands.autocomplete(case_id=any_case)
    @moderator_only()
    async def view(self, interaction: discord.Interaction, case_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._reply_with_case(interaction, case_id)

    @case.command(name="list", description="List cases in the review queue.")
    @app_commands.describe(status="Which cases to show", only="Narrow the list down", page="Page number")
    @moderator_only()
    async def list_cases(
        self,
        interaction: discord.Interaction,
        status: Literal["open", "confirmed", "dismissed", "all"] = "open",
        only: Literal["everything", "unclaimed", "mine"] = "everything",
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        assert interaction.guild is not None
        per_page = 15
        cases = await self.bot.db.list_cases(
            interaction.guild.id,
            status=None if status == "all" else status,
            claimed_by=interaction.user.id if only == "mine" else None,
            unclaimed=only == "unclaimed",
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        counts = await self.bot.db.case_counts(interaction.guild.id)
        title = f"{status.title()} cases" + ("" if only == "everything" else f" · {only}") + f" · page {page}"
        await interaction.response.send_message(
            embed=embeds.case_list_embed(title, cases, counts),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @case.command(name="claim", description="Claim a case so other moderators know you're on it.")
    @app_commands.autocomplete(case_id=open_case)
    @moderator_only()
    async def claim(self, interaction: discord.Interaction, case_id: int) -> None:
        await self._run(interaction, "claim", case_id, f"👀 You claimed case #{case_id}.")

    @case.command(name="release", description="Give up your claim on a case (admins can release anyone's).")
    @app_commands.autocomplete(case_id=open_case)
    @moderator_only()
    async def release(self, interaction: discord.Interaction, case_id: int) -> None:
        await self._run(interaction, "release", case_id, f"↩️ Released case #{case_id}.")

    @case.command(name="resolve", description="Close a case as confirmed (it really is an alt / NSFW) or dismissed.")
    @app_commands.describe(outcome="confirmed = the flag was right, dismissed = false alarm", note="Optional note for the team")
    @app_commands.autocomplete(case_id=open_case)
    @moderator_only()
    async def resolve(
        self,
        interaction: discord.Interaction,
        case_id: int,
        outcome: Literal["confirmed", "dismissed"],
        note: str | None = None,
    ) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        action = "confirm" if outcome == "confirmed" else "dismiss"
        result = await self.bot.cases.act(interaction.guild, interaction.user, action, case_id, refresh=not note)
        if result.ok and note:
            await self.bot.cases.add_note(interaction.guild, interaction.user, case_id, f"[{outcome}] {note}")
        if result.case is None:
            await interaction.followup.send(result.error or "Case not found.", ephemeral=True)
            return
        await self._reply_with_case(interaction, case_id, result.error or f"Case #{case_id} {outcome}.")

    @case.command(name="reopen", description="Reopen a closed case.")
    @app_commands.autocomplete(case_id=any_case)
    @moderator_only()
    async def reopen(self, interaction: discord.Interaction, case_id: int) -> None:
        await self._run(interaction, "reopen", case_id, f"🔄 Case #{case_id} reopened.")

    @case.command(name="note", description="Add a note to a case for the rest of the team.")
    @app_commands.autocomplete(case_id=any_case)
    @moderator_only()
    async def note(
        self, interaction: discord.Interaction, case_id: int, text: app_commands.Range[str, 1, 1000]
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.cases.add_note(interaction.guild, interaction.user, case_id, text)
        if not result.ok:
            await interaction.followup.send(result.error, ephemeral=True)
            return
        await self._reply_with_case(interaction, case_id, f"📝 Note added to case #{case_id}.")

    @case.command(name="open", description="Manually open a case for a Roblox player.")
    @app_commands.describe(user="Roblox username, user ID or profile link", reason="Why the team should look at this")
    @moderator_only()
    async def open_case_cmd(
        self, interaction: discord.Interaction, user: str, reason: app_commands.Range[str, 1, 500]
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.scanner.scan(interaction.guild.id, user)
        case, created = await self.bot.cases.open_from_scan(
            interaction.guild, result, opened_by=interaction.user.id, manual_reason=reason
        )
        if case and not created:
            await self.bot.db.add_note(case.id, interaction.user.id, reason)
            await self.bot.cases.publish(interaction.guild, case)
        await interaction.followup.send(self.bot.cases.case_opened_text(interaction.guild, case, created), ephemeral=True)

    @case.command(name="history", description="Every case this server has had for a Roblox player.")
    @app_commands.describe(user="Roblox username, user ID or profile link")
    @moderator_only()
    async def history(self, interaction: discord.Interaction, user: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self.bot.roblox.resolve_user_id(user)
        cases = await self.bot.db.list_cases(interaction.guild.id, status=None, roblox_id=roblox_id, limit=25)
        await interaction.followup.send(
            embed=embeds.case_list_embed(f"Case history · {user}", cases),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @case.command(name="stats", description="Queue size and who has been handling cases.")
    @app_commands.describe(days="Look back this many days")
    @moderator_only()
    async def stats(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30) -> None:
        assert interaction.guild is not None
        counts = await self.bot.db.case_counts(interaction.guild.id)
        rows = await self.bot.db.moderator_stats(interaction.guild.id, since=utcnow() - timedelta(days=days))
        embed = discord.Embed(
            title="Moderation stats",
            colour=embeds.BLUE,
            description=(
                f"**{counts['open']}** open (**{counts['unclaimed']}** unclaimed)\n"
                f"**{counts['confirmed']}** confirmed · **{counts['dismissed']}** dismissed (all time)"
            ),
        )
        lines = [
            f"{embeds.mention(uid)}: **{resolved}** resolved · {claims} claimed now · {notes} notes"
            for uid, resolved, claims, notes in rows
        ]
        embed.add_field(name=f"Moderators · last {days} days", value="\n".join(lines) or "No activity yet.", inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: "RobloxCheckerBot") -> None:
    await bot.add_cog(Cases(bot))
