"""Detects sexual / erotic content in Roblox profiles.

Text is normalised before matching so common evasions still hit:

* look-alike letters (Cyrillic ``е``, full-width letters, zero-width spaces)
* leetspeak inside words (``s3x``, ``p0rn``, ``$lut``)
* spaced-out letters (``s e x``, ``n.s.f.w``)
* stretched letters (``hornyyy``, ``seeexy``) and ``*`` censoring (``s*x``)
* CamelCase usernames (``xXSexyGirlXx``)

Each rule has a weight; the best hit per rule is kept and the weights are summed
into a 0..100 score. The account is flagged when the score reaches the server's
threshold. Moderators can add, re-weight or disable terms per server.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import AccountData

DEFAULT_TERMS_PATH = Path(__file__).resolve().parent.parent / "data" / "nsfw_terms.json"

MATCH_MODES = ("word", "prefix", "substring")

# How much a hit counts depending on where it was found. Names are chosen on
# purpose and shown everywhere, group membership is weaker evidence.
FIELD_WEIGHTS = {
    "username": 1.2,
    "display name": 1.2,
    "description": 1.0,
    "past username": 1.0,
    "group": 0.6,
}
AVATAR_BLOCKED_WEIGHT = 25

_ZERO_WIDTH = re.compile(r"[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")
_CONFUSABLE_MAP = {
    "а": "a", "е": "e", "ё": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "к": "k", "м": "m", "т": "t", "в": "b",
    "н": "h", "ԁ": "d", "ɡ": "g", "ı": "i", "ο": "o", "α": "a", "ε": "e", "ν": "v",
    "τ": "t", "κ": "k", "ρ": "p", "χ": "x",
}
# Map capital look-alikes too (Cyrillic "Е" -> "E"); lowercasing happens after folding.
_CONFUSABLE_MAP.update(
    {k.upper(): v.upper() for k, v in list(_CONFUSABLE_MAP.items()) if len(k.upper()) == 1 and k.upper() != k}
)
_CONFUSABLES = str.maketrans(_CONFUSABLE_MAP)
_LEET_DIGITS = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b"})
_LEET_SYMBOLS = {"@": "a", "$": "s", "!": "i", "|": "l", "€": "e"}
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_LETTER = re.compile(r"[^a-z*]+")
_SPACED_LETTERS = re.compile(r"(?<![a-z*])(?:[a-z*] ){2,}[a-z*](?![a-z*])")
_TERM_CHARS = re.compile(r"^[a-z ]+$")


@dataclass(slots=True)
class Rule:
    key: str
    label: str
    category: str
    weight: int
    regex: re.Pattern[str]
    raw: bool = False  # patterns run on the raw text, terms on the normalised views


@dataclass(slots=True)
class Hit:
    label: str
    category: str
    field: str
    weight: int
    matched: str


@dataclass(slots=True)
class NsfwReport:
    score: int
    flagged: bool
    hits: list[Hit] = field(default_factory=list)


@dataclass(slots=True)
class TermOverride:
    term: str
    weight: int
    match: str = "word"
    category: str = "custom"


def normalize_term(term: str) -> str | None:
    """Clean up a moderator-supplied term; ``None`` if it can't be used."""
    text = " ".join(_fold(term).lower().split())
    if not 2 <= len(text) <= 50 or not _TERM_CHARS.match(text):
        return None
    return text


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return text.translate(_CONFUSABLES)


def _letters_only(text: str) -> str:
    text = _NOT_LETTER.sub(" ", text).strip()
    return _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)


def _deleet(text: str) -> str:
    out = []
    for token in text.split():
        if not any(ch.isalpha() for ch in token):
            out.append(token)  # leave plain numbers alone ("7175" is not "tits")
            continue
        chars = list(token.translate(_LEET_DIGITS))
        for i, ch in enumerate(chars):
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if ch in _LEET_SYMBOLS and nxt.isalpha():
                chars[i] = _LEET_SYMBOLS[ch]
        out.append("".join(chars))
    return " ".join(out)


def views(text: str) -> tuple[str, list[str]]:
    """Return ``(raw, [plain, leet])`` normalised views of ``text``."""
    folded = _fold(text)
    raw = folded.lower()
    split = _CAMEL.sub(" ", folded).lower()
    plain = _letters_only(split)
    leet = _letters_only(_deleet(split))
    return raw, ([plain] if leet == plain else [plain, leet])


