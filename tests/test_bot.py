"""Offline checks of the Discord layer: command tree, embeds, buttons and the case service."""

import discord
import pytest

from bot import embeds
from bot.bot import EXTENSIONS, RobloxCheckerBot
from bot.config import Settings
from bot.content_filter import ContentFilter
from bot import alt_detector
from bot.scanner import ScanResult
from bot.views import CaseButton, ProfileButton, case_view, profile_view

from .helpers import make_account


@pytest.fixture
async def bot(tmp_path):
    settings = Settings(
        token="x", dev_guild_id=None, database_path=str(tmp_path / "bot.db"),
        track_interval_minutes=360, max_badges=500, log_level="INFO",
    )
    instance = RobloxCheckerBot(settings)
    await instance.db.connect()
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    instance.get_cog("Tracking").tracker.cancel()
    yield instance
    await instance.db.close()
    await instance.roblox.close()


def scan_result(**account_kwargs):
    account = make_account(**account_kwargs)
    nsfw = ContentFilter.load().scan_account(account, 40)
    return ScanResult(account=account, alt=alt_detector.analyse(account), nsfw=nsfw)


async def test_command_tree(bot):
    names = {c.name for c in bot.tree.get_commands()}
    assert names == {"check", "badges", "audit", "setup", "case", "track", "config", "trust", "keywords"}
    for command in bot.tree.get_commands():
        command.to_dict(bot.tree)  # raises if Discord would reject the definition


def embed_size(embed: discord.Embed) -> int:
    return len(embed)


def test_scan_embed_limits():
    long_bio = "horny 18+ add me for fun " * 200
    result = scan_result(description=long_bio, username="HornyAlt99999", age_days=1, badges=0)
    embed = embeds.scan_embed(result)
    assert embed_size(embed) <= 6000
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert embed.colour == embeds.DARK_RED


def test_case_embed_and_view():
    from datetime import datetime, timezone
    from bot.db import Case, Note

    result = scan_result(age_days=2, badges=0)
    case = Case(
        id=7, guild_id=1, roblox_id=123, roblox_name="BuilderKid", reasons=["alt", "manual"], status="open",
        alt_score=80, nsfw_score=0, details={**result.case_details(), "manual_reason": "x" * 2000},
        opened_by=None, opened_at=datetime.now(timezone.utc), claimed_by=55, claimed_at=datetime.now(timezone.utc),
        resolved_by=None, resolved_at=None, resolution=None, review_channel_id=None, review_message_id=None,
        note_count=5,
    )
    notes = [Note(i, 55, "note " * 100, datetime.now(timezone.utc)) for i in range(5)]
    embed = embeds.case_embed(case, notes)
    assert embed_size(embed) <= 6000
    assert embed.footer.text == "Case #7"

    ids = [item.custom_id for item in case_view(case).children if hasattr(item, "custom_id") and item.custom_id]
    assert ids == ["case:release:7", "case:confirm:7", "case:dismiss:7", "case:note:7"]
    case.status = "confirmed"
    ids = [item.custom_id for item in case_view(case).children if getattr(item, "custom_id", None)]
    assert ids == ["case:reopen:7", "case:note:7"]


def test_dynamic_button_templates():
    assert CaseButton.__discord_ui_compiled_template__.fullmatch("case:claim:12")
    assert ProfileButton.__discord_ui_compiled_template__.fullmatch("rbx:track:99")
    view = profile_view(99, show_case=False)
    assert [getattr(i, "custom_id", None) for i in view.children][:2] == ["rbx:track:99", "rbx:trust:99"]


class FakeChannel(discord.abc.Messageable):
    def __init__(self):
        self.id = 500
        self.sent = []

    async def _get_channel(self):
        return self

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return type("Msg", (), {"id": 1000 + len(self.sent)})()


class FakeGuild:
    def __init__(self, channel):
        self.id = 1
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 500 else None

    def get_channel_or_thread(self, channel_id):
        return None  # forces a fresh post, which FakeChannel records


class FakeMember:
    def __init__(self, user_id):
        self.id = user_id


