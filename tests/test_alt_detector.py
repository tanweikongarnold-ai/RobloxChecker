from datetime import timedelta

from bot import alt_detector
from bot.alt_detector import LIKELY_ALT, LIKELY_MAIN, SUSPICIOUS
from bot.models import Badge

from .helpers import NOW, make_account


def analyse(account, **kwargs):
    return alt_detector.analyse(account, now=NOW, **kwargs)


def test_established_account_is_main():
    report = analyse(make_account())
    assert report.verdict == LIKELY_MAIN
    assert report.score == 0


def test_fresh_empty_account_is_alt():
    account = make_account(
        age_days=2, badges=0, friends=0, followers=0, groups=0, description="", username="Player84721"
    )
    report = analyse(account)
    assert report.verdict == LIKELY_ALT
    assert report.score >= 85
    labels = " ".join(s.label for s in report.signals)
    assert "No game badges" in labels and "No friends" in labels


def test_aged_account_with_few_badges_is_suspicious():
    account = make_account(age_days=700, badges=3, games=3, friends=0, groups=0, badge_spread_days=600)
    report = analyse(account)
    assert report.verdict == SUSPICIOUS


def test_badge_farming_from_one_game_in_one_day():
    account = make_account(age_days=20, badges=30, games=1, friends=2, groups=1, badge_spread_days=0)
    report = analyse(account)
    labels = " ".join(s.label for s in report.signals)
    assert "only 1 game" in labels
    assert "one 24h window" in labels
    assert report.verdict != LIKELY_MAIN


def test_reactivated_old_account():
    account = make_account(age_days=900, badges=12, games=6, friends=3, groups=0)
    for i, badge in enumerate(account.badges):
        badge.awarded_at = NOW - timedelta(days=1 + i)  # all within the last 2 weeks, not in a single day
    report = analyse(account)
    labels = " ".join(s.label for s in report.signals)
    assert "only started earning badges" in labels
    assert "after the account was made" in labels


def test_hidden_badges_are_not_treated_as_zero():
    hidden = make_account(age_days=400, badges=0, badges_hidden=True)
    visible_zero = make_account(age_days=400, badges=0)
    assert analyse(hidden).score < analyse(visible_zero).score


def test_verified_and_premium_reduce_score():
    base = make_account(age_days=10, badges=2, friends=1)
    trusted = make_account(age_days=10, badges=2, friends=1, verified=True, legacy_badges=["Welcome To The Club"])
    assert analyse(trusted).score < analyse(base).score - 40


def test_thresholds_are_configurable():
    account = make_account(age_days=700, badges=3, games=3, friends=0, groups=0, badge_spread_days=600)
    score = analyse(account).score
    assert analyse(account, alt_threshold=score, suspicious_threshold=score - 1).verdict == LIKELY_ALT


def test_badge_stats():
    account = make_account(age_days=100, badges=10, games=2, badge_spread_days=0)
    account.badges.append(Badge(id=1, name="undated", awarder_id=9))
    stats = alt_detector.badge_stats(account, now=NOW)
    assert stats.total == 11
    assert stats.distinct_games == 3
    assert stats.busiest_day_share == 1.0  # all dated badges in one window


def test_score_is_clamped():
    account = make_account(
        age_days=0, badges=0, friends=0, followers=0, groups=0, description="", username="Guest123456"
    )
    assert 90 <= analyse(account).score <= 100
    assert analyse(make_account(age_days=4000, badges=900, verified=True, followers=5000)).score == 0
