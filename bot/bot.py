"""Bot setup: wires the database, Roblox client, scanner and cogs together."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings
from .db import Database
from .permissions import NotAdmin, NotModerator
from .roblox import RobloxClient, RobloxError, UserNotFound
from .scanner import FilterRegistry, Scanner
from .services import CaseService
from .views import CaseButton, ProfileButton

log = logging.getLogger("robloxchecker")

EXTENSIONS = ("bot.cogs.check", "bot.cogs.cases", "bot.cogs.tracking", "bot.cogs.settings")


class RobloxCheckerBot(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.roblox = RobloxClient(max_badges=settings.max_badges)
        self.filters = FilterRegistry(self.db)
        self.scanner = Scanner(self.roblox, self.db, self.filters)
        self.cases = CaseService(self)

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
        elif isinstance(original, UserNotFound):
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
    bot.run(settings.token, log_handler=None)
