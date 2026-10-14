"""/config, /trust and /keywords."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from .. import embeds
from ..content_filter import MATCH_MODES, normalize_term
from ..permissions import admin_only, moderator_only

if TYPE_CHECKING:
    from ..bot import RobloxCheckerBot

NO_MENTIONS = discord.AllowedMentions.none()


def _can_post(channel: discord.TextChannel, me: discord.Member) -> bool:
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.send_messages and perms.embed_links


class Settings(commands.Cog):
    config = app_commands.Group(name="config", description="Bot settings for this server", guild_only=True)
    trust = app_commands.Group(name="trust", description="Accounts that should never be auto-flagged", guild_only=True)
    keywords = app_commands.Group(name="keywords", description="Tune the NSFW filter for this server", guild_only=True)

    def __init__(self, bot: "RobloxCheckerBot"):
        self.bot = bot

    # ------------------------------------------------------------------- setup

    @app_commands.command(name="setup", description="One-step setup: create the bot's channels and choose your moderator role.")
    @app_commands.describe(mod_role="The role your moderators have. They'll be able to see the bot's channels and use it.")
    @app_commands.guild_only()
    @admin_only()
    async def setup_cmd(self, interaction: discord.Interaction, mod_role: discord.Role | None = None) -> None:
        guild = interaction.guild
        assert guild is not None
        cfg = await self.bot.db.get_config(guild.id)
        existing = guild.get_channel(cfg.review_channel_id) if cfg.review_channel_id else None
        if existing is not None:
            if mod_role:
                await self.bot.db.add_mod_role(guild.id, mod_role.id)
            await interaction.response.send_message(
                f"✅ Already set up. Flagged accounts go to {existing.mention}."
                + (f" {mod_role.mention} is a moderator role." if mod_role else "")
                + "\nTo use different channels, use `/config channels`.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        if not guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission to create my channels. Kick me and add me again with the "
                "link from the bot's window, or make the channels yourself and use `/config channels`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        # Private to moderators: hidden from @everyone, visible to the bot and the mod role (admins see everything).
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
        }
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        reason = f"RobloxChecker /setup by {interaction.user}"
        try:
            category = await guild.create_category("RobloxChecker", overwrites=overwrites, reason=reason)
            review = await guild.create_text_channel(
                "alt-review", category=category, reason=reason,
                topic="Flagged Roblox accounts. Press Claim, look into it, then Confirm or Dismiss.",
            )
            log = await guild.create_text_channel(
                "mod-log", category=category, reason=reason, topic="Everything moderators do with RobloxChecker."
            )
            tracking = await guild.create_text_channel(
                "badge-tracking", category=category, reason=reason, topic="Badge updates for players on the watchlist (/track add)."
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Couldn't create the channels: {exc.text or exc}. Check my permissions and try again.", ephemeral=True)
            return

        await self.bot.db.update_config(
            guild.id, review_channel_id=review.id, log_channel_id=log.id, tracking_channel_id=tracking.id
        )
        if mod_role:
            await self.bot.db.add_mod_role(guild.id, mod_role.id)
        await self.bot.cases.audit(guild, interaction.user.id, "config", "Ran /setup")
        who = (
            f"Only admins and {mod_role.mention} can see them."
            if mod_role
            else "Only admins can see them. Run `/setup mod_role:@YourModRole` to let your mods in."
        )
        await interaction.followup.send(
            f"✅ All set! I made {review.mention}, {log.mention} and {tracking.mention}. {who}\n\n"
            "**Try it:** type `/check` and paste a Roblox profile link.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    # ------------------------------------------------------------------ config

    @config.command(name="show", description="Show the current settings.")
    @moderator_only()
    async def show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        cfg = await self.bot.db.get_config(interaction.guild.id)

        def channel(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "not set"

        roles = ", ".join(f"<@&{r}>" for r in cfg.mod_role_ids) or "none (Moderate/Kick/Ban Members permission counts)"
        embed = discord.Embed(title="RobloxChecker settings", colour=embeds.BLUE)
        embed.add_field(
            name="Channels",
            value=(
                f"Review queue: {channel(cfg.review_channel_id)}\n"
                f"Mod log: {channel(cfg.log_channel_id)}\n"
                f"Tracking updates: {channel(cfg.tracking_channel_id)}"
            ),
            inline=False,
        )
        embed.add_field(name="Moderator roles", value=roles, inline=False)
        embed.add_field(
            name="Thresholds",
            value=(
                f"Likely alt ≥ **{cfg.alt_threshold}** · Suspicious ≥ **{cfg.suspicious_threshold}**\n"
                f"NSFW flag ≥ **{cfg.nsfw_threshold}**"
            ),
            inline=False,
        )
        embed.add_field(name="Auto-open cases", value="on" if cfg.auto_flag else "off", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _changed(self, interaction: discord.Interaction, description: str) -> None:
        assert interaction.guild is not None
        await self.bot.cases.audit(interaction.guild, interaction.user.id, "config", description)
        await interaction.response.send_message(f"⚙️ {description}", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @config.command(name="channels", description="Choose where cases, logs and tracking updates are posted.")
    @app_commands.describe(
        review="Shared review queue: every flagged account gets a message with Claim/Confirm/Dismiss buttons",
        log="Mod log: every action moderators take",
        tracking="Badge tracking updates (falls back to the review channel)",
    )
    @admin_only()
    async def channels(
        self,
        interaction: discord.Interaction,
        review: discord.TextChannel | None = None,
        log: discord.TextChannel | None = None,
        tracking: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        values = {}
        if review:
            values["review_channel_id"] = review.id
        if log:
            values["log_channel_id"] = log.id
        if tracking:
            values["tracking_channel_id"] = tracking.id
        if not values:
            await interaction.response.send_message("Pick at least one channel.", ephemeral=True)
            return
        me = interaction.guild.me
        missing = [
            channel.mention
            for channel in (review, log, tracking)
            if channel and not _can_post(channel, me)
        ]
        if missing:
            await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {', '.join(missing)}. "
                "Fix my permissions there first.",
                ephemeral=True,
            )
            return
        await self.bot.db.update_config(interaction.guild.id, **values)
        parts = [f"{name.split('_')[0]} → <#{cid}>" for name, cid in values.items()]
        await self._changed(interaction, "Channels updated: " + ", ".join(parts))

    @config.command(name="clear-channel", description="Stop posting to one of the bot channels.")
    @admin_only()
    async def clear_channel(self, interaction: discord.Interaction, which: Literal["review", "log", "tracking"]) -> None:
        assert interaction.guild is not None
        await self.bot.db.update_config(interaction.guild.id, **{f"{which}_channel_id": None})
        await self._changed(interaction, f"Cleared the {which} channel")

    @config.command(name="add-modrole", description="Let members with this role use the moderation commands.")
    @admin_only()
    async def add_modrole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild is not None
        if not await self.bot.db.add_mod_role(interaction.guild.id, role.id):
            await interaction.response.send_message(f"{role.mention} is already a moderator role.", ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        await self._changed(interaction, f"Added moderator role {role.mention}")

    @config.command(name="remove-modrole", description="Remove a moderator role.")
    @admin_only()
    async def remove_modrole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild is not None
        if not await self.bot.db.remove_mod_role(interaction.guild.id, role.id):
            await interaction.response.send_message(f"{role.mention} isn't a moderator role.", ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        await self._changed(interaction, f"Removed moderator role {role.mention}")

    @config.command(name="thresholds", description="Tune when accounts get flagged (scores are 0-100).")
    @app_commands.describe(
        alt="Alt score at which an account is a 'Likely alt' and gets a case (default 60)",
        suspicious="Alt score shown as 'Suspicious' (default 35)",
        nsfw="NSFW score at which an account gets a case (default 40)",
    )
    @admin_only()
    async def thresholds(
        self,
        interaction: discord.Interaction,
        alt: app_commands.Range[int, 1, 100] | None = None,
        suspicious: app_commands.Range[int, 1, 100] | None = None,
        nsfw: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        assert interaction.guild is not None
        cfg = await self.bot.db.get_config(interaction.guild.id)
        new_alt = alt or cfg.alt_threshold
        new_suspicious = suspicious or cfg.suspicious_threshold
        if new_suspicious >= new_alt:
            await interaction.response.send_message(
                f"The suspicious threshold ({new_suspicious}) has to be lower than the alt threshold ({new_alt}).",
                ephemeral=True,
            )
            return
        values = {"alt_threshold": new_alt, "suspicious_threshold": new_suspicious}
        if nsfw:
            values["nsfw_threshold"] = nsfw
        await self.bot.db.update_config(interaction.guild.id, **values)
        await self._changed(
            interaction,
            f"Thresholds: alt ≥ {new_alt}, suspicious ≥ {new_suspicious}, nsfw ≥ {nsfw or cfg.nsfw_threshold}",
        )

    @config.command(name="autoflag", description="Automatically open cases when a scan crosses a threshold.")
    @admin_only()
    async def autoflag(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.bot.db.update_config(interaction.guild.id, auto_flag=int(enabled))
        await self._changed(interaction, f"Auto-open cases {'enabled' if enabled else 'disabled'}")

    # ------------------------------------------------------------------- trust

    @trust.command(name="add", description="Mark an account as fine so it's never auto-flagged.")
    @app_commands.describe(user="Roblox username, user ID or profile link", reason="Why it's OK, for the other mods")
    @moderator_only()
    async def trust_add(
        self, interaction: discord.Interaction, user: str, reason: app_commands.Range[str, 1, 300] | None = None
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self.bot.roblox.resolve_user_id(user)
        message = await self.bot.cases.trust(interaction.guild, interaction.user, roblox_id, reason)
        await interaction.followup.send(message, ephemeral=True)

    @trust.command(name="remove", description="Remove an account from the trusted list.")
    @moderator_only()
    async def trust_remove(self, interaction: discord.Interaction, user: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        roblox_id = await self.bot.roblox.resolve_user_id(user)
        row = await self.bot.db.get_trusted(interaction.guild.id, roblox_id)
        if row is None:
            await interaction.followup.send("That account isn't trusted.", ephemeral=True)
            return
        await self.bot.db.remove_trusted(interaction.guild.id, roblox_id)
        await self.bot.cases.audit(interaction.guild, interaction.user.id, "trust.remove", row["roblox_name"])
        await interaction.followup.send(f"Removed **{discord.utils.escape_markdown(row['roblox_name'])}** from the trusted list.", ephemeral=True)

    @trust.command(name="list", description="Show trusted accounts.")
    @moderator_only()
    async def trust_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        rows = await self.bot.db.list_trusted(interaction.guild.id)
        lines = [
            f"[{discord.utils.escape_markdown(r['roblox_name'])}](https://www.roblox.com/users/{r['roblox_id']}/profile)"
            f" · by {embeds.mention(r['added_by'])}" + (f": {embeds.clip(r['reason'], 80)}" if r["reason"] else "")
            for r in rows
        ]
        embed = discord.Embed(
            title=f"Trusted accounts ({len(rows)})",
            description=embeds.clip("\n".join(lines) or "None yet.", 4000),
            colour=embeds.GREEN,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ---------------------------------------------------------------- keywords

    async def filter_terms(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id is None:
            return []
        content_filter = await self.bot.filters.get(interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=embeds.clip(f"{rule.label} ({rule.category}, {rule.weight})", 100), value=rule.label[:100])
            for rule in content_filter.rules
            if current in rule.label.lower()
        ][:25]

    async def custom_terms(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id is None:
            return []
        rows = await self.bot.db.list_terms(interaction.guild_id)
        return [
            app_commands.Choice(name=f"{r['term']} ({'disabled' if r['weight'] == 0 else r['weight']})", value=r["term"])
            for r in rows
            if current.lower() in r["term"]
        ][:25]

    async def _terms_changed(self, interaction: discord.Interaction, action: str, description: str) -> None:
        assert interaction.guild is not None
        self.bot.filters.invalidate(interaction.guild.id)
        await self.bot.cases.audit(interaction.guild, interaction.user.id, action, description)
        await interaction.response.send_message(f"🔤 {description}", ephemeral=True)

    @keywords.command(name="add", description="Add a term to the NSFW filter (or change a term's weight).")
    @app_commands.describe(
        term="Letters and spaces only; leetspeak and spaced-out versions are matched automatically",
        weight="Points per match (the server's NSFW threshold is usually 40, so 40+ flags on its own)",
        match="word = whole word, prefix = word starting with it, substring = anywhere",
        category="Label shown to moderators",
    )
    @moderator_only()
    async def keywords_add(
        self,
        interaction: discord.Interaction,
        term: str,
        weight: app_commands.Range[int, 1, 100] = 40,
        match: Literal["word", "prefix", "substring"] = "word",
        category: app_commands.Range[str, 1, 30] = "custom",
    ) -> None:
        assert interaction.guild is not None and match in MATCH_MODES
        cleaned = normalize_term(term)
        if cleaned is None:
            await interaction.response.send_message("Terms must be 2-50 letters/spaces.", ephemeral=True)
            return
        await self.bot.db.set_term(interaction.guild.id, cleaned, weight, match, category, interaction.user.id)
        await self._terms_changed(interaction, "keyword.set", f"`{cleaned}` → weight {weight}, {match} match ({category})")

    @keywords.command(name="disable", description="Turn off one of the built-in terms/patterns for this server.")
    @app_commands.autocomplete(term=filter_terms)
    @moderator_only()
    async def keywords_disable(self, interaction: discord.Interaction, term: str) -> None:
        assert interaction.guild is not None
        content_filter = await self.bot.filters.get(interaction.guild.id)
        rule = next((r for r in content_filter.rules if r.label.lower() == term.lower()), None)
        if rule is None:
            await interaction.response.send_message("That term isn't active. Pick one from the list.", ephemeral=True)
            return
        key = normalize_term(rule.label) or rule.label.lower()
        await self.bot.db.set_term(interaction.guild.id, key, 0, "word", rule.category, interaction.user.id)
        await self._terms_changed(interaction, "keyword.set", f"`{rule.label}` disabled")

    @keywords.command(name="remove", description="Undo a custom term, re-weight or disable (back to the default list).")
    @app_commands.autocomplete(term=custom_terms)
    @moderator_only()
    async def keywords_remove(self, interaction: discord.Interaction, term: str) -> None:
        assert interaction.guild is not None
        if not await self.bot.db.remove_term(interaction.guild.id, term.lower()):
            await interaction.response.send_message("No custom setting for that term.", ephemeral=True)
            return
        await self._terms_changed(interaction, "keyword.remove", f"`{term.lower()}` reset to default")

    @keywords.command(name="list", description="Show this server's custom filter terms.")
    @moderator_only()
    async def keywords_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        rows = await self.bot.db.list_terms(interaction.guild.id)
        content_filter = await self.bot.filters.get(interaction.guild.id)
        lines = [
            f"`{r['term']}` · " + ("**disabled**" if r["weight"] == 0 else f"{r['weight']} pts, {r['match']}, {r['category']}")
            + f" · {embeds.mention(r['added_by'])}"
            for r in rows
        ]
        embed = discord.Embed(
            title="NSFW filter",
            description=(
                f"**{len(content_filter.rules)}** active rules (built-in list + this server's changes).\n"
                "Use `/keywords test` to try text against the filter.\n\n"
                + ("**This server's changes**\n" + "\n".join(lines) if lines else "No custom changes yet.")
            )[:4000],
            colour=embeds.BLUE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @keywords.command(name="test", description="See how the NSFW filter would score some text (only you see this).")
    @moderator_only()
    async def keywords_test(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]) -> None:
        assert interaction.guild is not None
        cfg = await self.bot.db.get_config(interaction.guild.id)
        content_filter = await self.bot.filters.get(interaction.guild.id)
        report = content_filter.scan_fields([("description", text)], cfg.nsfw_threshold)
        await interaction.response.send_message(
            embed=embeds.filter_test_embed(text, report.hits, report.score, cfg.nsfw_threshold), ephemeral=True
        )


async def setup(bot: "RobloxCheckerBot") -> None:
    await bot.add_cog(Settings(bot))