async def test_case_service_flow(bot):
    channel = FakeChannel()
    guild = FakeGuild(channel)
    await bot.db.update_config(1, review_channel_id=500, log_channel_id=500)

    clean = scan_result()
    case, created = await bot.cases.open_from_scan(guild, clean, opened_by=None)
    assert case is None and not created  # nothing to flag

    result = scan_result(age_days=2, badges=0, friends=0, groups=0, description="")
    case, created = await bot.cases.open_from_scan(guild, result, opened_by=None)
    assert created and case.reasons == ["alt"]
    posted = [kwargs for _, kwargs in channel.sent if "embed" in kwargs]
    assert posted and posted[0]["embed"].footer.text == f"Case #{case.id}"
    assert (await bot.db.get_case(1, case.id)).review_message_id is not None

    alice, bob = FakeMember(1), FakeMember(2)
    assert (await bot.cases.act(guild, alice, "claim", case.id)).ok
    denied = await bot.cases.act(guild, bob, "confirm", case.id)
    assert not denied.ok and "claimed by <@1>" in denied.error
    assert (await bot.cases.add_note(guild, bob, case.id, "their friend list is all alts")).ok
    assert (await bot.cases.act(guild, alice, "confirm", case.id)).ok
    again = await bot.cases.act(guild, bob, "dismiss", case.id)
    assert not again.ok and "already **confirmed**" in again.error

    log_lines = [content for content, _ in channel.sent if content]
    assert any("claimed" in line for line in log_lines)
    assert any("confirmed" in line for line in log_lines)

    trusted = scan_result(age_days=2, badges=0)
    trusted.trusted_by = 9
    assert (await bot.cases.open_from_scan(guild, trusted, opened_by=None)) == (None, False)


class FakeResponse:
    def __init__(self):
        self.deferred = None
        self.sent = []

    async def defer(self, **kwargs):
        self.deferred = kwargs

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def is_done(self):
        return self.deferred is not None or bool(self.sent)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild, self.guild_id, self.user = guild, guild.id, user
        self.response, self.followup = FakeResponse(), FakeFollowup()


def use_fake_roblox(bot, client):
    bot.roblox = client
    bot.scanner.roblox = client


async def test_check_command_opens_case_and_reuses_it(bot):
    from datetime import timedelta

    from bot.models import utcnow

    from .test_roblox import USER, FakeRoblox, badge_pages, routes

    two_days_old = {**USER, "created": (utcnow() - timedelta(days=2)).isoformat()}
    use_fake_roblox(
        bot,
        FakeRoblox(
            routes(
                **{
                    "badges.roblox.com/v1/users/42/badges": badge_pages(0),
                    "users.roblox.com/v1/users/42": two_days_old,
                }
            )
        ),
    )
    channel = FakeChannel()
    guild = FakeGuild(channel)
    await bot.db.update_config(1, review_channel_id=500)
    cog = bot.get_cog("Check")

    first = FakeInteraction(guild, FakeMember(1))
    await cog.check.callback(cog, first, "SneakyAlt")
    content, kwargs = first.followup.sent[0]
    assert "Opened **case #1** → https://discord.com/channels/1/500/" in content
    assert kwargs["embed"].title.startswith("Sneaky (@SneakyAlt)")
    assert kwargs["ephemeral"] is True
    assert len([k for _, k in channel.sent if "embed" in k]) == 1  # posted to the review queue

    second = FakeInteraction(guild, FakeMember(2))
    await cog.check.callback(cog, second, "SneakyAlt")
    assert "already has open **case #1**" in second.followup.sent[0][0]
    assert len(await bot.db.list_cases(1, status=None)) == 1


async def test_tracker_reports_new_badges(bot):
    from .test_roblox import FakeRoblox, badge_pages, routes

    client = FakeRoblox(routes(**{"badges.roblox.com/v1/users/42/badges": badge_pages(60)}))
    use_fake_roblox(bot, client)
    channel = FakeChannel()
    guild = FakeGuild(channel)
    await bot.db.update_config(1, tracking_channel_id=500, auto_flag=0)
    tracking = bot.get_cog("Tracking")

    message = await tracking.start_tracking(guild, FakeMember(1), 42, "suspected alt")
    assert "Now tracking **SneakyAlt**" in message
    assert channel.sent == []  # baseline scan is silent

    client.routes["badges.roblox.com/v1/users/42/badges"] = badge_pages(63)
    client._cache.clear()
    entry = await bot.db.get_watch(1, 42)
    await tracking.rescan(guild, entry)
    embed = channel.sent[-1][1]["embed"]
    assert "+3** new badge(s)" in embed.description
    assert (await bot.db.get_watch(1, 42)).last_badge_count == 63
    assert len(await bot.db.snapshots(1, 42)) == 2
