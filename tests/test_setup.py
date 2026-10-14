"""First-run helpers: asking for the token, the invite link, and the /setup command."""

import discord
import pytest

from bot import config
from bot.bot import invite_link

from .test_bot import FakeInteraction, bot  # noqa: F401  (fixture)

TOKEN = "fake-token-for-tests.only." + "0" * 40  # shaped like a token, but not a real one


def test_env_file_helpers(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEV_GUILD_ID=5\nDISCORD_TOKEN=old\n")
    config.save_env_value("DISCORD_TOKEN", "new", env)
    assert env.read_text().splitlines() == ["DEV_GUILD_ID=5", "DISCORD_TOKEN=new"]
    config.forget_env_value("DISCORD_TOKEN", env)
    assert env.read_text().splitlines() == ["DEV_GUILD_ID=5"]


def test_token_shape():
    assert config.looks_like_token(TOKEN)
    assert not config.looks_like_token("my token")
    assert not config.looks_like_token("abc.def.ghi")


def test_first_run_asks_for_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    answers = iter(["oops", f'"{TOKEN}"'])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    settings = config.Settings.from_env()
    assert settings.token == TOKEN
    assert f"DISCORD_TOKEN={TOKEN}" in (tmp_path / ".env").read_text()


def test_missing_token_without_a_window_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="DISCORD_TOKEN is not set"):
        config.Settings.from_env()


def test_invite_link():
    url = invite_link(123)
    assert "client_id=123" in url and "applications.commands" in url
    assert "permissions=85008" in url  # view, send, embed, history, manage channels
    assert url.endswith("&integration_type=0")  # "Add to Server", never "Add to My Apps"


class FakePerms:
    manage_channels = True


class FakeMe:
    id = 900
    guild_permissions = FakePerms()


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name, self.mention = cid, name, f"<#{cid}>"


class FakeSetupGuild:
    id = 1

    def __init__(self):
        self.me = FakeMe()
        self.default_role = object()
        self.created = []
        self.channels = {}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def create_category(self, name, overwrites=None, reason=None):
        self.category_overwrites = overwrites
        return FakeChannel(10, name)

    async def create_text_channel(self, name, category=None, reason=None, topic=None):
        ch = FakeChannel(100 + len(self.created), name)
        self.created.append(name)
        self.channels[ch.id] = ch
        return ch


class FakeRole:
    id = 77
    mention = "<@&77>"


async def test_setup_creates_private_channels_once(bot):  # noqa: F811
    cog = bot.get_cog("Settings")
    guild = FakeSetupGuild()
    first = FakeInteraction(guild, type("U", (), {"id": 5})())
    await cog.setup_cmd.callback(cog, first, FakeRole())
    assert guild.created == ["alt-review", "mod-log", "badge-tracking"]
    assert guild.category_overwrites[guild.default_role].view_channel is False
    cfg = await bot.db.get_config(1)
    assert (cfg.review_channel_id, cfg.log_channel_id, cfg.tracking_channel_id) == (100, 101, 102)
    assert cfg.mod_role_ids == [77]
    assert "All set" in first.followup.sent[0][0]

    again = FakeInteraction(guild, type("U", (), {"id": 5})())
    await cog.setup_cmd.callback(cog, again, None)
    assert len(guild.created) == 3  # nothing duplicated
    assert "Already set up" in again.response.sent[0][0][0]


async def test_setup_without_manage_channels_explains(bot):  # noqa: F811
    cog = bot.get_cog("Settings")
    guild = FakeSetupGuild()
    interaction = FakeInteraction(guild, type("U", (), {"id": 5})(), app_permissions=discord.Permissions.none())
    await cog.setup_cmd.callback(cog, interaction, None)
    assert guild.created == []
    assert "Manage Channels" in interaction.response.sent[0][0][0]


def interaction_member(bot, permissions: int, roles=("777",)):  # noqa: F811
    """A member exactly as a slash command delivers it when the bot can't see the server's roles."""
    from discord.guild import Guild

    state = bot._connection
    guild = Guild._create_unavailable(state=state, guild_id=555, data=None)
    data = {
        "user": {"id": "42", "username": "owner", "discriminator": "0", "avatar": None, "global_name": None},
        "roles": list(roles), "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False,
        "flags": 0, "permissions": str(permissions),
    }
    return discord.Member(data=data, guild=guild, state=state)


async def test_manage_server_is_read_from_discord_not_the_cache(bot):  # noqa: F811
    from bot.permissions import is_admin

    manage_server = discord.Permissions(manage_guild=True).value
    member = interaction_member(bot, manage_server)
    assert member.guild_permissions.manage_guild is False  # the bug: computed from an empty cache
    assert is_admin(member)  # the fix: Discord's own answer
    assert not is_admin(interaction_member(bot, discord.Permissions(send_messages=True).value))


async def test_mod_role_works_before_roles_are_cached(bot):  # noqa: F811
    from bot.permissions import is_moderator

    await bot.db.add_mod_role(555, 777)
    plain = discord.Permissions(send_messages=True).value
    assert await is_moderator(bot, interaction_member(bot, plain, roles=("777",)))
    assert not await is_moderator(bot, interaction_member(bot, plain, roles=("888",)))


async def test_commands_explain_when_the_bot_is_not_in_the_server(bot):  # noqa: F811
    interaction = FakeInteraction(type("G", (), {"id": 999})(), type("U", (), {"id": 5})())
    interaction.client = bot
    assert await bot.tree.interaction_check(interaction) is False
    text = interaction.response.sent[0][0][0]
    assert "not in this server" in text and "Add to Server" in text


def test_commands_are_server_only(bot):  # noqa: F811
    assert bot.tree.allowed_installs.guild and not bot.tree.allowed_installs.user
    payload = bot.tree.get_command("setup").to_dict(bot.tree)
    assert payload.get("integration_types") == [0]
