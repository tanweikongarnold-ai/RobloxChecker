import asyncio
from datetime import timedelta

import pytest

from bot.db import Database
from bot.models import utcnow

GUILD = 1


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


async def open_case(db, roblox_id=10, reasons=("alt",), by=None, details=None):
    return await db.open_case(GUILD, roblox_id, f"user{roblox_id}", list(reasons), 70, 0, details or {}, by)


async def test_config_defaults_and_update(db):
    cfg = await db.get_config(GUILD)
    assert cfg.alt_threshold == 60 and cfg.auto_flag
    await db.update_config(GUILD, review_channel_id=5, alt_threshold=70, auto_flag=0)
    cfg = await db.get_config(GUILD)
    assert (cfg.review_channel_id, cfg.alt_threshold, cfg.auto_flag) == (5, 70, False)
    with pytest.raises(ValueError):
        await db.update_config(GUILD, bogus=3)


async def test_mod_roles(db):
    assert await db.add_mod_role(GUILD, 99)
    assert not await db.add_mod_role(GUILD, 99)
    assert (await db.get_config(GUILD)).mod_role_ids == [99]
    assert await db.remove_mod_role(GUILD, 99)


async def test_only_one_open_case_per_account(db):
    first, created = await open_case(db, reasons=["alt"], details={"manual_reason": "looks off"})
    assert created
    second, created = await open_case(db, reasons=["nsfw"])
    assert not created and second.id == first.id
    assert second.reasons == ["alt", "nsfw"]
    assert second.details["manual_reason"] == "looks off"  # merged, not overwritten


async def test_concurrent_opens_create_one_case(db):
    results = await asyncio.gather(*(open_case(db) for _ in range(10)))
    assert len({case.id for case, _ in results}) == 1
    assert sum(created for _, created in results) == 1


async def test_claims_are_exclusive(db):
    case, _ = await open_case(db)
    outcomes = await asyncio.gather(*(db.claim_case(GUILD, case.id, uid) for uid in (100, 200, 300)))
    assert outcomes.count(True) == 1
    winner = (await db.get_case(GUILD, case.id)).claimed_by
    losers = [uid for uid in (100, 200, 300) if uid != winner]

    # Someone else can't resolve or release a claimed case...
    assert not await db.resolve_case(GUILD, case.id, losers[0], "confirmed")
    assert not await db.release_case(GUILD, case.id, losers[0])
    # ...but an admin can override, and the claimer can release.
    assert await db.release_case(GUILD, case.id, winner)
    assert await db.claim_case(GUILD, case.id, losers[0])
    assert await db.resolve_case(GUILD, case.id, 999, "dismissed", force=True)
    case = await db.get_case(GUILD, case.id)
    assert case.status == "dismissed" and case.resolved_by == 999


async def test_resolve_only_once(db):
    case, _ = await open_case(db)
    outcomes = await asyncio.gather(
        db.resolve_case(GUILD, case.id, 1, "confirmed"), db.resolve_case(GUILD, case.id, 2, "dismissed")
    )
    assert outcomes.count(True) == 1
    with pytest.raises(ValueError):
        await db.resolve_case(GUILD, case.id, 1, "banana")


async def test_new_case_after_resolve_and_reopen_conflict(db):
    old, _ = await open_case(db)
    await db.resolve_case(GUILD, old.id, 1, "dismissed")
    new, created = await open_case(db)
    assert created and new.id != old.id
    assert not await db.reopen_case(GUILD, old.id)  # account already has an open case
    await db.resolve_case(GUILD, new.id, 1, "confirmed")
    assert await db.reopen_case(GUILD, old.id)
    reopened = await db.get_case(GUILD, old.id)
    assert reopened.is_open and reopened.resolved_by is None


async def test_cases_are_scoped_to_guild(db):
    case, _ = await open_case(db)
    assert await db.get_case(GUILD + 1, case.id) is None
    assert not await db.claim_case(GUILD + 1, case.id, 5)


async def test_notes_lists_and_stats(db):
    a, _ = await open_case(db, roblox_id=1)
    b, _ = await open_case(db, roblox_id=2)
    await db.claim_case(GUILD, a.id, 7)
    await db.add_note(a.id, 7, "checking their friends list")
    await db.resolve_case(GUILD, b.id, 8, "confirmed")

    assert [n.body for n in await db.get_notes(a.id)] == ["checking their friends list"]
    assert (await db.get_case(GUILD, a.id)).note_count == 1
    assert [c.id for c in await db.list_cases(GUILD, unclaimed=True)] == []
    assert [c.id for c in await db.list_cases(GUILD, claimed_by=7)] == [a.id]
    assert [c.id for c in await db.list_cases(GUILD, status=None)] == [b.id, a.id]
    assert [c.id for c in await db.search_case_ids(GUILD, "user2", open_only=False)] == [b.id]
    assert await db.case_counts(GUILD) == {"open": 1, "unclaimed": 0, "confirmed": 1, "dismissed": 0}

    stats = {uid: (resolved, claims, notes) for uid, resolved, claims, notes in await db.moderator_stats(GUILD)}
    assert stats[7] == (0, 1, 1)
    assert stats[8] == (1, 0, 0)


async def test_watchlist_flow(db):
    assert await db.add_watch(GUILD, 50, "watched", 1, "alt of a banned player")
    assert not await db.add_watch(GUILD, 50, "watched", 2, None)
    due = await db.due_watches(utcnow(), 10, [GUILD])
    assert [e.roblox_id for e in due] == [50]
    assert await db.due_watches(utcnow(), 10, [GUILD + 1]) == []

    await db.add_known_badges(GUILD, 50, [1, 2, 3])
    assert await db.known_badges(GUILD, 50) == {1, 2, 3}
    await db.record_watch_scan(GUILD, 50, "renamed", 3, 0, 40, 0)
    assert await db.due_watches(utcnow() - timedelta(minutes=5), 10, [GUILD]) == []
    entry = await db.get_watch(GUILD, 50)
    assert entry.roblox_name == "renamed" and entry.last_badge_count == 3
    assert len(await db.snapshots(GUILD, 50)) == 1

    assert await db.remove_watch(GUILD, 50)
    assert await db.known_badges(GUILD, 50) == set()


async def test_trusted_and_terms(db):
    await db.add_trusted(GUILD, 9, "goodkid", 1, "verified main")
    await db.add_trusted(GUILD, 9, "goodkid", 1, "updated reason")
    assert (await db.get_trusted(GUILD, 9))["reason"] == "updated reason"
    assert len(await db.list_trusted(GUILD)) == 1
    assert await db.remove_trusted(GUILD, 9)

    await db.set_term(GUILD, "bad club", 50, "word", "custom", 1)
    await db.set_term(GUILD, "bad club", 0, "word", "custom", 2)
    rows = await db.list_terms(GUILD)
    assert len(rows) == 1 and rows[0]["weight"] == 0
    assert await db.remove_term(GUILD, "bad club")


async def test_audit_log(db):
    await db.log(GUILD, 1, "case.claim", "case #1")
    await db.log(GUILD, 2, "case.note", "case #1", "hi")
    assert [r["action"] for r in await db.recent_audit(GUILD)] == ["case.note", "case.claim"]
    assert [r["actor_id"] for r in await db.recent_audit(GUILD, actor_id=1)] == [1]
