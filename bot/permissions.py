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


def is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


async def is_moderator(bot: "RobloxCheckerBot", member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if is_admin(member):
        return True
    mod_roles = await bot.db.get_mod_roles(member.guild.id)
    if mod_roles:
        return any(role.id in mod_roles for role in member.roles)
    perms = member.guild_permissions
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
