"""The three pages of a /check result: overview, friends, groups & badges."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from . import embeds
from .content_filter import Hit
from .db import Case
from .friends import FriendsReport, analyse_friends
from .roblox import RobloxError
from .scanner import ScanResult
from .views import PROFILE_BUTTONS, _moderator_gate, profile_link, profile_view

if TYPE_CHECKING:
    from .bot import RobloxCheckerBot

log = logging.getLogger(__name__)

PAGE_LABELS = {"overview": "Page 1/3", "friends": "Page 2/3", "groups": "Page 3/3"}


class CheckPagesView(discord.ui.View):
    """Buttons to flip between pages. The friends page fills in while you read page 1.

    Lives ~14 minutes (as long as Discord lets the bot edit the reply), then
    swaps itself for the permanent Open case / Track / Trust buttons.
    """

    def __init__(
        self,
        bot: "RobloxCheckerBot",
        guild: discord.Guild,
        result: ScanResult,
        open_case: Case | None,
        group_hits: dict[int, list[Hit]],
        *,
        show_case: bool,
        show_track: bool,
        show_trust: bool,
        checked_by: int | None = None,
    ):
        super().__init__(timeout=840)
        self.checked_by = checked_by
        self.bot = bot
        self.guild = guild
        self.result = result
        self.open_case = open_case
        self.group_hits = group_hits
        self.show = {"case": show_case, "track": show_track, "trust": show_trust}
        self.page = "overview"
        self.sub_page = 0
        self.report: FriendsReport | None = None
        self.error: str | None = None
        self.message: discord.Message | discord.WebhookMessage | None = None
        self._loader: asyncio.Task | None = None
        self._opening = asyncio.Lock()
        self._rebuild()

    @classmethod
    async def create(cls, bot: "RobloxCheckerBot", guild: discord.Guild, result: ScanResult, open_case: Case | None, **show):
        content_filter = await bot.filters.get(guild.id)
        group_hits = {g.id: content_filter.scan_text(g.name, "group") for g in result.account.groups or []}
        return cls(bot, guild, result, open_case, group_hits, **show)

    # ------------------------------------------------------------ rendering

    @property
    def user_id(self) -> int:
        return self.result.account.user_id

    def embed(self) -> discord.Embed:
        label = PAGE_LABELS[self.page]
        if self.page == "friends":
            return embeds.friends_embed(
                self.result.account, self.report, sub_page=self.sub_page,
                progress=self.bot.friends.progress(self.user_id), error=self.error, page_label=label,
            )
        if self.page == "groups":
            return embeds.groups_badges_embed(self.result, self.group_hits, page_label=label)
        return embeds.scan_embed(self.result, open_case=self.open_case, page_label=label)

    def _friends_label(self) -> str:
        if self.error:
            return "Friends ⚠️"
        if self.report is None:
            return "Friends ⏳"
        flagged = len(self.report.flagged)
        return f"Friends ({self.report.data.total})" + (f" · {flagged} flagged" if flagged else "")

    def _button(self, *, label: str | None = None, emoji: str | None = None, style=discord.ButtonStyle.secondary,
                row: int, disabled: bool = False, callback) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)

    def _rebuild(self) -> None:
        self.clear_items()
        tabs = (("overview", "📋", "Overview"), ("friends", "👥", self._friends_label()), ("groups", "🏷️", "Groups & badges"))
        for key, emoji, label in tabs:
            style = discord.ButtonStyle.primary if key == self.page else discord.ButtonStyle.secondary
            self._button(label=label, emoji=emoji, style=style, row=0, callback=self._go(key))

        if self.page == "friends" and self.report is not None:
            pages = embeds.friends_page_count(self.report)
            if pages > 1:
                self._button(emoji="◀️", row=1, disabled=self.sub_page == 0, callback=self._flip(-1))
                self._button(emoji="▶️", row=1, disabled=self.sub_page >= pages - 1, callback=self._flip(1))
            todo = [c for c in self.report.flagged if not c.open_case]
            if todo:
                self._button(
                    label=f"Open cases for {len(todo)} flagged friend(s)", emoji="📂",
                    style=discord.ButtonStyle.danger, row=1, callback=self._open_friend_cases,
                )

        for action, (style, label, emoji) in PROFILE_BUTTONS.items():
            if self.show[action]:
                self._button(label=label, emoji=emoji, style=style, row=2, callback=self._action(action))
        link = profile_link(self.user_id)
        link.row = 2
        self.add_item(link)

    async def _refresh(self) -> None:
        """Redraw the message (used by the background friends check)."""
        self._rebuild()
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException as exc:
            log.info("Couldn't update /check pages: %s", exc)

    # ------------------------------------------------------------ buttons

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _moderator_gate(interaction)

    def _go(self, page: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.page = page
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        return callback

    def _flip(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            pages = embeds.friends_page_count(self.report)
            self.sub_page = max(0, min(pages - 1, self.sub_page + step))
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        return callback

    def _action(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.bot.cases.handle_profile_button(interaction, action, self.user_id)
        return callback

    async def _open_friend_cases(self, interaction: discord.Interaction) -> None:
        assert self.report is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._opening:
            account = self.result.account
            note = f"Found among the friends of @{account.username} ({account.profile_url})."
            created = existing = 0
            for check in [c for c in self.report.flagged if not c.open_case]:
                case, was_created = await self.bot.cases.open_from_scan(
                    self.guild, check.result, opened_by=interaction.user.id, note=note, announce=False
                )
                if case:
                    check.open_case = case.id
                    created += was_created
                    existing += not was_created
            await self.bot.cases.audit(
                self.guild, interaction.user.id, "friends.cases", account.username,
                f"{created} new case(s), {existing} already open",
            )
        extra = f" {existing} already had one." if existing else ""
        await interaction.followup.send(f"📂 Opened {created} new case(s) in the review channel.{extra}", ephemeral=True)
        await self._refresh()

    # ------------------------------------------------------- friends check

    def start(self) -> None:
        self._loader = asyncio.create_task(self._load_friends())

    async def _load_friends(self) -> None:
        fetch = asyncio.ensure_future(self.bot.friends.fetch(self.user_id))
        try:
            while not fetch.done():
                await asyncio.wait({fetch}, timeout=3)
                if not fetch.done() and self.page == "friends":
                    await self._refresh()  # show progress
            self.report = await analyse_friends(self.bot, self.guild.id, fetch.result())
            found_via = f"friend of @{self.result.account.username}"
            await self.bot.db.save_players(self.guild.id, [
                c.result.record(self.report.config, depth="quick", found_via=found_via, checked_by=self.checked_by)
                for c in self.report.checks
            ])
        except RobloxError as exc:
            self.error = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Friends check for %s failed", self.user_id)
            self.error = "something went wrong while checking them"
        await self._refresh()

    async def on_timeout(self) -> None:
        if self._loader and not self._loader.done():
            self._loader.cancel()
        if self.message is None:
            return
        # Page buttons stop working once Discord's 15 minutes are up; keep the permanent action buttons.
        try:
            await self.message.edit(
                view=profile_view(self.user_id, show_case=self.show["case"], show_track=self.show["track"], show_trust=self.show["trust"])
            )
        except discord.HTTPException:
            pass
