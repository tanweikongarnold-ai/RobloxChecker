"""/groupscan against a fake Roblox group (no network)."""

import re
from datetime import timedelta

import pytest

from bot.embeds import group_scan_embed
from bot.group_scan import scan_group
from bot.models import utcnow
from bot.roblox import GroupNotFound, RobloxClient, _HTTPStatus

from .test_bot import FakeChannel, FakeFollowup, FakeGuild, FakeInteraction, FakeResponse, bot, use_fake_roblox  # noqa: F401

GROUP = {"id": 555, "name": "Cool Builders", "description": "A group for builders", "memberCount": 6000,
         "owner": {"userId": 1, "username": "Boss"}, "hasVerifiedBadge": False}


def person(uid, name, *, age_days, badges, games=10, friends=10, groups=1, bio=""):
    return {"id": uid, "name": name, "displayName": name, "age": age_days, "badges": badges, "games": games,
            "friends": friends, "groups": groups, "bio": bio}


PEOPLE = {
    101: person(101, "GoodMain", age_days=1500, badges=150, games=60, friends=80, groups=4, bio="I build obbies"),
    102: person(102, "Horny_Kid", age_days=300, badges=40, friends=20, bio="add me 4 fun 18+"),
    103: person(103, "Player84721", age_days=2, badges=0, friends=0, groups=0),
    105: person(105, "TrustedAlt9999", age_days=3, badges=0, friends=0, groups=0),
    106: person(106, "MaybeAlt", age_days=60, badges=3, friends=3, bio="hi"),
}
MEMBER_IDS = [101, 102, 103, 104, 105, 106]  # 104 has been deleted


