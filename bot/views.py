"""Buttons and modals.

Buttons are :class:`discord.ui.DynamicItem` s: their ``custom_id`` encodes the
action and target (``case:claim:12``), so they keep working after the bot
restarts and any moderator can press them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from .permissions import is_moderator

if TYPE_CHECKING:
    from .bot import RobloxCheckerBot
    from .db import Case

CASE_BUTTONS = {
    "claim": (discord.ButtonStyle.primary, "Claim", "👀"),
    "release": (discord.ButtonStyle.secondary, "Release claim", "↩️"),
    "confirm": (discord.ButtonStyle.danger, "Confirm", "✅"),
    "dismiss": (discord.ButtonStyle.secondary, "Dismiss", "🗑️"),
    "note": (discord.ButtonStyle.secondary, "Add note", "📝"),
    "reopen": (discord.ButtonStyle.secondary, "Reopen", "🔄"),
}
PROFILE_BUTTONS = {
    "case": (discord.ButtonStyle.danger, "Open case", "📂"),
    "track": (discord.ButtonStyle.primary, "Track badges", "📈"),
    "trust": (discord.ButtonStyle.success, "Mark trusted", "🛡️"),
}


async def _moderator_gate(interaction: discord.Interaction) -> bool:
    if interaction.guild and await is_moderator(interaction.client, interaction.user):  # type: ignore[arg-type]
        return True
    await interaction.response.send_message("Only moderators can use these buttons.", ephemeral=True)
    return False


class CaseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"case:(?P<action>[a-z]+):(?P<case_id>[0-9]+)"):
    def __init__(self, action: str, case_id: int):
        style, label, emoji = CASE_BUTTONS[action]
        super().__init__(discord.ui.Button(style=style, label=label, emoji=emoji, custom_id=f"case:{action}:{case_id}"))
        self.action = action
        self.case_id = case_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        action = match["action"]
        if action not in CASE_BUTTONS:
            raise ValueError(f"unknown case action {action}")
        return cls(action, int(match["case_id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _moderator_gate(interaction)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RobloxCheckerBot = interaction.client  # type: ignore[assignment]
        await bot.cases.handle_button(interaction, self.action, self.case_id)


class ProfileButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rbx:(?P<action>[a-z]+):(?P<roblox_id>[0-9]+)"):
    def __init__(self, action: str, roblox_id: int):
        style, label, emoji = PROFILE_BUTTONS[action]
        super().__init__(
            discord.ui.Button(style=style, label=label, emoji=emoji, custom_id=f"rbx:{action}:{roblox_id}")
        )
        self.action = action
        self.roblox_id = roblox_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        action = match["action"]
        if action not in PROFILE_BUTTONS:
            raise ValueError(f"unknown profile action {action}")
        return cls(action, int(match["roblox_id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _moderator_gate(interaction)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RobloxCheckerBot = interaction.client  # type: ignore[assignment]
        await bot.cases.handle_profile_button(interaction, self.action, self.roblox_id)


class TextModal(discord.ui.Modal):
    """A one-field modal that hands the submitted text to ``on_done``."""

    def __init__(
        self,
        title: str,
        label: str,
        on_done: Callable[[discord.Interaction, str], Awaitable[None]],
        *,
        required: bool = False,
        placeholder: str | None = None,
        max_length: int = 1000,
    ):
        super().__init__(title=title, timeout=600)
        self.field = discord.ui.TextInput(
            label=label,
            style=discord.TextStyle.paragraph,
            required=required,
            placeholder=placeholder,
            max_length=max_length,
        )
        self.add_item(self.field)
        self._on_done = on_done

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_done(interaction, self.field.value.strip())


def profile_link(roblox_id: int) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.link, label="Roblox profile", url=f"https://www.roblox.com/users/{roblox_id}/profile"
    )


def case_view(case: "Case") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if case.is_open:
        actions = ["release" if case.claimed_by else "claim", "confirm", "dismiss", "note"]
    else:
        actions = ["reopen", "note"]
    for action in actions:
        view.add_item(CaseButton(action, case.id))
    view.add_item(profile_link(case.roblox_id))
    return view


def profile_view(roblox_id: int, *, show_case: bool = True, show_track: bool = True, show_trust: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action, show in (("case", show_case), ("track", show_track), ("trust", show_trust)):
        if show:
            view.add_item(ProfileButton(action, roblox_id))
    view.add_item(profile_link(roblox_id))
    return view
