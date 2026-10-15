"""The three /check pages: friends quick-check and groups & badges (no network)."""

import asyncio
import re
from datetime import timedelta


from bot import embeds, friends
from bot.check_pages import CheckPagesView
from bot.friends import FriendChecker, analyse_friends
from bot.models import Group, utcnow
from bot.roblox import RobloxClient, _HTTPStatus

from .test_bot import FakeChannel, FakeGuild, FakeInteraction, bot, scan_result, use_fake_roblox  # noqa: F401

MAIN = 42
NSFW_FRIENDS = [301, 302]
ALT_FRIENDS = list(range(310, 322))  # 12 brand-new, badge-less accounts
NORMAL_FRIENDS = list(range(400, 424))
DELETED = 999
TRUSTED_ALT = 330
FRIEND_IDS = NSFW_FRIENDS + ALT_FRIENDS + [TRUSTED_ALT] + NORMAL_FRIENDS + [DELETED]


def profile(uid):
    if uid in NSFW_FRIENDS:
        return {"name": f"Freaky{uid}", "bio": "add me 4 fun 18+ horny", "age": 200, "badges": 30}
    if uid in ALT_FRIENDS or uid == TRUSTED_ALT:
        return {"name": f"Player{uid}0000", "bio": "", "age": 2, "badges": 0}
    if uid in NORMAL_FRIENDS:
        return {"name": f"Builder{uid}", "bio": "obbies!", "age": 1500, "badges": 150}
    return None


class FakeRobloxFriends(RobloxClient):
    def __init__(self, *, find_works=True):
        super().__init__()
        self.find_works = find_works
        self.calls = []

    async def _request(self, method, url, *, params=None, json=None):
        params = params or {}
        self.calls.append(url)
        await asyncio.sleep(0)
        now = utcnow()
        if url.endswith(f"/v1/users/{MAIN}/friends/find"):
            if not self.find_works:
                raise _HTTPStatus(404, url)
            start = int(params.get("cursor") or 0)
            items = [{"id": i} for i in FRIEND_IDS[start : start + 50]]
            nxt = str(start + 50) if start + 50 < len(FRIEND_IDS) else None
            return {"PreviousCursor": None, "PageItems": items, "NextCursor": nxt, "HasMore": nxt is not None}
        if url.endswith(f"/v1/users/{MAIN}/friends"):
            return {"data": [{"id": i, "name": "", "displayName": ""} for i in FRIEND_IDS]}
        if url.endswith("users.roblox.com/v1/users") and method == "POST":
            return {"data": [{"id": i, "name": f"Later{i}", "displayName": f"Later {i}"} for i in json["userIds"]]}
        uid = int(re.search(r"/users/(\d+)", url).group(1))
        p = profile(uid)
        if url.endswith(f"/v1/users/{uid}"):
            if not p:
                raise _HTTPStatus(404, url)
            created = (now - timedelta(days=p["age"])).isoformat()
            return {"id": uid, "name": p["name"], "displayName": p["name"], "description": p["bio"], "created": created,
                    "isBanned": False, "hasVerifiedBadge": False}
        if url.endswith("/badges"):
            n = p["badges"]
            return {"data": [{"id": uid * 1000 + i, "name": f"B{i}", "awarder": {"id": i % 20}} for i in range(min(n, 100))],
                    "nextPageCursor": "more" if n > 100 else None}
        if "can-view-inventory" in url:
            return {"canView": True}
        raise AssertionError(f"unexpected request {method} {url}")


async def test_friend_ids_new_and_old_endpoints():
    assert await FakeRobloxFriends().get_friend_ids(MAIN) == FRIEND_IDS
    assert await FakeRobloxFriends(find_works=False).get_friend_ids(MAIN) == FRIEND_IDS
    assert await FakeRobloxFriends().get_friend_ids(MAIN, limit=5) == FRIEND_IDS[:5]


async def test_brief_check_marks_private_inventories():
    class Private(FakeRobloxFriends):
        async def _request(self, method, url, **kw):
            if "can-view-inventory" in url:
                return {"canView": False}
            return await super()._request(method, url, **kw)

    account = await Private().fetch_brief(310)
    assert account.badges == [] and account.badges_hidden
    assert account.friends is None and account.groups is None  # unknown, not zero
    assert not (await FakeRobloxFriends().fetch_brief(310)).badges_hidden


async def test_checker_shares_work_caps_and_names_the_rest(monkeypatch):
    monkeypatch.setattr(friends, "FRIEND_CHECK_LIMIT", 30)
    client = FakeRobloxFriends()
    checker = FriendChecker(client)
    first, second = await asyncio.gather(checker.fetch(MAIN), checker.fetch(MAIN))
    assert first is second  # two moderators, one set of Roblox calls
    assert first.total == len(FRIEND_IDS)
    assert len(first.accounts) == 30 and first.failed == 0
    assert [u[0] for u in first.unchecked] == FRIEND_IDS[30:]
    assert first.unchecked[0][1] == f"Later{FRIEND_IDS[30]}"
    calls = len(client.calls)
    await checker.fetch(MAIN)  # cached
    assert len(client.calls) == calls
    assert checker.progress(MAIN) is None


