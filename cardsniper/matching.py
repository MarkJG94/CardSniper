"""Match free-text listing titles (eBay, shops) to card printings.

The matcher is deliberately conservative: when a title does not pin down a
single printing, the deal is judged against the *cheapest* printing that
fits, so a cheap reprint can never masquerade as a discounted original.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(text: str | None) -> str:
    """lowercase, strip accents and punctuation: "Æther Vial, Urza's" -> "aether vial urzas"."""
    if not text:
        return ""
    text = text.replace("Æ", "Ae").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", "").replace("’", "").replace("‘", "")
    return _NON_ALNUM.sub(" ", text).strip()


# Scryfall promo types / frame effects we care about, and the words sellers use for them.
TREATMENT_KEYWORDS: dict[str, list[str]] = {
    "borderless": ["borderless"],
    "extendedart": ["extended art", "extended"],
    "showcase": ["showcase"],
    "etched": ["etched", "foil etched"],
    "fullart": ["full art", "fullart"],
    "retro": ["retro frame", "retro", "old frame", "old border"],
    "serialized": ["serialized", "serialised", "serial numbered"],
    "surgefoil": ["surge foil", "surge"],
    "textured": ["textured"],
    "galaxyfoil": ["galaxy foil"],
    "halofoil": ["halo foil"],
    "confettifoil": ["confetti foil"],
    "gilded": ["gilded"],
    "oilslick": ["oil slick"],
    "stepandcompleat": ["step and compleat"],
    "prerelease": ["prerelease", "pre release"],
    "promopack": ["promo pack"],
}
_TRACKED_PROMOS = {k for k in TREATMENT_KEYWORDS if k not in ("borderless", "extendedart", "showcase",
                                                                "etched", "fullart", "retro")}

EXCLUDE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("graded card", re.compile(r"\b(psa|bgs|cgc|beckett|graded|slab|slabbed|ace grading|tag grading)\b", re.I)),
    ("not a genuine single", re.compile(
        r"\b(proxy|proxies|custom|altered|alter|playtest|orica|fake|replica|mtgo|arena code|code card|"
        r"art card|art series|oversized|jumbo|misprint|signed|autograph|autographed|artist proof)\b", re.I)),
    ("multiple cards", re.compile(r"\b(lot|job lot|bundle|playset|set of|x[2-9]|[2-9]x|[2-9] x|x [2-9])\b", re.I)),
    ("sealed product / accessory", re.compile(
        r"\b(booster|display box|deck box|sleeves|playmat|binder|precon|starter kit|fat pack)\b", re.I)),
]
NON_ENGLISH = re.compile(
    r"\b(japanese|jpn|jp|german|deutsch|ger|french|francais|français|fr|italian|italiano|ita|spanish|"
    r"espanol|español|spa|portuguese|chinese|korean|russian|phyrexian language)\b", re.I)
_NON_LATIN = re.compile(r"[Ѐ-ӿ぀-ヿ㐀-鿿가-힯]")
_FOIL = re.compile(r"\bfoil\b|\bfoils\b", re.I)
_NON_FOIL = re.compile(r"\bnon[\s-]?foil\b|\bnonfoil\b|\bnot foil\b", re.I)
_ETCHED = re.compile(r"\betched\b", re.I)
_COLLECTOR = re.compile(r"(?:#|no\.?\s*)(\d{1,4}[a-z]?)\b|\b(\d{1,4})\s*/\s*\d{2,4}\b", re.I)


@dataclass(slots=True)
class CardRef:
    id: str
    name: str
    name_norm: str
    set_code: str
    set_name: str
    set_norm: str
    collector_number: str
    finishes: tuple[str, ...]
    treatments: frozenset[str]
    eur: float | None
    eur_foil: float | None

    def eur_for(self, finish: str) -> float | None:
        return self.eur if finish == "nonfoil" else self.eur_foil

    @property
    def printing(self) -> str:
        return f"{self.set_name} #{self.collector_number}"


@dataclass
class Match:
    name_norm: str
    display_name: str
    finish: str  # nonfoil | foil | etched
    candidates: list[CardRef]
    set_matched: bool = False
    treatments: set[str] = field(default_factory=set)

    @property
    def unique(self) -> CardRef | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    def reference_eur(self) -> tuple[float, CardRef] | None:
        """Cheapest known market price among the printings this listing could be."""
        priced = [(c.eur_for(self.finish), c) for c in self.candidates if c.eur_for(self.finish)]
        if not priced:
            return None
        return min(priced, key=lambda p: p[0])


def treatments_for(card: dict) -> list[str]:
    """Derive treatment tags from a Scryfall card object."""
    out: set[str] = set()
    if card.get("border_color") == "borderless":
        out.add("borderless")
    effects = set(card.get("frame_effects") or [])
    for key in ("extendedart", "showcase", "etched", "inverted"):
        if key in effects:
            out.add(key)
    if card.get("full_art"):
        out.add("fullart")
    if card.get("frame") == "1997" and (card.get("released_at") or "") >= "2020":
        out.add("retro")
    for promo in card.get("promo_types") or []:
        if promo in _TRACKED_PROMOS:
            out.add(promo)
    if "etched" in (card.get("finishes") or []) and "etched" not in out and len(card.get("finishes")) == 1:
        out.add("etched")
    return sorted(out)


def exclusion_reason(title: str) -> str | None:
    for reason, pattern in EXCLUDE_PATTERNS:
        if pattern.search(title):
            return reason
    if NON_ENGLISH.search(title) or _NON_LATIN.search(title):
        return "not English"  # (callers pass raw or normalised text)
    return None


def detect_finish(text: str) -> str:
    if _ETCHED.search(text):
        return "etched"
    if _FOIL.search(text) and not _NON_FOIL.search(text):
        return "foil"
    return "nonfoil"


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {haystack} "


class CardIndex:
    """In-memory name -> printings index built from the cards table."""

    def __init__(self, cards: Iterable[CardRef], front_names: dict[str, str] | None = None):
        self.by_name: dict[str, list[CardRef]] = {}
        self.by_id: dict[str, CardRef] = {}
        for c in cards:
            self.by_name.setdefault(c.name_norm, []).append(c)
            self.by_id[c.id] = c
        # front faces of double-faced / adventure cards ("Bonecrusher Giant")
        for front, full in (front_names or {}).items():
            if front not in self.by_name and full in self.by_name:
                self.by_name[front] = self.by_name[full]
        self.max_tokens = max((len(n.split()) for n in self.by_name), default=1)

    @classmethod
    def from_session(cls, session) -> "CardIndex":
        from .models import Card

        from sqlalchemy import func

        rows = session.query(
            Card.id, Card.name, Card.name_norm, Card.front_norm, Card.set_code, Card.set_name,
            Card.collector_number, Card.finishes, Card.treatments,
            # market price: Cardmarket's own price guide, else Scryfall's copy of it
            func.coalesce(Card.cm_trend, Card.eur).label("eur"),
            func.coalesce(Card.cm_trend_foil, Card.eur_foil).label("eur_foil"),
        ).yield_per(5000)
        refs, fronts = [], {}
        for r in rows:
            refs.append(CardRef(
                id=r.id, name=r.name, name_norm=r.name_norm, set_code=r.set_code, set_name=r.set_name,
                set_norm=norm(r.set_name), collector_number=r.collector_number,
                finishes=tuple(f for f in r.finishes.split(",") if f),
                treatments=frozenset(t for t in r.treatments.split(",") if t),
                eur=r.eur, eur_foil=r.eur_foil,
            ))
            if r.front_norm and r.front_norm != r.name_norm:
                fronts[r.front_norm] = r.name_norm
        return cls(refs, fronts)

    def __len__(self) -> int:
        return len(self.by_id)

    # -- name detection -------------------------------------------------
    def find_name(self, title_norm: str, hint: str | None = None, hint_exact: bool = False) -> str | None:
        hint_norm = norm(hint) if hint else ""
        if hint_exact and hint_norm in self.by_name:
            return hint_norm
        tokens = title_norm.split()
        best: tuple[int, int, str] | None = None
        for i in range(len(tokens)):
            for size in range(min(self.max_tokens, len(tokens) - i), 0, -1):
                phrase = " ".join(tokens[i:i + size])
                if phrase in self.by_name:
                    if best is None or size > best[0]:
                        best = (size, i, phrase)
                    break
        if hint_norm in self.by_name and _contains_phrase(title_norm, hint_norm):
            # prefer the name we searched for, unless a longer card name contains it
            if best is None or not (best[0] > len(hint_norm.split()) and hint_norm in best[2]):
                return hint_norm
        return best[2] if best else None

    # -- full match -------------------------------------------------------
    def match(self, title: str, name_hint: str | None = None, set_hint: str | None = None,
              finish: str | None = None, hint_exact: bool = False) -> tuple[Match | None, str | None]:
        """Return (match, None) or (None, reason it was rejected).

        ``hint_exact``: the name hint came from a structured title ("Name [Set]"), so trust it
        over longer names found in the text.
        """
        if _NON_LATIN.search(title):
            return None, "not English"
        title_norm = norm(title)
        name = self.find_name(title_norm, name_hint, hint_exact)
        if not name:
            return None, exclusion_reason(title_norm) or "no card name recognised"
        printings = self.by_name[name]
        # everything except the card name - so "Alter Fate" or "Booster Tutor" aren't mistaken for junk
        rest = _remove_phrase(title_norm, name)

        # set / edition
        candidates = list(printings)
        set_matched = False
        set_text = norm(set_hint) if set_hint else rest
        by_set = [c for c in candidates if _contains_phrase(set_text, c.set_norm)]
        if by_set:
            longest = max(len(c.set_norm) for c in by_set)
            candidates = [c for c in by_set if len(c.set_norm) == longest]
            set_matched = True
            rest = _remove_phrase(rest, candidates[0].set_norm)  # "Mystery Booster 2" is not a booster
        else:
            coded = [c for c in candidates
                     if re.search(rf"(?<![A-Za-z0-9]){re.escape(c.set_code.upper())}(?![A-Za-z0-9])",
                                  set_hint or title)]
            if coded:
                candidates, set_matched = coded, True

        reason = exclusion_reason(rest)
        if reason:
            return None, reason

        # collector number
        numbers = {(a or b).lower().lstrip("0") for a, b in _COLLECTOR.findall(title)}
        if numbers:
            by_number = [c for c in candidates if c.collector_number.lower().lstrip("0") in numbers]
            if by_number:
                candidates = by_number

        # special treatments mentioned in the title
        treatments: set[str] = set()
        for key, words in TREATMENT_KEYWORDS.items():
            if any(_contains_phrase(rest, w) for w in words):
                treatments.add(key)
        for t in treatments:
            narrowed = [c for c in candidates if t in c.treatments]
            if narrowed:
                candidates = narrowed

        # finish: explicit from the source or the title, otherwise non-foil unless only foils fit
        detected = detect_finish(rest)
        explicit = finish is not None or detected != "nonfoil" or bool(_NON_FOIL.search(rest))
        finish = finish or detected
        by_finish = [c for c in candidates if _supports(c, finish)]
        if not by_finish and not explicit:
            for alt in ("foil", "etched"):
                by_finish = [c for c in candidates if alt in c.finishes]
                if by_finish:
                    finish = alt
                    break
        if not by_finish:
            return None, f"no {finish} printing of {printings[0].name}" + (" in that set" if set_matched else "")

        return Match(name_norm=name, display_name=printings[0].name, finish=finish,
                     candidates=by_finish, set_matched=set_matched, treatments=treatments), None


def _supports(card: CardRef, finish: str) -> bool:
    return finish in card.finishes or (finish == "etched" and "foil" in card.finishes)


def _remove_phrase(text: str, phrase: str) -> str:
    return " ".join(f" {text} ".replace(f" {phrase} ", " ", 1).split())
