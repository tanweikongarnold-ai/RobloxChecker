"""Bot setup: wires the database, Roblox client, scanner and cogs together."""

from __future__ import annotations

import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings, forget_env_value
from .db import Database
from .friends import FriendChecker
from .permissions import NotAdmin, NotModerator
from .roblox import GroupNotFound, RobloxClient, RobloxError, UserNotFound
from .scanner import FilterRegistry, Scanner
from .services import CaseService
from .views import CaseButton, ProfileButton

log = logging.getLogger("robloxchecker")

EXTENSIONS = ("bot.cogs.check", "bot.cogs.cases", "bot.cogs.tracking", "bot.cogs.settings", "bot.cogs.groups",
              "bot.cogs.database")

# View Channels, Send Messages, Embed Links, Read Message History, and Manage Channels (for /setup).
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_channels=True)


def invite_link(app_id: int) -> str:
    # integration_type=0 means "add to a server" (not "Add to My Apps", which never puts the bot in the server).
    url = discord.utils.oauth_url(app_id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands"))
    return url + "&integration_type=0"


class Tree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Stop early, with the fix, when the bot itself isn't in the server the command came from."""
        if interaction.guild_id is None or self.client.get_guild(interaction.guild_id) is not None:
            return True
        link = invite_link(self.client.user.id) if self.client.user else "the link in the bot's window"
        await interaction.response.send_message(
            "I'm not in this server yet, so I can't see its roles or channels.\n"
            f"Add me with this link and choose **Add to Server** (not *Add to My Apps*):\n{link}\n"
            "If you only just started me or added me, wait 10 seconds and try again.",
            ephemeral=True,
        )
        return False


class RobloxCheckerBot(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            tree_cls=Tree,
            # Only offer the commands inside servers that added the bot.
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.roblox = RobloxClient(max_badges=settings.max_badges)
        self.filters = FilterRegistry(self.db)
        self.scanner = Scanner(self.roblox, self.db, self.filters)
        self.friends = FriendChecker(self.roblox)
        self.cases = CaseService(self)
        self._greeted = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.roblox.start()
        # Buttons encode their action in the custom_id, so registering the
        # classes makes every old case message clickable again after a restart.
        self.add_dynamic_items(CaseButton, ProfileButton)
        self.tree.error(self.on_app_command_error)
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), self.settings.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d server(s)", self.user, getattr(self.user, "id", "?"), len(self.guilds))
        if self._greeted or self.user is None:
            return
        self._greeted = True
        line = "=" * 64
        print(f"\n{line}\n  {self.user.name} is online!\n")
        print("  Add it to your server with this link:")
        print(f"  {invite_link(self.user.id)}\n")
        print("  Then type /setup in your server.")
        print(f"  Keep this window open. Closing it turns the bot off.\n{line}\n", flush=True)

    async def close(self) -> None:
        await super().close()
        await self.roblox.close()
        await self.db.close()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, (NotModerator, NotAdmin)):
            message = f"🚫 {error}"
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command only works inside a server."
        elif isinstance(error, app_commands.CheckFailure):
            message = "🚫 You can't use this command."
        elif isinstance(original, (UserNotFound, GroupNotFound)):
            message = f"🔍 {original}"
        elif isinstance(original, RobloxError):
            message = f"⚠️ Roblox API problem: {original}"
        else:
            log.exception("Command %s failed", getattr(interaction.command, "qualified_name", "?"), exc_info=original)
            message = "💥 Something went wrong running that command. The error has been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    settings = Settings.from_env()
    discord.utils.setup_logging(level=getattr(logging, settings.log_level, logging.INFO))
    bot = RobloxCheckerBot(settings)
    try:
        bot.run(settings.token, log_handler=None)
    except discord.LoginFailure:
        forget_env_value("DISCORD_TOKEN")
        print(
            "\nDiscord didn't accept that bot token. Get a new one (Developer Portal > your app > Bot > Reset Token),"
            "\nthen start the bot again and paste it when asked.",
            file=sys.stderr,
        )
        sys.exit(1)
    except (OSError, discord.GatewayNotFound) as exc:
        print(f"\nCouldn't connect to Discord ({exc.__class__.__name__}). Check your internet connection and start the bot again.", file=sys.stderr)
        sys.exit(1)
