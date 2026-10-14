"""Roblox client parsing, tested against canned API responses (no network)."""

import asyncio

import pytest

from bot.db import Database
from bot.roblox import RobloxClient, UserNotFound, _HTTPStatus
from bot.scanner import FilterRegistry, Scanner

USER = {
    "id": 42,
    "name": "SneakyAlt",
    "displayName": "Sneaky",
    "description": "",
    "created": "2026-09-28T10:00:00.123Z",
    "isBanned": False,
    "hasVerifiedBadge": False,
}


class FakeRoblox(RobloxClient):
    def __init__(self, routes, **kwargs):
        super().__init__(**kwargs)
        self.routes = routes
        self.calls = []

    async def _request(self, method, url, *, params=None, json=None):
        self.calls.append((method, url, params, json))
        await asyncio.sleep(0)
        for fragment, response in self.routes.items():
            if fragment in url:
                result = response(params, json) if callable(response) else response
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected request {url}")


def badge_pages(total):
    def respond(params, _json):
        start = int(params.get("cursor") or 0)
        data = [
            {"id": i, "name": f"Badge {i}", "awarder": {"id": i % 3, "type": "Place"}}
            for i in range(start, min(total, start + params["limit"]))
        ]
        nxt = start + params["limit"]
        return {"data": data, "nextPageCursor": str(nxt) if nxt < total else None}

    return respond


def routes(**overrides):
    base = {
        "/badges/awarded-dates": lambda params, _: {
            "data": [{"badgeId": int(b), "awardedDate": "2026-09-29T12:00:00Z"} for b in params["badgeIds"].split(",")]
        },
        "badges.roblox.com/v1/users/42/badges": badge_pages(250),
        "friends/count": {"count": 0},
        "followers/count": {"count": 1},
        "followings/count": {"count": 2},
        "groups/roles": {"data": [{"group": {"id": 7, "name": "Cool Group", "memberCount": 10}, "role": {"name": "Member", "rank": 1}}]},
        "username-history": {"data": [{"name": "OldName"}, {"name": "SneakyAlt"}]},
        "roblox-badges": [{"id": 1, "name": "Veteran"}],
        "avatar-headshot": {"data": [{"targetId": 42, "state": "Completed", "imageUrl": "https://img"}]},
        "can-view-inventory": {"canView": True},
        "users.roblox.com/v1/users/42": USER,
        "usernames/users": lambda _p, body: {"data": [{"id": 42}] if body["usernames"] == ["SneakyAlt"] else []},
        "users.roblox.com/v1/users/": _HTTPStatus(404, "x"),
    }
    base.update(overrides)
    return base


async def test_fetch_account_parses_everything():
    client = FakeRoblox(routes())
    account = await client.fetch_account(42)
    assert account.username == "SneakyAlt" and account.display_name == "Sneaky"
    assert account.created.year == 2026
    assert len(account.badges) == 250 and not account.badges_hidden and not account.badges_truncated
    assert all(b.awarded_at is not None for b in account.badges)
    assert (account.friends, account.followers, account.following) == (0, 1, 2)
    assert account.groups[0].name == "Cool Group"
    assert account.username_history == ["OldName"]  # current name filtered out
    assert account.legacy_badges == ["Veteran"]
    assert account.avatar_state == "Completed"


async def test_badges_are_capped():
    client = FakeRoblox(routes(), max_badges=100)
    account = await client.fetch_account(42)
    assert len(account.badges) == 100 and account.badges_truncated


async def test_private_badges():
    client = FakeRoblox(routes(**{"badges.roblox.com/v1/users/42/badges": _HTTPStatus(403, "x")}))
    account = await client.fetch_account(42)
    assert account.badges_hidden and account.badges == []


async def test_empty_badges_with_private_inventory_counts_as_hidden():
    client = FakeRoblox(
        routes(
            **{
                "badges.roblox.com/v1/users/42/badges": {"data": [], "nextPageCursor": None},
                "can-view-inventory": {"canView": False},
            }
        )
    )
    assert (await client.fetch_account(42)).badges_hidden


async def test_optional_failures_do_not_break_scan():
    client = FakeRoblox(routes(**{"friends/count": _HTTPStatus(500, "x"), "groups/roles": _HTTPStatus(500, "x")}))
    account = await client.fetch_account(42)
    assert account.friends is None and account.groups is None


@pytest.mark.parametrize(
    "query",
    ["SneakyAlt", "@SneakyAlt", "42", "id:42", "https://www.roblox.com/users/42/profile"],
)
async def test_resolve_user_id(query):
    assert await FakeRoblox(routes()).resolve_user_id(query) == 42


async def test_resolve_unknown_user():
    with pytest.raises(UserNotFound):
        await FakeRoblox(routes()).resolve_user_id("NobodyHere")
    with pytest.raises(UserNotFound):
        await FakeRoblox(routes()).resolve_user_id("not a name!")


async def test_scanner_shares_concurrent_fetches(tmp_path):
    db = Database(tmp_path / "s.db")
    await db.connect()
    client = FakeRoblox(routes())
    scanner = Scanner(client, db, FilterRegistry(db))
    results = await asyncio.gather(*(scanner.scan(1, 42) for _ in range(5)))
    user_calls = [c for c in client.calls if c[1].endswith("/v1/users/42")]
    assert len(user_calls) == 1
    assert {r.account.user_id for r in results} == {42}
    assert results[0].alt.signals and results[0].nsfw.score == 0
    await db.close()