def compile_term(term: str, match: str) -> re.Pattern[str]:
    parts = []
    for ch in term:
        if ch == " ":
            parts.append(r"\s+")
            continue
        options = ch + ("*" if ch in "aeiouy" else "") + ("i" if ch == "l" else "")
        parts.append((re.escape(ch) if len(options) == 1 else "[" + re.escape(options) + "]") + "+")
    body = "".join(parts)
    if match == "word":
        return re.compile(rf"(?<![a-z*]){body}(?![a-z*])")
    if match == "prefix":
        return re.compile(rf"(?<![a-z*]){body}")
    return re.compile(body)


class ContentFilter:
    def __init__(self, rules: list[Rule]):
        self.rules = rules

    @classmethod
    def load(
        cls,
        overrides: Iterable[TermOverride] = (),
        path: Path = DEFAULT_TERMS_PATH,
    ) -> "ContentFilter":
        data = json.loads(path.read_text(encoding="utf-8"))
        terms: dict[str, tuple[int, str, str]] = {}
        for entry in data.get("terms", []):
            key = normalize_term(entry["term"])
            if key:
                terms[key] = (int(entry["weight"]), entry.get("match", "word"), entry.get("category", "sexual"))
        for override in overrides:
            key = normalize_term(override.term)
            if not key:
                continue
            if override.weight <= 0:
                terms.pop(key, None)
            else:
                match = override.match if override.match in MATCH_MODES else "word"
                terms[key] = (override.weight, match, override.category)

        rules = [
            Rule(key=key, label=key, category=category, weight=weight, regex=compile_term(key, match))
            for key, (weight, match, category) in terms.items()
        ]
        disabled = {normalize_term(o.term) or o.term for o in overrides if o.weight <= 0}
        for entry in data.get("patterns", []):
            label = entry["label"]
            if label.lower() in disabled:
                continue
            rules.append(
                Rule(
                    key=f"pattern:{label}",
                    label=label,
                    category=entry.get("category", "sexual"),
                    weight=int(entry["weight"]),
                    regex=re.compile(entry["pattern"]),
                    raw=True,
                )
            )
        return cls(rules)

    def scan_text(self, text: str, field_name: str = "description") -> list[Hit]:
        if not text or not text.strip():
            return []
        raw, normalised = views(text)
        multiplier = FIELD_WEIGHTS.get(field_name, 1.0)
        hits = []
        for rule in self.rules:
            haystacks = [raw] if rule.raw else normalised
            for haystack in haystacks:
                found = rule.regex.search(haystack)
                if found:
                    hits.append(
                        Hit(
                            label=rule.label,
                            category=rule.category,
                            field=field_name,
                            weight=round(rule.weight * multiplier),
                            matched=found.group(0).strip(),
                        )
                    )
                    break
        return hits

    def scan_fields(self, fields: Iterable[tuple[str, str]], threshold: int, extra: Iterable[Hit] = ()) -> NsfwReport:
        best: dict[str, Hit] = {}
        for field_name, text in fields:
            for hit in self.scan_text(text, field_name):
                current = best.get(hit.label)
                if current is None or hit.weight > current.weight:
                    best[hit.label] = hit
        for hit in extra:
            best[hit.label] = hit
        hits = sorted(best.values(), key=lambda h: -h.weight)
        score = min(100, sum(h.weight for h in hits))
        return NsfwReport(score=score, flagged=score >= threshold, hits=hits)

    def scan_account(self, account: AccountData, threshold: int = 40) -> NsfwReport:
        fields: list[tuple[str, str]] = [
            ("username", account.username),
            ("display name", account.display_name),
            ("description", account.description),
        ]
        fields += [("past username", name) for name in account.username_history]
        fields += [("group", group.name) for group in account.groups or []]
        extra = []
        if (account.avatar_state or "").lower() == "blocked":
            extra.append(
                Hit(
                    label="Avatar image blocked by Roblox moderation",
                    category="avatar",
                    field="avatar",
                    weight=AVATAR_BLOCKED_WEIGHT,
                    matched="",
                )
            )
        return self.scan_fields(fields, threshold, extra)
