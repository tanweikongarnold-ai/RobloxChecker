"""Case workflow shared by slash commands, buttons and the background tracker."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from . import embeds
from .db import Case
from .permissions import is_admin
from .roblox import RobloxError
from .scanner import ScanResult
from .views import TextModal, case_view

if TYPE_CHECKING:
    from .bot import RobloxCheckerBot

log = logging.getLogger(__name__)

AUDIT_VERBS = {
    "case.open": "📂 opened",
    "case.update": "🔁 re-flagged",
    "case.claim": "👀 claimed",
    "case.release": "↩️ released",
    "case.confirm": "🔴 confirmed",
    "case.dismiss": "⚪ dismissed",
    "case.reopen": "🔄 reopened",
    "case.note": "📝 added a note to",
    "watch.add": "📈 started tracking",
    "watch.remove": "📉 stopped tracking",
    "trust.add": "🛡️ trusted",
    "trust.remove": "🛡️ removed trust from",
    "keyword.set": "🔤 set filter term",
    "keyword.remove": "🔤 removed filter term",
    "config": "⚙️ changed setting",
    "groupscan": "🔎 scanned the group",
    "groupscan.cases": "📂 opened cases from a scan of",
    "friends.cases": "📂 opened cases for flagged friends of",
}


PAST_TENSE = {
    "claim": "claimed",
    "release": "released",
    "confirm": "confirmed",
    "dismiss": "dismissed",
    "reopen": "reopened",
}


@dataclass(slots=True)
class ActionResult:
    ok: bool
    case: Case | None
    error: str | None = None


def _who(user_id: int | None) -> str:
    return embeds.mention(user_id, "someone")


class CaseService:
    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ audit

    async def audit(
        self, guild: discord.Guild, actor_id: int | None, action: str, target: str, details: str = ""
    ) -> None:
        await self.db.log(guild.id, actor_id, action, target, details)
        config = await self.db.get_config(guild.id)
        channel = guild.get_channel(config.log_channel_id) if config.log_channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        verb = AUDIT_VERBS.get(action, action)
        actor = embeds.mention(actor_id, "🤖 Auto-flag")
        text = f"{actor} {verb} **{discord.utils.escape_markdown(target)}**"
        if details:
            text += f": {embeds.clip(details, 300)}"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Could not post to log channel in %s: %s", guild.id, exc)

    # ---------------------------------------------------------- review queue

    async def render(self, case: Case) -> tuple[discord.Embed, discord.ui.View]:
        notes = await self.db.get_notes(case.id, limit=4)
        return embeds.case_embed(case, notes), case_view(case)

    async def publish(self, guild: discord.Guild, case: Case) -> None:
        """Create or refresh the case's message in the review channel."""
        embed, view = await self.render(case)
        if case.review_channel_id and case.review_message_id:
            channel = guild.get_channel_or_thread(case.review_channel_id)
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.get_partial_message(case.review_message_id).edit(embed=embed, view=view)
                    return
                except discord.NotFound:
                    pass  # message was deleted: post a fresh one below
                except discord.HTTPException as exc:
                    log.warning("Could not edit review message for case %s: %s", case.id, exc)
                    return

        config = await self.db.get_config(guild.id)
        channel = guild.get_channel(config.review_channel_id) if config.review_channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            log.warning("Could not post case %s to review channel: %s", case.id, exc)
            return
        await self.db.set_case_message(case.id, channel.id, message.id)

    async def open_from_scan(
        self,
        guild: discord.Guild,
        result: ScanResult,
        *,
        opened_by: int | None,
        manual_reason: str | None = None,
        note: str | None = None,
        announce: bool = True,
    ) -> tuple[Case | None, bool]:
        """Open (or refresh) a case for a scanned account.

        Automatic flags (``manual_reason is None``) respect thresholds and the
        trusted list; a moderator opening a case by hand always gets one.
        Returns ``(case, created)``; ``case`` is ``None`` if nothing was flagged.
        ``note`` is added to a newly opened case (e.g. where it was found);
        ``announce=False`` skips the per-case line in the mod log (bulk opens log once).
        """
        config = await self.db.get_config(guild.id)
        reasons = result.flag_reasons(config)
        if manual_reason is None:
            if not reasons or result.trusted:
                return None, False
        else:
            reasons.append("manual")
        details = result.case_details()
        if manual_reason:
            details["manual_reason"] = manual_reason
        account = result.account
        case, created = await self.db.open_case(
            guild.id,
            account.user_id,
            account.username,
            reasons,
            result.alt.score,
            result.nsfw.score,
            details,
            opened_by,
        )
        if created and note and opened_by:
            await self.db.add_note(case.id, opened_by, note)
        await self.publish(guild, case)
        case = await self.db.get_case(guild.id, case.id) or case  # now includes the review message link
        if created and announce:
            await self.audit(guild, opened_by, "case.open", f"case #{case.id} ({account.username})", ", ".join(reasons))
        return case, created

    # ---------------------------------------------------------------- actions

    def _explain(self, case: Case, action: str, user_id: int) -> str:
        if action == "reopen":
            if case.is_open:
                return f"Case #{case.id} is already open."
            return f"Case #{case.id} can't be reopened: this account already has another open case."
        if not case.is_open:
            return f"Case #{case.id} was already **{case.status}** by {_who(case.resolved_by)} {embeds.ts(case.resolved_at)}."
        if action == "claim":
            if case.claimed_by == user_id:
                return f"You already claimed case #{case.id}."
            return f"Case #{case.id} is already claimed by {_who(case.claimed_by)}."
        if action == "release":
            if case.claimed_by is None:
                return f"Case #{case.id} isn't claimed."
            return f"Only {_who(case.claimed_by)} or an admin can release this claim."
        return (
            f"Case #{case.id} is claimed by {_who(case.claimed_by)}. "
            "Ask them to finish it or release it (admins can override)."
        )

    async def act(
        self, guild: discord.Guild, user: discord.Member, action: str, case_id: int, *, refresh: bool = True
    ) -> ActionResult:
        """Run a case action (claim/release/confirm/dismiss/reopen) with all the checks and logging."""
        case = await self.db.get_case(guild.id, case_id)
        if case is None:
            return ActionResult(False, None, f"There's no case #{case_id} in this server.")

        admin = is_admin(user)
        if action == "claim":
            ok = await self.db.claim_case(guild.id, case_id, user.id)
        elif action == "release":
            ok = await self.db.release_case(guild.id, case_id, user.id, force=admin)
        elif action in ("confirm", "dismiss"):
            status = "confirmed" if action == "confirm" else "dismissed"
            ok = await self.db.resolve_case(guild.id, case_id, user.id, status, force=admin)
        elif action == "reopen":
            ok = await self.db.reopen_case(guild.id, case_id)
        else:
            return ActionResult(False, case, f"Unknown action `{action}`.")

        fresh = await self.db.get_case(guild.id, case_id)
        if not ok:
            return ActionResult(False, fresh, self._explain(fresh or case, action, user.id))
        await self.audit(guild, user.id, f"case.{action}", f"case #{case_id} ({case.roblox_name})")
        if refresh and fresh:
            await self.publish(guild, fresh)
        return ActionResult(True, fresh)

    async def add_note(self, guild: discord.Guild, user: discord.abc.User, case_id: int, body: str) -> ActionResult:
        case = await self.db.get_case(guild.id, case_id)
        if case is None:
            return ActionResult(False, None, f"There's no case #{case_id} in this server.")
        await self.db.add_note(case_id, user.id, body)
        await self.audit(guild, user.id, "case.note", f"case #{case_id} ({case.roblox_name})", body)
        fresh = await self.db.get_case(guild.id, case_id)
        assert fresh is not None
        await self.publish(guild, fresh)
        return ActionResult(True, fresh)

    # ---------------------------------------------------------------- buttons

    async def handle_button(self, interaction: discord.Interaction, action: str, case_id: int) -> None:
        guild = interaction.guild
        assert guild is not None and isinstance(interaction.user, discord.Member)

        if action == "note":
            async def save(modal_interaction: discord.Interaction, text: str) -> None:
                if not text:
                    await modal_interaction.response.send_message("Note was empty, nothing saved.", ephemeral=True)
                    return
                await modal_interaction.response.defer(ephemeral=True, thinking=True)
                result = await self.add_note(guild, modal_interaction.user, case_id, text)
                await modal_interaction.followup.send(
                    f"📝 Note added to case #{case_id}." if result.ok else result.error, ephemeral=True
                )

            await interaction.response.send_modal(TextModal(f"Note for case #{case_id}", "Note", save, required=True))
            return

        on_case_message = bool(
            interaction.message
            and interaction.message.embeds
            and interaction.message.embeds[0].footer.text == f"Case #{case_id}"
        )
        # Acknowledge first: Discord only waits 3 seconds for a response.
        if on_case_message:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

        result = await self.act(guild, interaction.user, action, case_id, refresh=False)
        case = result.case
        if case and result.ok and not (interaction.message and interaction.message.id == case.review_message_id):
            await self.publish(guild, case)  # keep the shared review message in sync
        if on_case_message:
            if case:
                embed, view = await self.render(case)
                await interaction.edit_original_response(embed=embed, view=view)
            if result.error:
                await interaction.followup.send(result.error, ephemeral=True)
        else:
            await interaction.followup.send(result.error or f"Done: case #{case_id} {PAST_TENSE[action]}.", ephemeral=True)

    async def handle_profile_button(self, interaction: discord.Interaction, action: str, roblox_id: int) -> None:
        guild = interaction.guild
        assert guild is not None
        user = interaction.user

        if action == "track":
            await interaction.response.defer(ephemeral=True, thinking=True)
            tracking = self.bot.get_cog("Tracking")
            message = await tracking.start_tracking(guild, user, roblox_id, None)  # type: ignore[union-attr]
            await interaction.followup.send(message, ephemeral=True)
            return

        if action == "trust":
            async def trust(modal_interaction: discord.Interaction, reason: str) -> None:
                await modal_interaction.response.defer(ephemeral=True, thinking=True)
                message = await self.trust(guild, modal_interaction.user, roblox_id, reason or None)
                await modal_interaction.followup.send(message, ephemeral=True)

            await interaction.response.send_modal(
                TextModal("Mark account as trusted", "Why is this account OK? (optional)", trust)
            )
            return

        if action == "case":
            async def open_case(modal_interaction: discord.Interaction, reason: str) -> None:
                await modal_interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    result = await self.bot.scanner.scan(guild.id, roblox_id)
                except RobloxError as exc:
                    await modal_interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
                    return
                case, created = await self.open_from_scan(
                    guild, result, opened_by=modal_interaction.user.id, manual_reason=reason or "Opened from /check"
                )
                await modal_interaction.followup.send(self.case_opened_text(guild, case, created), ephemeral=True)

            await interaction.response.send_modal(
                TextModal("Open a case", "Reason (optional)", open_case, placeholder="What should other mods look at?")
            )

    def case_opened_text(self, guild: discord.Guild, case: Case | None, created: bool) -> str:
        if case is None:
            return "Nothing was flagged, so no case was opened."
        where = ""
        if case.review_channel_id and case.review_message_id:
            where = f" → https://discord.com/channels/{guild.id}/{case.review_channel_id}/{case.review_message_id}"
        elif not case.review_channel_id:
            where = " (set a review channel with `/config channels` so the team sees it)"
        if created:
            return f"📂 Opened **case #{case.id}**{where}"
        return f"📂 This account already has open **case #{case.id}**; it was updated{where}"

    # ---------------------------------------------------------------- trusted

    async def trust(self, guild: discord.Guild, user: discord.abc.User, roblox_id: int, reason: str | None) -> str:
        try:
            account = await self.bot.scanner.fetch(roblox_id)
        except RobloxError as exc:
            return f"⚠️ {exc}"
        await self.db.add_trusted(guild.id, roblox_id, account.username, user.id, reason)
        await self.audit(guild, user.id, "trust.add", account.username, reason or "")
        return (
            f"🛡️ **{discord.utils.escape_markdown(account.username)}** is now trusted: "
            "it won't be auto-flagged again in this server."
        )
