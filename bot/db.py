"""Shared SQLite storage.

Everything moderators do lives here so the whole team sees the same state:
cases (with claims and notes), the audit log, the watchlist, trusted accounts,
custom filter terms and per-server settings.

State changes that several moderators could race on (opening a case, claiming,
resolving) are single conditional SQL statements, so two people clicking the
same button at once can never both "win".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import aiosqlite

from .models import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id             INTEGER PRIMARY KEY,
    review_channel_id    INTEGER,
    log_channel_id       INTEGER,
    tracking_channel_id  INTEGER,
    alt_threshold        INTEGER NOT NULL DEFAULT 60,
    suspicious_threshold INTEGER NOT NULL DEFAULT 35,
    nsfw_threshold       INTEGER NOT NULL DEFAULT 40,
    auto_flag            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS mod_roles (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS cases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    roblox_id         INTEGER NOT NULL,
    roblox_name       TEXT    NOT NULL,
    reasons           TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'open',
    alt_score         INTEGER,
    nsfw_score        INTEGER,
    details           TEXT    NOT NULL DEFAULT '{}',
    opened_by         INTEGER,
    opened_at         TEXT    NOT NULL,
    claimed_by        INTEGER,
    claimed_at        TEXT,
    resolved_by       INTEGER,
    resolved_at       TEXT,
    resolution        TEXT,
    review_channel_id INTEGER,
    review_message_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_case_per_account
    ON cases (guild_id, roblox_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS cases_by_status ON cases (guild_id, status);

CREATE TABLE IF NOT EXISTS case_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    INTEGER NOT NULL REFERENCES cases (id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    actor_id   INTEGER,
    action     TEXT    NOT NULL,
    target     TEXT,
    details    TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_by_guild ON audit_log (guild_id, id);

CREATE TABLE IF NOT EXISTS watchlist (
    guild_id         INTEGER NOT NULL,
    roblox_id        INTEGER NOT NULL,
    roblox_name      TEXT    NOT NULL,
    note             TEXT,
    added_by         INTEGER NOT NULL,
    added_at         TEXT    NOT NULL,
    last_checked_at  TEXT,
    last_badge_count INTEGER,
    last_alt_score   INTEGER,
    last_nsfw_score  INTEGER,
    PRIMARY KEY (guild_id, roblox_id)
);

CREATE TABLE IF NOT EXISTS badge_snapshots (
    guild_id    INTEGER NOT NULL,
    roblox_id   INTEGER NOT NULL,
    taken_at    TEXT    NOT NULL,
    badge_count INTEGER NOT NULL,
    new_badges  INTEGER NOT NULL,
    alt_score   INTEGER NOT NULL,
    nsfw_score  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_by_account ON badge_snapshots (guild_id, roblox_id, taken_at);

CREATE TABLE IF NOT EXISTS known_badges (
    guild_id  INTEGER NOT NULL,
    roblox_id INTEGER NOT NULL,
    badge_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, roblox_id, badge_id)
);

CREATE TABLE IF NOT EXISTS trusted_accounts (
    guild_id    INTEGER NOT NULL,
    roblox_id   INTEGER NOT NULL,
    roblox_name TEXT    NOT NULL,
    reason      TEXT,
    added_by    INTEGER NOT NULL,
    added_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, roblox_id)
);

CREATE TABLE IF NOT EXISTS players (
    guild_id        INTEGER NOT NULL,
    roblox_id       INTEGER NOT NULL,
    username        TEXT    NOT NULL,
    display_name    TEXT,
    account_created TEXT,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    verified        INTEGER NOT NULL DEFAULT 0,
    badges          INTEGER,
    badges_more     INTEGER NOT NULL DEFAULT 0,
    friends         INTEGER,
    followers       INTEGER,
    groups_count    INTEGER,
    bio             TEXT,
    alt_score       INTEGER NOT NULL,
    alt_verdict     TEXT    NOT NULL,
    nsfw_score      INTEGER NOT NULL,
    nsfw_matches    TEXT,
    flags           TEXT    NOT NULL DEFAULT '',
    depth           TEXT    NOT NULL,
    found_via       TEXT    NOT NULL,
    first_seen      TEXT    NOT NULL,
    last_checked    TEXT    NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    last_checked_by INTEGER,
    PRIMARY KEY (guild_id, roblox_id)
);
CREATE INDEX IF NOT EXISTS players_by_checked ON players (guild_id, last_checked);

CREATE TABLE IF NOT EXISTS roblox_groups (
    guild_id           INTEGER NOT NULL,
    group_id           INTEGER NOT NULL,
    name               TEXT    NOT NULL,
    member_count       INTEGER,
    owner              TEXT,
    nsfw_score         INTEGER NOT NULL DEFAULT 0,
    nsfw_matches       TEXT,
    members_checked    INTEGER NOT NULL DEFAULT 0,
    nsfw_members       INTEGER NOT NULL DEFAULT 0,
    alt_members        INTEGER NOT NULL DEFAULT 0,
    suspicious_members INTEGER NOT NULL DEFAULT 0,
    first_scanned      TEXT    NOT NULL,
    last_scanned       TEXT    NOT NULL,
    times_scanned      INTEGER NOT NULL DEFAULT 1,
    last_scanned_by    INTEGER,
    PRIMARY KEY (guild_id, group_id)
);

CREATE TABLE IF NOT EXISTS guild_terms (
    guild_id INTEGER NOT NULL,
    term     TEXT    NOT NULL,
    weight   INTEGER NOT NULL,
    match    TEXT    NOT NULL DEFAULT 'word',
    category TEXT    NOT NULL DEFAULT 'custom',
    added_by INTEGER NOT NULL,
    added_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, term)
);
"""