class FakeRobloxGroups(RobloxClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def _request(self, method, url, *, params=None, json=None):
        params = params or {}
        self.calls.append(url)
        now = utcnow()
        if url.endswith("/v1/groups/search/lookup"):
            hit = params["groupName"].lower() == "cool builders"
            return {"data": [{"id": 555, "name": "Cool Builders", "memberCount": 6000}] if hit else []}
        if m := re.search(r"/v1/groups/(\d+)/users$", url):
            ids = MEMBER_IDS[:3] if not params.get("cursor") else MEMBER_IDS[3:]
            data = [{"user": {"userId": i, "username": PEOPLE.get(i, {}).get("name", f"gone{i}"), "displayName": ""},
                     "role": {"name": "Member"}} for i in ids]
            return {"data": data, "nextPageCursor": None if params.get("cursor") else "page2"}
        if m := re.search(r"/v1/groups/(\d+)$", url):
            if int(m.group(1)) != 555:
                raise _HTTPStatus(400, url)
            return GROUP
        m = re.search(r"/users/(\d+)", url)
        uid = int(m.group(1)) if m else None
        p = PEOPLE.get(uid)
        if url.endswith(f"/v1/users/{uid}"):
            if not p:
                raise _HTTPStatus(404, url)
            created = (now - timedelta(days=p["age"])).isoformat().replace("+00:00", "Z")
            return {"id": uid, "name": p["name"], "displayName": p["displayName"], "description": p["bio"],
                    "created": created, "isBanned": False, "hasVerifiedBadge": False}
        if "/badges/awarded-dates" in url:
            ids = [int(x) for x in params["badgeIds"].split(",")]
            return {"data": [{"badgeId": b, "awardedDate": (now - timedelta(days=p["age"]) + timedelta(days=1 + i * p["age"] // max(1, len(ids)))).isoformat()}
                             for i, b in enumerate(ids)]}
        if url.endswith("/badges"):
            start = int(params.get("cursor") or 0)
            total = p["badges"]
            data = [{"id": uid * 1000 + i, "name": f"B{i}", "awarder": {"id": i % p["games"]}} for i in range(start, min(total, start + 100))]
            return {"data": data, "nextPageCursor": str(start + 100) if start + 100 < total else None}
        if url.endswith("/friends/count"):
            return {"count": p["friends"]}
        if url.endswith(("/followers/count", "/followings/count")):
            return {"count": 0}
        if url.endswith("/groups/roles"):
            return {"data": [{"group": {"id": g, "name": f"Club {g}"}, "role": {"name": "Member"}} for g in range(p["groups"])]}
        if "username-history" in url:
            return {"data": []}
        if "roblox-badges" in url:
            return []
        if "avatar-headshot" in url and params.get("userIds"):
            ids = [int(i) for i in str(params["userIds"]).split(",")]
            return {"data": [{"targetId": i, "state": "Completed", "imageUrl": None} for i in ids if i in PEOPLE]}
        if "avatar-headshot" in url:
            return {"data": [{"targetId": uid, "state": "Completed", "imageUrl": None}]}
        if "can-view-inventory" in url:
            return {"canView": True}
        raise AssertionError(f"unexpected request {url}")


@pytest.mark.parametrize(
    "query",
    ["https://www.roblox.com/groups/555/Cool-Builders", "https://www.roblox.com/communities/555/Cool-Builders#!/about",
     "roblox.com/es/communities/555", "555", "id:555", "Cool Builders", "cool builders"],
)
async def test_resolve_group(query):
    assert await FakeRobloxGroups().resolve_group_id(query) == 555


async def test_unknown_group():
    client = FakeRobloxGroups()
    with pytest.raises(GroupNotFound):
        await client.resolve_group_id("Nobody Group")
    with pytest.raises(GroupNotFound):
        await client.get_group(999)


async def test_members_are_paged_and_capped():
    client = FakeRobloxGroups()
    assert [m.user_id for m in await client.get_group_members(555, 200)] == MEMBER_IDS
    assert [m.user_id for m in await client.get_group_members(555, 2)] == [101, 102]


async def test_scan_sorts_members_into_groups(bot):  # noqa: F811
    use_fake_roblox(bot, FakeRobloxGroups())
    await bot.db.add_trusted(1, 105, "TrustedAlt9999", 9, "staff test account")
    seen = []

    async def progress(group, stage, done, total):
        seen.append((group.name, stage, done, total))

    report = await scan_group(bot, 1, "https://www.roblox.com/communities/555", 50, progress)
    assert seen[0] == ("Cool Builders", "quick", 0, 6)
    assert ("Cool Builders", "quick", 6, 6) in seen
    # GoodMain (old, lots of badges) passes the quick check; the other four get a closer look.
    assert seen[-1] == ("Cool Builders", "full", 4, 4)
    assert [s.member.user_id for s in report.nsfw] == [102]
    assert [s.member.user_id for s in report.alts] == [103]
    assert [s.member.user_id for s in report.suspicious] == [106]
    assert [s.member.user_id for s in report.trusted] == [105]
    assert [s.member.user_id for s in report.failed] == [104]
    assert report.clean_count == 1  # GoodMain
    # Bulk scans read one badge page, so big collections show as "100+".
    good = next(s for s in report.scans if s.member.user_id == 101)
    assert len(good.result.account.badges) == 100 and good.result.account.badges_truncated
    assert good.depth == "quick" and report.full_checks == 4

    embed = group_scan_embed(report, {103: 7})
    assert len(embed) <= 6000 and all(len(f.value) <= 1024 for f in embed.fields)
    assert "Checked the newest **6** of **6,000** members" in embed.description
    fields = {f.name: f.value for f in embed.fields}
    assert "users/102/profile" in fields["🔞 NSFW accounts"] and "Horny\\_Kid" in fields["🔞 NSFW accounts"]
    assert "Player84721" in fields["🕵️ Likely alts"] and "case #7" in fields["🕵️ Likely alts"]
    assert "MaybeAlt" in fields["⚠️ Suspicious, below your thresholds (alt/NSFW)"]


class ScanInteraction(FakeInteraction):
    def __init__(self, guild, user):
        super().__init__(guild, user)
        self.edits = []

        async def edit_message(**kwargs):
            self.edits.append(kwargs)

        self.response.edit_message = edit_message

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


async def test_groupscan_command_and_open_cases_button(bot):  # noqa: F811
    use_fake_roblox(bot, FakeRobloxGroups())
    channel = FakeChannel()
    guild = FakeGuild(channel)
    await bot.db.update_config(1, review_channel_id=500)
    await bot.db.add_trusted(1, 105, "TrustedAlt9999", 9, "staff test account")
    cog = bot.get_cog("Groups")

    run = ScanInteraction(guild, type("U", (), {"id": 5})())
    await cog.groupscan.callback(cog, run, "Cool Builders", 50, False)
    assert run.response.deferred == {"ephemeral": True, "thinking": True}
    assert any("quick check on every member" in (e.get("content") or "") for e in run.edits)
    assert any("full check on the 4 that looked off" in (e.get("content") or "") for e in run.edits)
    assert "full check on the **4** that looked off" in run.edits[-1]["embed"].description
    final = run.edits[-1]
    assert final["content"] is None and final["embed"].title == "Group scan · Cool Builders"
    view = final["view"]
    assert view.open_button.label == "Open cases for 2 flagged"

    click = ScanInteraction(guild, type("U", (), {"id": 6})())
    await view.open_cases(click)
    cases = await bot.db.list_cases(1, status="open")
    assert sorted(c.roblox_id for c in cases) == [102, 103]
    for case in cases:
        notes = await bot.db.get_notes(case.id)
        assert notes[0].body.startswith("Found by a scan of the Roblox group “Cool Builders”")
        assert notes[0].author_id == 6
    assert click.edits[-1]["view"].open_button.disabled
    assert "Opened 2 new case(s)" in click.followup.sent[-1][0]

    # Pressing it again (or scanning again) doesn't make duplicates.
    again = ScanInteraction(guild, type("U", (), {"id": 6})())
    view.open_button.disabled = False
    await view.open_cases(again)
    assert len(await bot.db.list_cases(1, status="open")) == 2
    assert "2 already had an open case" in again.followup.sent[-1][0]


async def test_fast_scan_matches_thorough_with_far_fewer_requests(bot):  # noqa: F811
    async def run(thorough):
        client = FakeRobloxGroups()
        use_fake_roblox(bot, client)
        report = await scan_group(bot, 1, "555", 50, thorough=thorough)
        groups = (sorted(s.member.user_id for s in report.nsfw), sorted(s.member.user_id for s in report.alts),
                  sorted(s.member.user_id for s in report.suspicious))
        return client.calls, groups, report

    fast_calls, fast_groups, fast = await run(False)
    slow_calls, slow_groups, slow = await run(True)
    assert fast_groups == slow_groups  # same answer
    assert slow.full_checks == 5 and fast.full_checks == 4
    # The clean member never had their friends, groups or badge dates looked up.
    assert not any("/users/101/friends" in c or "/users/101/groups" in c for c in fast_calls)
    assert any("/users/101/friends" in c for c in slow_calls)
    # Avatars come in one request for the whole group.
    assert sum("avatar-headshot" in c for c in fast_calls) == 1
    assert len(fast_calls) < len(slow_calls)


async def test_too_many_requests_pauses_everyone(monkeypatch):
    """After a 429, later requests wait for the same cooldown instead of hammering Roblox."""
    import asyncio

    from bot import roblox as roblox_module

    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        slept.append(round(seconds))
        await real_sleep(0)

    monkeypatch.setattr(roblox_module.asyncio, "sleep", fake_sleep)

    class Resp:
        def __init__(self, status, body=None, headers=None):
            self.status, self.body, self.headers = status, body or {}, headers or {}

        async def json(self, content_type=None):
            return self.body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        closed = False

        def __init__(self):
            self.replies = [Resp(429, headers={"Retry-After": "5"}), Resp(200, {"ok": 1}), Resp(200, {"ok": 2})]

        def request(self, method, url, params=None, json=None):
            return self.replies.pop(0)

    client = RobloxClient()
    client._session = Session()
    assert await client._request("GET", "https://users.roblox.com/a") == {"ok": 1}
    assert slept == [5]  # waited out the Retry-After
    # A brand-new request still respects the cooldown (the fake clock never moved).
    assert await client._request("GET", "https://users.roblox.com/b") == {"ok": 2}
    assert slept[-1] == 5
