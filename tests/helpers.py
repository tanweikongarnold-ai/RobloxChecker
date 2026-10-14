from datetime import datetime, timedelta, timezone

from bot.models import AccountData, Badge, Group

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def make_account(
    *,
    age_days: int = 1000,
    badges: int = 150,
    games: int = 60,
    friends: int | None = 80,
    followers: int | None = 20,
    groups: int | None = 4,
    description: str = "I like obbies and tycoons",
    username: str = "BuilderKid",
    badge_spread_days: int = 900,
    **overrides,
) -> AccountData:
    created = NOW - timedelta(days=age_days)
    badge_list = []
    for i in range(badges):
        awarded = created + timedelta(days=1 + (badge_spread_days * i) // max(1, badges))
        badge_list.append(Badge(id=1000 + i, name=f"Badge {i}", awarder_id=(i % games) + 1, awarded_at=awarded))
    account = AccountData(
        user_id=123,
        username=username,
        display_name=username,
        description=description,
        created=created,
        badges=badge_list,
        friends=friends,
        followers=followers,
        following=10,
        groups=None if groups is None else [Group(id=i, name=f"Group {i}") for i in range(groups)],
    )
    for key, value in overrides.items():
        setattr(account, key, value)
    return account
