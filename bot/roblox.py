"""Async client for the public (no login needed) Roblox web APIs."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import aiohttp

from .models import AccountData, Badge, Group, GroupInfo, GroupMember, parse_roblox_time

log = logging.getLogger(__name__)

_PROFILE_URL = re.compile(r"roblox\.com/users/(\d+)", re.IGNORECASE)
_USERNAME = re.compile(r"^@?([A-Za-z0-9_]{3,20})$")
# Roblox renamed groups to "communities"; both kinds of link are around.
_GROUP_URL = re.compile(r"roblox\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:groups|communities)/(\d+)", re.IGNORECASE)


class RobloxError(Exception):
    """A Roblox API request failed."""


class UserNotFound(RobloxError):
    pass


class GroupNotFound(RobloxError):
    pass


class _HTTPStatus(RobloxError):
    def __init__(self, status: int, url: str):
        super().__init__(f"Roblox returned HTTP {status} for {url}")
        self.status = status


class RobloxClient:
    """Small wrapper around aiohttp with retries, a concurrency cap and a short GET cache.

    The cache means several moderators checking the same player at the same time
    only cost one round of API calls.
    """

    def __init__(self, *, max_concurrency: int = 4, cache_ttl: float = 120.0, max_badges: int = 500):
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = cache_ttl
        self.max_badges = max_badges

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"User-Agent": "RobloxChecker-DiscordBot/1.0", "Accept": "application/json"},
                trust_env=True,
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ HTTP

    async def _request(self, method: str, url: str, *, params: dict | None = None, json: Any = None) -> Any:
        await self.start()
        assert self._session is not None
        cache_key = None
        if method == "GET":
            cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            cached = self._cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]

        delay = 1.0
        for attempt in range(4):
            try:
                async with self._semaphore:
                    async with self._session.request(method, url, params=params, json=json) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            retry_after = resp.headers.get("Retry-After")
                            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                            log.warning("Roblox %s on %s, retrying in %.1fs", resp.status, url, wait)
                            await asyncio.sleep(min(wait, 30))
                            delay *= 2
                            continue
                        if resp.status >= 400:
                            raise _HTTPStatus(resp.status, url)
                        data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 3:
                    raise RobloxError(f"Could not reach Roblox: {exc}") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if cache_key:
                self._cache[cache_key] = (time.monotonic() + self._cache_ttl, data)
                if len(self._cache) > 2000:
                    now = time.monotonic()
                    self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            return data
        raise RobloxError(f"Roblox kept rate-limiting {url}; try again in a minute")

    async def _optional(self, coro, default=None):
        """Run a non-essential lookup; failures are logged and replaced by ``default``."""
        try:
            return await coro
        except RobloxError as exc:
            log.info("Optional Roblox lookup failed: %s", exc)
            return default

    # --------------------------------------------------------------- lookups

    async def resolve_user_id(self, query: str) -> int:
        """Accept a username, ``@username``, numeric ID or profile URL."""
        query = query.strip()
        if match := _PROFILE_URL.search(query):
            return int(match.group(1))
        if query.lower().startswith("id:"):
            query = query[3:].strip()
            if query.isdigit():
                return int(query)
        if query.isdigit():
            try:
                await self.get_user(int(query))
                return int(query)
            except UserNotFound:
                pass  # all-digit usernames exist; fall through to a name lookup
        match = _USERNAME.match(query)
        if not match:
            raise UserNotFound(f"`{query}` is not a valid Roblox username, ID or profile link")
        data = await self._request(
            "POST",
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [match.group(1)], "excludeBannedUsers": False},
        )
        users = data.get("data") or []
        if not users:
            raise UserNotFound(f"No Roblox user called `{match.group(1)}`")
        return int(users[0]["id"])

    async def get_user(self, user_id: int) -> dict:
        try:
            return await self._request("GET", f"https://users.roblox.com/v1/users/{user_id}")
        except _HTTPStatus as exc:
            if exc.status in (400, 404):
                raise UserNotFound(f"No Roblox user with ID {user_id}") from exc
            raise

    async def get_badges(self, user_id: int, max_badges: int | None = None) -> tuple[list[Badge], bool, bool]:
        """Return ``(badges, hidden, truncated)``; newest badges first."""
        cap = max_badges or self.max_badges
        badges: list[Badge] = []
        cursor = ""
        while len(badges) < cap:
            params = {"limit": 100, "sortOrder": "Desc"}
            if cursor:
                params["cursor"] = cursor
            try:
                page = await self._request("GET", f"https://badges.roblox.com/v1/users/{user_id}/badges", params=params)
            except _HTTPStatus as exc:
                if exc.status in (401, 403):
                    return [], True, False
                raise
            for item in page.get("data") or []:
                awarder = item.get("awarder") or {}
                badges.append(Badge(id=int(item["id"]), name=item.get("name") or "?", awarder_id=awarder.get("id")))
            cursor = page.get("nextPageCursor") or ""
            if not cursor:
                return badges, False, False
        return badges[:cap], False, True

    async def add_award_dates(self, user_id: int, badges: list[Badge]) -> None:
        by_id = {b.id: b for b in badges}
        ids = list(by_id)
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            data = await self._request(
                "GET",
                f"https://badges.roblox.com/v1/users/{user_id}/badges/awarded-dates",
                params={"badgeIds": ",".join(map(str, chunk))},
            )
            for item in data.get("data") or []:
                badge = by_id.get(int(item.get("badgeId", 0)))
                if badge:
                    badge.awarded_at = parse_roblox_time(item.get("awardedDate"))

    async def _count(self, url: str) -> int | None:
        data = await self._request("GET", url)
        return int(data.get("count", 0))

    async def get_groups(self, user_id: int) -> list[Group]:
        data = await self._request("GET", f"https://groups.roblox.com/v1/users/{user_id}/groups/roles")
        groups = []
        for item in data.get("data") or []:
            group, role = item.get("group") or {}, item.get("role") or {}
            groups.append(
                Group(
                    id=int(group.get("id", 0)),
                    name=group.get("name") or "",
                    role=role.get("name"),
                    rank=role.get("rank"),
                    member_count=group.get("memberCount"),
                )
            )
        return groups

    async def get_username_history(self, user_id: int) -> list[str]:
        data = await self._request(
            "GET",
            f"https://users.roblox.com/v1/users/{user_id}/username-history",
            params={"limit": 50, "sortOrder": "Desc"},
        )
        return [item["name"] for item in data.get("data") or [] if item.get("name")]

    async def get_legacy_badges(self, user_id: int) -> list[str]:
        data = await self._request("GET", f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges")
        return [item.get("name", "") for item in data or [] if isinstance(item, dict)]

    async def get_headshot(self, user_id: int) -> tuple[str | None, str | None]:
        data = await self._request(
            "GET",
            "https://thumbnails.roblox.com/v1/users/avatar-headshot",
            params={"userIds": user_id, "size": "150x150", "format": "Png", "isCircular": "false"},
        )
        for item in data.get("data") or []:
            return item.get("imageUrl"), item.get("state")
        return None, None

    async def can_view_inventory(self, user_id: int) -> bool | None:
        data = await self._request("GET", f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory")
        return data.get("canView")

    # --------------------------------------------------------------- friends

    async def get_friend_ids(self, user_id: int, limit: int = 1000) -> list[int]:
        """IDs of a player's friends ("connections"), up to ``limit``.

        Roblox pages these through ``/friends/find`` now; the older ``/friends``
        list is the fallback (it still returns the IDs, if not always names).
        """
        ids: list[int] = []
        try:
            cursor = ""
            while len(ids) < limit:
                params = {"limit": 50}
                if cursor:
                    params["cursor"] = cursor
                page = await self._request("GET", f"https://friends.roblox.com/v1/users/{user_id}/friends/find", params=params)
                items = page.get("PageItems") or page.get("pageItems") or []
                ids += [int(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]
                cursor = page.get("NextCursor") or page.get("nextCursor") or ""
                if not cursor or not items:
                    break
        except _HTTPStatus:
            ids = []
        if not ids:
            data = await self._request("GET", f"https://friends.roblox.com/v1/users/{user_id}/friends")
            ids = [int(f["id"]) for f in data.get("data") or [] if isinstance(f, dict) and f.get("id")]
        return list(dict.fromkeys(ids))[:limit]

    async def get_users_basic(self, user_ids: list[int]) -> dict[int, tuple[str, str]]:
        """``{id: (username, display name)}`` for many users, 100 per request."""
        names: dict[int, tuple[str, str]] = {}
        for start in range(0, len(user_ids), 100):
            data = await self._request(
                "POST",
                "https://users.roblox.com/v1/users",
                json={"userIds": user_ids[start : start + 100], "excludeBannedUsers": False},
            )
            for item in data.get("data") or []:
                if item.get("id"):
                    names[int(item["id"])] = (item.get("name") or "", item.get("displayName") or item.get("name") or "")
        return names

    async def fetch_brief(self, user_id: int) -> AccountData:
        """A quick look for bulk checks: profile, bio, age and the first badge page.

        Friends, groups and the rest stay ``None`` (unknown), so the scorer skips them.
        """
        user = await self.get_user(user_id)
        account = AccountData(
            user_id=int(user["id"]),
            username=user.get("name") or str(user_id),
            display_name=user.get("displayName") or user.get("name") or "",
            description=user.get("description") or "",
            created=parse_roblox_time(user.get("created")),
            is_banned=bool(user.get("isBanned")),
            verified=bool(user.get("hasVerifiedBadge")),
        )
        badge_result = await self._optional(self.get_badges(user_id, 100), None)
        if badge_result is None:
            account.badges_hidden = True
        else:
            account.badges, account.badges_hidden, account.badges_truncated = badge_result
            if not account.badges and not account.badges_hidden:
                # An empty list can mean a private inventory; don't call that "0 badges".
                account.badges_hidden = await self._optional(self.can_view_inventory(user_id)) is False
        return account

    # ---------------------------------------------------------------- groups

    async def resolve_group_id(self, query: str) -> int:
        """Accept a group/community link, a group ID or an exact group name."""
        query = query.strip()
        if match := _GROUP_URL.search(query):
            return int(match.group(1))
        if query.lower().startswith("id:"):
            query = query[3:].strip()
        if query.isdigit():
            return int(query)
        if not 2 <= len(query) <= 50:
            raise GroupNotFound(f"`{query}` isn't a Roblox group link, ID or name")
        data = await self._request(
            "GET", "https://groups.roblox.com/v1/groups/search/lookup", params={"groupName": query}
        )
        found = [g for g in data.get("data") or [] if g.get("id")]
        exact = [g for g in found if str(g.get("name", "")).lower() == query.lower()]
        if not (exact or found):
            raise GroupNotFound(f"No Roblox group called `{query}`. Try pasting the group's link instead")
        return int((exact or found)[0]["id"])

    async def get_group(self, group_id: int) -> GroupInfo:
        try:
            data = await self._request("GET", f"https://groups.roblox.com/v1/groups/{group_id}")
        except _HTTPStatus as exc:
            if exc.status in (400, 404):
                raise GroupNotFound(f"No Roblox group with ID {group_id}") from exc
            raise
        owner = data.get("owner") or {}
        return GroupInfo(
            id=int(data.get("id", group_id)),
            name=data.get("name") or f"Group {group_id}",
            description=data.get("description") or "",
            member_count=data.get("memberCount"),
            owner_name=owner.get("username"),
            verified=bool(data.get("hasVerifiedBadge")),
        )

    async def get_group_members(self, group_id: int, limit: int) -> list[GroupMember]:
        """Up to ``limit`` members, newest first as Roblox lists them."""
        members: list[GroupMember] = []
        cursor = ""
        while len(members) < limit:
            params = {"limit": 100, "sortOrder": "Desc"}
            if cursor:
                params["cursor"] = cursor
            page = await self._request("GET", f"https://groups.roblox.com/v1/groups/{group_id}/users", params=params)
            for item in page.get("data") or []:
                user, role = item.get("user") or {}, item.get("role") or {}
                if user.get("userId"):
                    members.append(
                        GroupMember(
                            user_id=int(user["userId"]),
                            username=user.get("username") or str(user["userId"]),
                            display_name=user.get("displayName") or user.get("username") or "",
                            role=role.get("name"),
                        )
                    )
            cursor = page.get("nextPageCursor") or ""
            if not cursor:
                break
        return members[:limit]

    # -------------------------------------------------------------- full scan

    async def fetch_account(self, user_id: int, *, max_badges: int | None = None) -> AccountData:
        """Fetch everything the analysers need. Only the core profile is mandatory."""
        user = await self.get_user(user_id)
        (
            badge_result,
            friends,
            followers,
            following,
            groups,
            history,
            legacy,
            headshot,
            inventory_visible,
        ) = await asyncio.gather(
            self._optional(self.get_badges(user_id, max_badges), None),
            self._optional(self._count(f"https://friends.roblox.com/v1/users/{user_id}/friends/count")),
            self._optional(self._count(f"https://friends.roblox.com/v1/users/{user_id}/followers/count")),
            self._optional(self._count(f"https://friends.roblox.com/v1/users/{user_id}/followings/count")),
            self._optional(self.get_groups(user_id)),
            self._optional(self.get_username_history(user_id), []),
            self._optional(self.get_legacy_badges(user_id), []),
            self._optional(self.get_headshot(user_id), (None, None)),
            self._optional(self.can_view_inventory(user_id)),
        )

        account = AccountData(
            user_id=int(user["id"]),
            username=user.get("name") or str(user_id),
            display_name=user.get("displayName") or user.get("name") or "",
            description=user.get("description") or "",
            created=parse_roblox_time(user.get("created")),
            is_banned=bool(user.get("isBanned")),
            verified=bool(user.get("hasVerifiedBadge")),
            friends=friends,
            followers=followers,
            following=following,
            groups=groups,
            username_history=[n for n in history if n.lower() != (user.get("name") or "").lower()],
            legacy_badges=legacy,
        )
        account.avatar_url, account.avatar_state = headshot

        if badge_result is None:
            account.badges_hidden = True  # request failed; don't treat as "0 badges"
        else:
            account.badges, account.badges_hidden, account.badges_truncated = badge_result
            if not account.badges and inventory_visible is False:
                account.badges_hidden = True
        if account.badges:
            await self._optional(self.add_award_dates(user_id, account.badges))
        return account