async def test_report_sorts_and_flags(bot):  # noqa: F811
    use_fake_roblox(bot, FakeRobloxFriends())
    await bot.db.add_trusted(1, TRUSTED_ALT, f"Player{TRUSTED_ALT}0000", 9, "staff alt")
    case, _ = await bot.db.open_case(1, 301, "Freaky301", ["nsfw"], 0, 80, {}, None)
    data = await bot.friends.fetch(MAIN)
    report = await analyse_friends(bot, 1, data)
    kinds = [c.kind for c in report.checks]
    assert kinds == sorted(kinds, key=friends.KIND_ORDER.get)
    assert report.count("nsfw") == 2 and report.count("alt") == 12 and report.count("trusted") == 1
    assert report.count("ok") == len(NORMAL_FRIENDS) and data.failed == 1  # the deleted account
    assert next(c for c in report.checks if c.account.user_id == 301).open_case == case.id
    assert len(report.flagged) == 14

    page = embeds.friends_embed(report.checks[0].account, report)  # any account works for the header
    assert len(page) <= 6000 and len(page.description) <= 4096
    assert "**40** friends" in page.description and "**2** NSFW" in page.description
    assert "of their friends look like alts" in page.description  # 12 of 39 judged
    assert "🔞" in page.description.split("\n\n", 1)[1].splitlines()[0]  # flagged friends come first
    assert embeds.friends_page_count(report) == 3  # 39 checked rows, 15 per page
    last = embeds.friends_embed(report.checks[0].account, report, sub_page=2)
    assert "friends 3/3" in last.footer.text


def test_groups_and_badges_page():
    from bot.content_filter import ContentFilter

    result = scan_result(age_days=900, badges=60)
    result.account.groups = [Group(id=1, name="Condo Hangout", role="Member", member_count=50),
                             Group(id=2, name="Builders Club", role="Admin", member_count=12000)]
    result.account.username_history = ["OldName"]
    cf = ContentFilter.load()
    hits = {g.id: cf.scan_text(g.name, "group") for g in result.account.groups}
    page = embeds.groups_badges_embed(result, hits)
    fields = {f.name: f.value for f in page.fields}
    groups_field = next(v for k, v in fields.items() if k.startswith("Groups (2)"))
    assert groups_field.splitlines()[0].startswith("🔞 [Condo Hangout]")
    assert "12,000 members" in groups_field
    assert "**60** badges" in fields["Badges"]
    assert len(fields["Most recent badges"].splitlines()) == 10
    assert fields["Past usernames (1)"] == "OldName"
    assert len(page) <= 6000


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class ClickInteraction(FakeInteraction):
    def __init__(self, guild, user_id):
        super().__init__(guild, type("U", (), {"id": user_id})())
        self.edits = []

        async def edit_message(**kwargs):
            self.edits.append(kwargs)

        self.response.edit_message = edit_message


def labels(view):
    return [item.label for item in view.children]


async def test_three_pages_and_open_cases_for_friends(bot):  # noqa: F811
    use_fake_roblox(bot, FakeRobloxFriends())
    guild = FakeGuild(FakeChannel())
    await bot.db.update_config(1, review_channel_id=500)
    await bot.db.add_trusted(1, TRUSTED_ALT, f"Player{TRUSTED_ALT}0000", 9, "staff alt")
    main = scan_result(age_days=900, badges=60)
    main.account.user_id = MAIN
    view = await CheckPagesView.create(bot, guild, main, None, show_case=True, show_track=True, show_trust=True)
    view.message = FakeMessage()
    assert view.embed().footer.text.startswith("Page 1/3")
    assert labels(view)[:3] == ["Overview", "Friends ⏳", "Groups & badges"]

    view.start()
    await view._loader
    assert labels(view)[1] == "Friends (40) · 14 flagged"
    assert view.message.edits  # the finished check redrew the message

    await view._go("friends")(ClickInteraction(guild, 5))
    assert view.embed().footer.text.startswith("Page 2/3")
    assert "Open cases for 14 flagged friend(s)" in labels(view)
    click = ClickInteraction(guild, 5)
    await view._flip(1)(click)
    assert view.sub_page == 1 and "friends 2/3" in click.edits[-1]["embed"].footer.text

    await view._go("groups")(ClickInteraction(guild, 5))
    assert view.embed().title.startswith("Groups & badges")
    assert "Open cases for 14 flagged friend(s)" not in labels(view)

    view.page = "friends"
    opener = ClickInteraction(guild, 6)
    await view._open_friend_cases(opener)
    cases = await bot.db.list_cases(1, status="open", limit=100)
    assert len(cases) == 14
    note = (await bot.db.get_notes(cases[0].id))[0]
    assert note.body.startswith(f"Found among the friends of @{main.account.username}") and note.author_id == 6
    assert "Opened 14 new case(s)" in opener.followup.sent[-1][0]
    assert not any((label or "").startswith("Open cases") for label in labels(view))  # all done

    await view.on_timeout()
    final = view.message.edits[-1]["view"]
    assert [getattr(i, "custom_id", None) for i in final.children][:3] == [f"rbx:case:{MAIN}", f"rbx:track:{MAIN}", f"rbx:trust:{MAIN}"]


async def test_friends_page_shows_errors(bot):  # noqa: F811
    class Broken(FakeRobloxFriends):
        async def _request(self, method, url, **kw):
            if "/friends" in url:
                raise _HTTPStatus(500, url)
            return await super()._request(method, url, **kw)

    use_fake_roblox(bot, Broken())
    main = scan_result()
    main.account.user_id = MAIN
    view = await CheckPagesView.create(bot, FakeGuild(FakeChannel()), main, None, show_case=False, show_track=False, show_trust=False)
    view.start()
    await view._loader
    assert labels(view)[1] == "Friends ⚠️"
    view.page = "friends"
    assert view.embed().description.startswith("Couldn't load their friends")
