"""Badge-driven alt-account scoring.

Every check produces a list of :class:`Signal` objects. Positive points push the
account towards "alt", negative points are trust signals. The final score is the
sum, clamped to 0..100, so moderators can always see *why* a score is what it is.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import AccountData, utcnow

LIKELY_ALT = "Likely alt"
SUSPICIOUS = "Suspicious"
LIKELY_MAIN = "Likely main"

PREMIUM_BADGE = "welcome to the club"
_GENERATED_NAME = re.compile(r"\d{4,}$")


@dataclass(slots=True)
class Signal:
    points: int
    label: str


@dataclass(slots=True)
class BadgeStats:
    total: int = 0
    distinct_games: int = 0
    top_game_share: float = 0.0
    first_awarded: datetime | None = None
    last_awarded: datetime | None = None
    busiest_day_share: float = 0.0
    recent_share: float = 0.0  # share of dated badges earned in the last 14 days


@dataclass(slots=True)
class AltReport:
    score: int
    verdict: str
    signals: list[Signal] = field(default_factory=list)
    badge_stats: BadgeStats = field(default_factory=BadgeStats)


def badge_stats(account: AccountData, now: datetime | None = None) -> BadgeStats:
    now = now or utcnow()
    badges = account.badges
    stats = BadgeStats(total=len(badges))
    if not badges:
        return stats

    games = Counter(b.awarder_id for b in badges if b.awarder_id is not None)
    stats.distinct_games = len(games)
    if games:
        stats.top_game_share = games.most_common(1)[0][1] / sum(games.values())

    dates = sorted(b.awarded_at for b in badges if b.awarded_at is not None)
    if dates:
        stats.first_awarded, stats.last_awarded = dates[0], dates[-1]
        # Largest number of badges earned inside any rolling 24 hour window.
        best, start = 0, 0
        for end, moment in enumerate(dates):
            while moment - dates[start] > timedelta(hours=24):
                start += 1
            best = max(best, end - start + 1)
        stats.busiest_day_share = best / len(dates)
        recent = sum(1 for d in dates if now - d <= timedelta(days=14))
        stats.recent_share = recent / len(dates)
    return stats


def analyse(
    account: AccountData,
    *,
    alt_threshold: int = 60,
    suspicious_threshold: int = 35,
    now: datetime | None = None,
) -> AltReport:
    now = now or utcnow()
    signals: list[Signal] = []
    add = lambda points, label: signals.append(Signal(points, label))  # noqa: E731

    if account.verified:
        add(-40, "Roblox verified badge")

    # --- Account age -----------------------------------------------------
    age = account.age_days(now)
    if age is None:
        add(0, "Account creation date unavailable")
    elif age < 3:
        add(35, f"Brand new account ({age} days old)")
    elif age < 14:
        add(28, f"Very new account ({age} days old)")
    elif age < 30:
        add(20, f"New account ({age} days old)")
    elif age < 90:
        add(12, f"Young account ({age} days old)")
    elif age < 365:
        add(5, f"Account under a year old ({age} days)")
    elif age >= 3 * 365:
        add(-10, f"Established account ({age // 365} years old)")

    # --- Badges ------------------------------------------------------------
    stats = badge_stats(account, now)
    if account.badges_hidden:
        add(6, "Badges/inventory are private, so they couldn't be verified")
    else:
        count = stats.total
        shown = f"{count}+" if account.badges_truncated else str(count)
        if count == 0:
            add(30, "No game badges at all")
        elif count < 5:
            add(22, f"Very few badges ({shown})")
        elif count < 20:
            add(12, f"Few badges ({shown})")
        elif count < 50:
            add(5, f"Low badge count ({shown})")
        elif count >= 200:
            add(-10, f"Large badge collection ({shown})")
        else:
            add(-5, f"Healthy badge count ({shown})")

        if count >= 5:
            if stats.distinct_games and stats.distinct_games <= 2:
                add(12, f"All badges come from only {stats.distinct_games} game(s), a sign of badge farming")
            elif count >= 10 and stats.top_game_share >= 0.8:
                add(8, f"{stats.top_game_share:.0%} of badges come from a single game")
            elif stats.distinct_games >= 25:
                add(-5, f"Badges from {stats.distinct_games} different games")

        if count >= 5 and stats.busiest_day_share >= 0.8:
            add(12, f"{stats.busiest_day_share:.0%} of badges were earned within one 24h window")

        # With a capped badge list we only see the newest badges, so "when did
        # they start earning badges" can't be judged.
        if (
            not account.badges_truncated
            and age is not None
            and age > 180
            and count >= 5
            and stats.recent_share >= 0.9
            and stats.busiest_day_share < 0.8
        ):
            add(10, "Old account that only started earning badges in the last 2 weeks")

        if not account.badges_truncated and stats.first_awarded and account.created and count >= 3:
            gap = (stats.first_awarded - account.created).days
            if gap > 365:
                add(6, f"First badge earned {gap // 365} year(s) after the account was made")

    # --- Social footprint ------------------------------------------------
    if account.friends is not None:
        if account.friends == 0:
            add(10, "No friends")
        elif account.friends < 5:
            add(6, f"Very few friends ({account.friends})")
        elif account.friends >= 50:
            add(-5, f"Many friends ({account.friends})")

    if account.followers is not None:
        if account.followers >= 1000:
            add(-10, f"{account.followers:,} followers")
        elif account.followers >= 100:
            add(-5, f"{account.followers:,} followers")

    if account.groups is not None:
        if not account.groups:
            add(6, "Not in any groups")
        elif len(account.groups) >= 5:
            add(-3, f"Member of {len(account.groups)} groups")

    # --- Profile ---------------------------------------------------------
    if not account.description.strip():
        add(4, "Empty profile description")
    if _GENERATED_NAME.search(account.username):
        add(6, "Username ends in a long number (looks auto-generated)")
    if account.username_history:
        add(-4, f"Has changed username {len(account.username_history)} time(s) (costs Robux)")

    legacy = [name.lower() for name in account.legacy_badges]
    if PREMIUM_BADGE in legacy:
        add(-10, "Has had Roblox Premium")
    other_legacy = [n for n in legacy if n != PREMIUM_BADGE]
    if other_legacy:
        add(-min(12, 4 * len(other_legacy)), f"{len(other_legacy)} Roblox achievement badge(s)")

    score = max(0, min(100, sum(s.points for s in signals)))
    if score >= alt_threshold:
        verdict = LIKELY_ALT
    elif score >= suspicious_threshold:
        verdict = SUSPICIOUS
    else:
        verdict = LIKELY_MAIN

    signals = [s for s in signals if s.points != 0 or "unavailable" in s.label]
    signals.sort(key=lambda s: -abs(s.points))
    return AltReport(score=score, verdict=verdict, signals=signals, badge_stats=stats)