CONFIG_FIELDS = (
    "review_channel_id",
    "log_channel_id",
    "tracking_channel_id",
    "alt_threshold",
    "suspicious_threshold",
    "nsfw_threshold",
    "auto_flag",
)


def _now() -> str:
    return utcnow().isoformat()


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(slots=True)
class GuildConfig:
    guild_id: int
    review_channel_id: int | None = None
    log_channel_id: int | None = None
    tracking_channel_id: int | None = None
    alt_threshold: int = 60
    suspicious_threshold: int = 35
    nsfw_threshold: int = 40
    auto_flag: bool = True
    mod_role_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Case:
    id: int
    guild_id: int
    roblox_id: int
    roblox_name: str
    reasons: list[str]
    status: str
    alt_score: int | None
    nsfw_score: int | None
    details: dict[str, Any]
    opened_by: int | None
    opened_at: datetime
    claimed_by: int | None
    claimed_at: datetime | None
    resolved_by: int | None
    resolved_at: datetime | None
    resolution: str | None
    review_channel_id: int | None
    review_message_id: int | None
    note_count: int = 0

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Case":
        keys = row.keys()
        return cls(
            id=row["id"],
            guild_id=row["guild_id"],
            roblox_id=row["roblox_id"],
            roblox_name=row["roblox_name"],
            reasons=[r for r in row["reasons"].split(",") if r],
            status=row["status"],
            alt_score=row["alt_score"],
            nsfw_score=row["nsfw_score"],
            details=json.loads(row["details"] or "{}"),
            opened_by=row["opened_by"],
            opened_at=parse_ts(row["opened_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=parse_ts(row["claimed_at"]),
            resolved_by=row["resolved_by"],
            resolved_at=parse_ts(row["resolved_at"]),
            resolution=row["resolution"],
            review_channel_id=row["review_channel_id"],
            review_message_id=row["review_message_id"],
            note_count=row["note_count"] if "note_count" in keys else 0,
        )


@dataclass(slots=True)
class Note:
    id: int
    author_id: int
    body: str
    created_at: datetime


@dataclass(slots=True)
class WatchEntry:
    guild_id: int
    roblox_id: int
    roblox_name: str
    note: str | None
    added_by: int
    added_at: datetime
    last_checked_at: datetime | None
    last_badge_count: int | None
    last_alt_score: int | None
    last_nsfw_score: int | None


_CASE_SELECT = """
SELECT cases.*, (SELECT COUNT(*) FROM case_notes WHERE case_notes.case_id = cases.id) AS note_count
FROM cases
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _write(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> aiosqlite.Cursor:
        # Named parameters (a dict) must stay a dict; tuple() would keep only the keys.
        cursor = await self.conn.execute(sql, params if isinstance(params, Mapping) else tuple(params))
        await self.conn.commit()
        return cursor

    # ----------------------------------------------------------- configuration

    async def get_config(self, guild_id: int) -> GuildConfig:
        async with self.conn.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        config = GuildConfig(guild_id=guild_id)
        if row:
            for name in CONFIG_FIELDS:
                setattr(config, name, row[name])
            config.auto_flag = bool(row["auto_flag"])
        config.mod_role_ids = await self.get_mod_roles(guild_id)
        return config

    async def update_config(self, guild_id: int, **values: Any) -> None:
        unknown = set(values) - set(CONFIG_FIELDS)
        if unknown:
            raise ValueError(f"Unknown config fields: {unknown}")
        if not values:
            return
        await self.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        assignments = ", ".join(f"{name} = ?" for name in values)
        await self._write(
            f"UPDATE guild_config SET {assignments} WHERE guild_id = ?",
            [*values.values(), guild_id],
        )

    async def get_mod_roles(self, guild_id: int) -> list[int]:
        async with self.conn.execute("SELECT role_id FROM mod_roles WHERE guild_id = ?", (guild_id,)) as cur:
            return [row["role_id"] for row in await cur.fetchall()]

    async def add_mod_role(self, guild_id: int, role_id: int) -> bool:
        cur = await self._write("INSERT OR IGNORE INTO mod_roles VALUES (?, ?)", (guild_id, role_id))
        return cur.rowcount == 1

    async def remove_mod_role(self, guild_id: int, role_id: int) -> bool:
        cur = await self._write("DELETE FROM mod_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        return cur.rowcount == 1

    # ------------------------------------------------------------------ cases

    async def open_case(
        self,
        guild_id: int,
        roblox_id: int,
        roblox_name: str,
        reasons: list[str],
        alt_score: int | None,
        nsfw_score: int | None,
        details: dict[str, Any],
        opened_by: int | None,
    ) -> tuple[Case, bool]:
        """Open a case, or refresh the existing open case for this account.

        Returns ``(case, created)``. Thanks to the partial unique index only one
        open case per account can exist, even if two moderators race.
        """
        cur = await self._write(
            """
            INSERT OR IGNORE INTO cases
                (guild_id, roblox_id, roblox_name, reasons, alt_score, nsfw_score, details, opened_by, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, roblox_id, roblox_name, ",".join(reasons), alt_score, nsfw_score,
             json.dumps(details), opened_by, _now()),
        )
        if cur.rowcount == 1:
            case = await self.get_case(guild_id, cur.lastrowid)
            assert case is not None
            return case, True

        existing = await self.find_open_case(guild_id, roblox_id)
        if existing is None:  # resolved between the insert and the lookup; just retry
            return await self.open_case(guild_id, roblox_id, roblox_name, reasons, alt_score, nsfw_score, details, opened_by)
        merged = sorted(set(existing.reasons) | set(reasons))
        merged_details = {**existing.details, **details}  # keeps e.g. an earlier manual reason
        await self._write(
            """
            UPDATE cases SET roblox_name = ?, reasons = ?, alt_score = ?, nsfw_score = ?, details = ?
            WHERE id = ?
            """,
            (roblox_name, ",".join(merged), alt_score, nsfw_score, json.dumps(merged_details), existing.id),
        )
        case = await self.get_case(guild_id, existing.id)
        assert case is not None
        return case, False

    async def get_case(self, guild_id: int, case_id: int) -> Case | None:
        async with self.conn.execute(
            _CASE_SELECT + "WHERE cases.guild_id = ? AND cases.id = ?", (guild_id, case_id)
        ) as cur:
            row = await cur.fetchone()
        return Case.from_row(row) if row else None

    async def find_open_case(self, guild_id: int, roblox_id: int) -> Case | None:
        async with self.conn.execute(
            _CASE_SELECT + "WHERE cases.guild_id = ? AND cases.roblox_id = ? AND cases.status = 'open'",
            (guild_id, roblox_id),
        ) as cur:
            row = await cur.fetchone()
        return Case.from_row(row) if row else None

    async def list_cases(
        self,
        guild_id: int,
        *,
        status: str | None = "open",
        claimed_by: int | None = None,
        unclaimed: bool = False,
        roblox_id: int | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Case]:
        clauses, params = ["cases.guild_id = ?"], [guild_id]
        if status:
            clauses.append("cases.status = ?")
            params.append(status)
        if claimed_by is not None:
            clauses.append("cases.claimed_by = ?")
            params.append(claimed_by)
        if unclaimed:
            clauses.append("cases.claimed_by IS NULL")
        if roblox_id is not None:
            clauses.append("cases.roblox_id = ?")
            params.append(roblox_id)
        sql = _CASE_SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY cases.id DESC LIMIT ? OFFSET ?"
        async with self.conn.execute(sql, (*params, limit, offset)) as cur:
            return [Case.from_row(row) for row in await cur.fetchall()]

    async def search_case_ids(self, guild_id: int, text: str, *, open_only: bool, limit: int = 25) -> list[Case]:
        clauses, params = ["cases.guild_id = ?"], [guild_id]
        if open_only:
            clauses.append("cases.status = 'open'")
        if text:
            clauses.append("(CAST(cases.id AS TEXT) LIKE ? OR cases.roblox_name LIKE ?)")
            params += [f"{text}%", f"%{text}%"]
        sql = _CASE_SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY cases.id DESC LIMIT ?"
        async with self.conn.execute(sql, (*params, limit)) as cur:
            return [Case.from_row(row) for row in await cur.fetchall()]

    async def claim_case(self, guild_id: int, case_id: int, user_id: int) -> bool:
        cur = await self._write(
            """
            UPDATE cases SET claimed_by = ?, claimed_at = ?
            WHERE guild_id = ? AND id = ? AND status = 'open' AND claimed_by IS NULL
            """,
            (user_id, _now(), guild_id, case_id),
        )
        return cur.rowcount == 1

    async def release_case(self, guild_id: int, case_id: int, user_id: int, *, force: bool = False) -> bool:
        cur = await self._write(
            """
            UPDATE cases SET claimed_by = NULL, claimed_at = NULL
            WHERE guild_id = ? AND id = ? AND status = 'open' AND claimed_by IS NOT NULL
              AND (claimed_by = ? OR ?)
            """,
            (guild_id, case_id, user_id, int(force)),
        )
        return cur.rowcount == 1

    async def resolve_case(
        self, guild_id: int, case_id: int, user_id: int, status: str, *, force: bool = False
    ) -> bool:
        """Close a case. Claimed cases can only be closed by the claimer unless ``force``."""
        if status not in ("confirmed", "dismissed"):
            raise ValueError("status must be 'confirmed' or 'dismissed'")
        cur = await self._write(
            """
            UPDATE cases SET status = ?, resolved_by = ?, resolved_at = ?, resolution = ?
            WHERE guild_id = ? AND id = ? AND status = 'open'
              AND (claimed_by IS NULL OR claimed_by = ? OR ?)
            """,
            (status, user_id, _now(), status, guild_id, case_id, user_id, int(force)),
        )
        return cur.rowcount == 1

    async def reopen_case(self, guild_id: int, case_id: int) -> bool:
        """Reopen a closed case. Fails if the account already has another open case."""
        try:
            cur = await self._write(
                """
                UPDATE cases SET status = 'open', resolved_by = NULL, resolved_at = NULL, resolution = NULL,
                                 claimed_by = NULL, claimed_at = NULL
                WHERE guild_id = ? AND id = ? AND status != 'open'
                """,
                (guild_id, case_id),
            )
        except aiosqlite.IntegrityError:
            # Only this statement is aborted; don't roll back other coroutines' pending writes.
            return False
        return cur.rowcount == 1

    async def set_case_message(self, case_id: int, channel_id: int | None, message_id: int | None) -> None:
        await self._write(
            "UPDATE cases SET review_channel_id = ?, review_message_id = ? WHERE id = ?",
            (channel_id, message_id, case_id),
        )

    async def add_note(self, case_id: int, author_id: int, body: str) -> None:
        await self._write(
            "INSERT INTO case_notes (case_id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
            (case_id, author_id, body, _now()),
        )

    async def get_notes(self, case_id: int, limit: int = 10) -> list[Note]:
        async with self.conn.execute(
            "SELECT * FROM case_notes WHERE case_id = ? ORDER BY id DESC LIMIT ?", (case_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [Note(r["id"], r["author_id"], r["body"], parse_ts(r["created_at"])) for r in reversed(rows)]

    async def case_counts(self, guild_id: int) -> dict[str, int]:
        async with self.conn.execute(
            """
            SELECT
                SUM(status = 'open')                            AS open,
                SUM(status = 'open' AND claimed_by IS NULL)     AS unclaimed,
                SUM(status = 'confirmed')                       AS confirmed,
                SUM(status = 'dismissed')                       AS dismissed
            FROM cases WHERE guild_id = ?
            """,
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        return {key: row[key] or 0 for key in ("open", "unclaimed", "confirmed", "dismissed")}

    async def moderator_stats(self, guild_id: int, since: datetime | None = None) -> list[tuple[int, int, int, int]]:
        """``[(user_id, resolved, open_claims, notes)]`` sorted by most resolved."""
        since_iso = since.isoformat() if since else ""
        async with self.conn.execute(
            """
            WITH mods AS (
                SELECT resolved_by AS uid FROM cases WHERE guild_id = :g AND resolved_by IS NOT NULL
                UNION SELECT claimed_by FROM cases WHERE guild_id = :g AND claimed_by IS NOT NULL
            )
            SELECT uid,
                (SELECT COUNT(*) FROM cases c WHERE c.guild_id = :g AND c.resolved_by = uid
                    AND c.resolved_at >= :since) AS resolved,
                (SELECT COUNT(*) FROM cases c WHERE c.guild_id = :g AND c.claimed_by = uid
                    AND c.status = 'open') AS claims,
                (SELECT COUNT(*) FROM case_notes n JOIN cases c ON c.id = n.case_id
                    WHERE c.guild_id = :g AND n.author_id = uid AND n.created_at >= :since) AS notes
            FROM mods ORDER BY resolved DESC, claims DESC LIMIT 15
            """,
            {"g": guild_id, "since": since_iso},
        ) as cur:
            return [(r["uid"], r["resolved"], r["claims"], r["notes"]) for r in await cur.fetchall()]

    # -------------------------------------------------------------- audit log

    async def log(self, guild_id: int, actor_id: int | None, action: str, target: str = "", details: str = "") -> None:
        await self._write(
            "INSERT INTO audit_log (guild_id, actor_id, action, target, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, actor_id, action, target, details, _now()),
        )

    async def recent_audit(self, guild_id: int, *, limit: int = 15, actor_id: int | None = None) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM audit_log WHERE guild_id = ?"
        params: list[Any] = [guild_id]
        if actor_id is not None:
            sql += " AND actor_id = ?"
            params.append(actor_id)
        sql += " ORDER BY id DESC LIMIT ?"
        async with self.conn.execute(sql, (*params, limit)) as cur:
            return list(await cur.fetchall())

    # -------------------------------------------------------------- watchlist

    async def add_watch(self, guild_id: int, roblox_id: int, roblox_name: str, added_by: int, note: str | None) -> bool:
        cur = await self._write(
            """
            INSERT OR IGNORE INTO watchlist (guild_id, roblox_id, roblox_name, note, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, roblox_id, roblox_name, note, added_by, _now()),
        )
        return cur.rowcount == 1

    async def remove_watch(self, guild_id: int, roblox_id: int) -> bool:
        cur = await self._write("DELETE FROM watchlist WHERE guild_id = ? AND roblox_id = ?", (guild_id, roblox_id))
        await self._write("DELETE FROM known_badges WHERE guild_id = ? AND roblox_id = ?", (guild_id, roblox_id))
        return cur.rowcount == 1

    @staticmethod
    def _watch(row: aiosqlite.Row) -> WatchEntry:
        return WatchEntry(
            guild_id=row["guild_id"],
            roblox_id=row["roblox_id"],
            roblox_name=row["roblox_name"],
            note=row["note"],
            added_by=row["added_by"],
            added_at=parse_ts(row["added_at"]),
            last_checked_at=parse_ts(row["last_checked_at"]),
            last_badge_count=row["last_badge_count"],
            last_alt_score=row["last_alt_score"],
            last_nsfw_score=row["last_nsfw_score"],
        )

    async def get_watch(self, guild_id: int, roblox_id: int) -> WatchEntry | None:
        async with self.conn.execute(
            "SELECT * FROM watchlist WHERE guild_id = ? AND roblox_id = ?", (guild_id, roblox_id)
        ) as cur:
            row = await cur.fetchone()
        return self._watch(row) if row else None

    async def list_watch(self, guild_id: int, *, limit: int = 25, offset: int = 0) -> list[WatchEntry]:
        async with self.conn.execute(
            "SELECT * FROM watchlist WHERE guild_id = ? ORDER BY added_at DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ) as cur:
            return [self._watch(r) for r in await cur.fetchall()]

    async def count_watch(self, guild_id: int) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM watchlist WHERE guild_id = ?", (guild_id,)) as cur:
            return (await cur.fetchone())[0]

    async def due_watches(self, older_than: datetime, limit: int, guild_ids: Iterable[int]) -> list[WatchEntry]:
        """Watched players not scanned since ``older_than``, never-scanned first."""
        guild_ids = list(guild_ids)
        if not guild_ids:
            return []
        placeholders = ",".join("?" * len(guild_ids))
        async with self.conn.execute(
            f"""
            SELECT * FROM watchlist
            WHERE guild_id IN ({placeholders}) AND (last_checked_at IS NULL OR last_checked_at < ?)
            ORDER BY last_checked_at IS NOT NULL, last_checked_at
            LIMIT ?
            """,
            (*guild_ids, older_than.isoformat(), limit),
        ) as cur:
            return [self._watch(r) for r in await cur.fetchall()]

    async def record_watch_scan(
        self,
        guild_id: int,
        roblox_id: int,
        roblox_name: str,
        badge_count: int,
        new_badges: int,
        alt_score: int,
        nsfw_score: int,
    ) -> None:
        now = _now()
        await self.conn.execute(
            """
            UPDATE watchlist SET roblox_name = ?, last_checked_at = ?, last_badge_count = ?,
                                 last_alt_score = ?, last_nsfw_score = ?
            WHERE guild_id = ? AND roblox_id = ?
            """,
            (roblox_name, now, badge_count, alt_score, nsfw_score, guild_id, roblox_id),
        )
        await self._write(
            "INSERT INTO badge_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, roblox_id, now, badge_count, new_badges, alt_score, nsfw_score),
        )

    async def snapshots(self, guild_id: int, roblox_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM badge_snapshots WHERE guild_id = ? AND roblox_id = ? ORDER BY taken_at DESC LIMIT ?",
            (guild_id, roblox_id, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def known_badges(self, guild_id: int, roblox_id: int) -> set[int]:
        async with self.conn.execute(
            "SELECT badge_id FROM known_badges WHERE guild_id = ? AND roblox_id = ?", (guild_id, roblox_id)
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def add_known_badges(self, guild_id: int, roblox_id: int, badge_ids: Iterable[int]) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO known_badges VALUES (?, ?, ?)",
            [(guild_id, roblox_id, badge_id) for badge_id in badge_ids],
        )
        await self.conn.commit()

    # ---------------------------------------------------------------- trusted

    async def add_trusted(self, guild_id: int, roblox_id: int, roblox_name: str, added_by: int, reason: str | None) -> bool:
        cur = await self._write(
            """
            INSERT INTO trusted_accounts (guild_id, roblox_id, roblox_name, reason, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, roblox_id) DO UPDATE SET reason = excluded.reason, roblox_name = excluded.roblox_name
            """,
            (guild_id, roblox_id, roblox_name, reason, added_by, _now()),
        )
        return cur.rowcount == 1

    async def remove_trusted(self, guild_id: int, roblox_id: int) -> bool:
        cur = await self._write(
            "DELETE FROM trusted_accounts WHERE guild_id = ? AND roblox_id = ?", (guild_id, roblox_id)
        )
        return cur.rowcount == 1

    async def get_trusted(self, guild_id: int, roblox_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM trusted_accounts WHERE guild_id = ? AND roblox_id = ?", (guild_id, roblox_id)
        ) as cur:
            return await cur.fetchone()

    async def list_trusted(self, guild_id: int, limit: int = 50) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM trusted_accounts WHERE guild_id = ? ORDER BY added_at DESC LIMIT ?", (guild_id, limit)
        ) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------ filter terms

    async def set_term(self, guild_id: int, term: str, weight: int, match: str, category: str, added_by: int) -> None:
        await self._write(
            """
            INSERT INTO guild_terms (guild_id, term, weight, match, category, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, term) DO UPDATE SET
                weight = excluded.weight, match = excluded.match, category = excluded.category,
                added_by = excluded.added_by, added_at = excluded.added_at
            """,
            (guild_id, term, weight, match, category, added_by, _now()),
        )

    async def remove_term(self, guild_id: int, term: str) -> bool:
        cur = await self._write("DELETE FROM guild_terms WHERE guild_id = ? AND term = ?", (guild_id, term))
        return cur.rowcount == 1

    async def list_terms(self, guild_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM guild_terms WHERE guild_id = ? ORDER BY term", (guild_id,)
        ) as cur:
            return list(await cur.fetchall())

    # ---------------------------------------------- saved players & groups

    async def save_players(self, guild_id: int, records: list[dict[str, Any]]) -> None:
        """Remember every account the bot has looked at (one row per account, updated each time).

        ``records`` come from :meth:`ScanResult.record`. A quick look (a friend
        on page 2 of /check) never overwrites the results of a full check.
        """
        if not records:
            return
        now = _now()
        keep = "(excluded.depth = 'quick' AND players.depth = 'full')"
        scored = ("badges", "badges_more", "friends", "followers", "groups_count", "alt_score", "alt_verdict",
                  "nsfw_score", "nsfw_matches", "flags", "depth")
        updates = ",\n".join(f"{col} = CASE WHEN {keep} THEN players.{col} ELSE excluded.{col} END" for col in scored)
        await self.conn.executemany(
            f"""
            INSERT INTO players (
                guild_id, roblox_id, username, display_name, account_created, is_banned, verified, badges, badges_more,
                friends, followers, groups_count, bio, alt_score, alt_verdict, nsfw_score, nsfw_matches, flags, depth,
                found_via, first_seen, last_checked, times_seen, last_checked_by
            ) VALUES (
                :guild_id, :roblox_id, :username, :display_name, :account_created, :is_banned, :verified, :badges,
                :badges_more, :friends, :followers, :groups_count, :bio, :alt_score, :alt_verdict, :nsfw_score,
                :nsfw_matches, :flags, :depth, :found_via, :now, :now, 1, :checked_by
            )
            ON CONFLICT (guild_id, roblox_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                account_created = COALESCE(excluded.account_created, players.account_created),
                is_banned = excluded.is_banned,
                verified = excluded.verified,
                bio = excluded.bio,
                last_checked = excluded.last_checked,
                times_seen = players.times_seen + 1,
                last_checked_by = COALESCE(excluded.last_checked_by, players.last_checked_by),
                {updates}
            """,
            [{**record, "guild_id": guild_id, "now": now} for record in records],
        )
        await self.conn.commit()

    async def save_group(self, guild_id: int, record: dict[str, Any]) -> None:
        now = _now()
        await self._write(
            """
            INSERT INTO roblox_groups (
                guild_id, group_id, name, member_count, owner, nsfw_score, nsfw_matches, members_checked,
                nsfw_members, alt_members, suspicious_members, first_scanned, last_scanned, times_scanned, last_scanned_by
            ) VALUES (
                :guild_id, :group_id, :name, :member_count, :owner, :nsfw_score, :nsfw_matches, :members_checked,
                :nsfw_members, :alt_members, :suspicious_members, :now, :now, 1, :scanned_by
            )
            ON CONFLICT (guild_id, group_id) DO UPDATE SET
                name = excluded.name, member_count = excluded.member_count, owner = excluded.owner,
                nsfw_score = excluded.nsfw_score, nsfw_matches = excluded.nsfw_matches,
                members_checked = excluded.members_checked, nsfw_members = excluded.nsfw_members,
                alt_members = excluded.alt_members, suspicious_members = excluded.suspicious_members,
                last_scanned = excluded.last_scanned, times_scanned = roblox_groups.times_scanned + 1,
                last_scanned_by = excluded.last_scanned_by
            """,
            {**record, "guild_id": guild_id, "now": now},
        )

    @staticmethod
    def _player_filter(guild_id: int, show: str, search: str | None) -> tuple[str, list[Any]]:
        clauses, params = ["guild_id = ?"], [guild_id]
        if show == "flagged":
            clauses.append("flags != ''")
        elif show == "nsfw":
            clauses.append("flags LIKE '%nsfw%'")
        elif show == "alts":
            clauses.append("flags LIKE '%alt%'")
        if search:
            text = search.strip().lstrip("@")
            clauses.append("(username LIKE ? OR display_name LIKE ? OR CAST(roblox_id AS TEXT) = ?)")
            params += [f"%{text}%", f"%{text}%", text]
        return " AND ".join(clauses), params

    async def list_players(
        self, guild_id: int, *, show: str = "all", search: str | None = None, limit: int = 15, offset: int = 0
    ) -> list[aiosqlite.Row]:
        where, params = self._player_filter(guild_id, show, search)
        order = "MAX(nsfw_score, alt_score) DESC, last_checked DESC" if show != "all" else "last_checked DESC"
        async with self.conn.execute(
            f"SELECT * FROM players WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset)
        ) as cur:
            return list(await cur.fetchall())

    async def count_players(self, guild_id: int, *, show: str = "all", search: str | None = None) -> int:
        where, params = self._player_filter(guild_id, show, search)
        async with self.conn.execute(f"SELECT COUNT(*) FROM players WHERE {where}", params) as cur:
            return (await cur.fetchone())[0]

    async def list_groups(self, guild_id: int, *, limit: int = 15, offset: int = 0) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM roblox_groups WHERE guild_id = ? ORDER BY last_scanned DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ) as cur:
            return list(await cur.fetchall())

    async def database_counts(self, guild_id: int) -> dict[str, int]:
        queries = {
            "players": "SELECT COUNT(*) FROM players WHERE guild_id = ?",
            "flagged": "SELECT COUNT(*) FROM players WHERE guild_id = ? AND flags != ''",
            "nsfw": "SELECT COUNT(*) FROM players WHERE guild_id = ? AND flags LIKE '%nsfw%'",
            "alts": "SELECT COUNT(*) FROM players WHERE guild_id = ? AND flags LIKE '%alt%'",
            "full": "SELECT COUNT(*) FROM players WHERE guild_id = ? AND depth = 'full'",
            "groups": "SELECT COUNT(*) FROM roblox_groups WHERE guild_id = ?",
            "cases": "SELECT COUNT(*) FROM cases WHERE guild_id = ?",
            "open_cases": "SELECT COUNT(*) FROM cases WHERE guild_id = ? AND status = 'open'",
            "notes": "SELECT COUNT(*) FROM case_notes JOIN cases ON cases.id = case_notes.case_id WHERE cases.guild_id = ?",
            "tracked": "SELECT COUNT(*) FROM watchlist WHERE guild_id = ?",
            "trusted": "SELECT COUNT(*) FROM trusted_accounts WHERE guild_id = ?",
            "terms": "SELECT COUNT(*) FROM guild_terms WHERE guild_id = ?",
            "activity": "SELECT COUNT(*) FROM audit_log WHERE guild_id = ?",
        }
        counts = {}
        for key, sql in queries.items():
            async with self.conn.execute(sql, (guild_id,)) as cur:
                counts[key] = (await cur.fetchone())[0]
        return counts

    async def export_tables(self, guild_id: int, *, players: str = "all") -> dict[str, list[aiosqlite.Row]]:
        """Everything this server has stored, table by table (for the Excel export)."""
        player_where = "guild_id = ?" + ("" if players == "all" else " AND (flags != '' OR depth = 'full')")
        queries = {
            "players": f"SELECT * FROM players WHERE {player_where} ORDER BY flags = '', MAX(nsfw_score, alt_score) DESC",
            "groups": "SELECT * FROM roblox_groups WHERE guild_id = ? ORDER BY last_scanned DESC",
            "cases": _CASE_SELECT + "WHERE cases.guild_id = ? ORDER BY cases.id DESC",
            "notes": """SELECT case_notes.*, cases.roblox_name FROM case_notes JOIN cases ON cases.id = case_notes.case_id
                        WHERE cases.guild_id = ? ORDER BY case_notes.case_id DESC, case_notes.id""",
            "watchlist": "SELECT * FROM watchlist WHERE guild_id = ? ORDER BY added_at DESC",
            "trusted": "SELECT * FROM trusted_accounts WHERE guild_id = ? ORDER BY added_at DESC",
            "activity": "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY id DESC LIMIT 10000",
            "terms": "SELECT * FROM guild_terms WHERE guild_id = ? ORDER BY term",
        }
        tables = {}
        for key, sql in queries.items():
            async with self.conn.execute(sql, (guild_id,)) as cur:
                tables[key] = list(await cur.fetchall())
        return tables
