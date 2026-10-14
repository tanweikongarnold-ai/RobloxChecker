import pytest

from bot.content_filter import ContentFilter, TermOverride, normalize_term
from bot.models import Group

from .helpers import make_account


@pytest.fixture(scope="module")
def cf():
    return ContentFilter.load()


def labels(cf, text, field="description"):
    return {hit.label for hit in cf.scan_text(text, field)}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("s3x", "sex"),
        ("$EX", "sex"),
        ("s e x", "sex"),
        ("s.e.x", "sex"),
        ("s*x", "sex"),
        ("s\u0435x", "sex"),  # Cyrillic e
        ("S\u0415X", "sex"),  # capital Cyrillic E
        ("s\u200bex", "sex"),  # zero-width space
        ("hornyyyy", "horny"),
        ("p0rn", "porn"),
        ("\uff30\uff4f\uff52\uff4e", "porn"),  # full-width letters
        ("n s f w", "nsfw"),
        ("add me for fun", '"add/dm me for fun"'),
        ("dm 4 fun", '"add/dm me for fun"'),
        ("looking for e-rp", "ERP (erotic roleplay)"),
        ("ERP", "ERP (erotic roleplay)"),
        ("r34 art", "rule34"),
        ("18+ only", "18+ / adults-only claim"),
        ("minors dni", '"minors DNI" / "no minors"'),
        ("scented cons", "scented con"),
    ],
)
def test_detects_evasions(cf, text, expected):
    assert expected in labels(cf, text)


@pytest.mark.parametrize(
    "text",
    [
        "I live in Essex and Sussex",
        "thorny rose garden",
        "my top ornament",
        "cucumber and documents",
        "condolences to your family",
        "watch out for the booby trap",
        "we hit 7175 visits",
        "hi! welcome to my profile",
        "I make obbies and tycoons, add me to play",
        "Cockpit simulator pilot",
        "therapist in training",
        "I'm gay and proud",
        "trans rights",
        "femboy",
        "I love roleplay (rp) games like Brookhaven",
        "Scunthorpe United fan",
    ],
)
def test_no_false_positives(cf, text):
    assert labels(cf, text) == set()


def test_camel_case_usernames(cf):
    assert "sexy" in labels(cf, "xXSexyGirlXx", "username")
    assert "sexy" in labels(cf, "sexygirl2010", "username")


def test_name_hits_weigh_more_than_group_hits(cf):
    name = cf.scan_text("HornyBoy", "username")[0]
    group = cf.scan_text("Horny club", "group")[0]
    assert name.weight > group.weight


def test_account_scan_flags_and_dedupes(cf):
    account = make_account(username="HornyKid", description="horny, 18+ dm for fun")
    account.groups = [Group(id=1, name="horny people")]
    report = cf.scan_account(account, threshold=40)
    assert report.flagged
    assert [h.label for h in report.hits].count("horny") == 1
    assert report.hits[0].field == "username"  # best (name) hit kept
    assert report.score <= 100


def test_clean_account(cf):
    report = cf.scan_account(make_account(), threshold=40)
    assert not report.flagged and report.score == 0


def test_low_weight_alone_does_not_flag(cf):
    report = cf.scan_account(make_account(description="snap: coolkid"), threshold=40)
    assert report.hits and not report.flagged


def test_blocked_avatar(cf):
    report = cf.scan_account(make_account(avatar_state="Blocked"), threshold=40)
    assert report.hits[0].category == "avatar"


def test_overrides_add_reweight_and_disable():
    cf = ContentFilter.load(
        [
            TermOverride("bad club", 50, "word"),
            TermOverride("sexy", 80, "prefix"),
            TermOverride("snap", 0),
            TermOverride("18+ / adults-only claim", 0),
        ]
    )
    assert "bad club" in labels(cf, "join the bad club")
    assert cf.scan_text("sexy", "description")[0].weight == 80
    assert labels(cf, "snap") == set()
    assert labels(cf, "18+") == set()


def test_normalize_term():
    assert normalize_term("  Bad   Word ") == "bad word"
    assert normalize_term("x") is None
    assert normalize_term("bad!") is None
