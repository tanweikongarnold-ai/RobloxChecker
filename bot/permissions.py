"""Who is allowed to do what.

* **Admins**: anyone with *Manage Server* (or Administrator). They configure the
  bot and can override another moderator's claim on a case.
* **Moderators**: members with one of the roles added via ``/config modrole``.
  Until a mod role is configured, anyone with Moderate/Kick/Ban Members counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from .bot import RobloxCheckerBot


class NotModerator(app_commands.CheckFailure):
    pass


class NotAdmin(app_commands.CheckFailure):
    pass


def member_permissions(member: discord.Member) -> discord.Permissions:
    """The member's permissions as Discord reported them with the interaction.

    Discord sends these with every slash command and button click, so they're
    right even when the bot's own copy of the server (roles, owner) is missing
    or still loading. Fall back to computing them from that copy otherwise.
    """
    resolved = member.resolved_permissions
    if resolved is not None and resolved.value:
        return resolved
    return member.guild_permissions


def is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member_permissions(member)
    return perms.administrator or perms.manage_guild


async def is_moderator(bot: "RobloxCheckerBot", member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if is_admin(member):
        return True
    mod_roles = await bot.db.get_mod_roles(member.guild.id)
    if mod_roles:
        # _roles holds the member's role IDs straight from Discord, even if the roles aren't cached yet.
        role_ids = {role.id for role in member.roles} | set(getattr(member, "_roles", ()))
        return any(role_id in mod_roles for role_id in role_ids)
    perms = member_permissions(member)
    return perms.moderate_members or perms.kick_members or perms.ban_members


async def ensure_moderator(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        raise app_commands.NoPrivateMessage()
    if not await is_moderator(interaction.client, interaction.user):  # type: ignore[arg-type]
        raise NotModerator("You need a moderator role to use this.")
    return True


async def ensure_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        raise app_commands.NoPrivateMessage()
    if not is_admin(interaction.user):
        raise NotAdmin("You need the **Manage Server** permission to change bot settings.")
    return True


def moderator_only():
    return app_commands.check(ensure_moderator)


def admin_only():
    return app_commands.check(ensure_admin)
