"""Card condition normalisation onto the Cardmarket scale (best first)."""

from __future__ import annotations

import re

CONDITIONS = ["MT", "NM", "EX", "GD", "LP", "PL", "PO"]
CONDITION_NAMES = {
    "MT": "Mint", "NM": "Near Mint", "EX": "Excellent", "GD": "Good",
    "LP": "Light Played", "PL": "Played", "PO": "Poor",
}
# Cardmarket's minCondition URL parameter uses these ids.
CARDMARKET_CONDITION_ID = {c: i + 1 for i, c in enumerate(CONDITIONS)}

# Shops and eBay mostly use the US-style scale (NM / LP / MP / HP / DMG).
# "Lightly played" at a UK shop is roughly Cardmarket "Excellent", etc.
# Order matters: more specific phrases first.
_GENERIC_PATTERNS: list[tuple[str, str]] = [
    (r"near[\s-]*mint|\bnm\b|\bnm/m\b|\bm/nm\b", "NM"),
    (r"\bmint\b", "MT"),
    (r"heavily[\s-]*played|\bhp\b", "PL"),
    (r"moderately[\s-]*played|\bmp\b|very[\s-]*good|\bvg\b", "GD"),
    (r"light(?:ly)?[\s-]*played|slightly[\s-]*played|\blp\b|\bsp\b|excellent|\bex\b", "EX"),
    (r"\bgood\b|\bgd\b", "GD"),
    (r"damaged|\bdmg\b|\bpoor\b", "PO"),
    (r"\bplayed\b|\bpl\b", "PL"),
]


def rank(code: str) -> int:
    return CONDITIONS.index(code)


def meets(condition: str | None, minimum: str) -> bool:
    """True if ``condition`` is at least as good as ``minimum``."""
    return condition is not None and rank(condition) <= rank(minimum)


def normalize(text: str | None) -> str | None:
    """Map free text from a shop or eBay to a Cardmarket condition code."""
    if not text:
        return None
    t = text.strip().lower()
    if t.upper() in CONDITIONS and t.upper() != "LP":
        return t.upper()
    for pattern, code in _GENERIC_PATTERNS:
        if re.search(pattern, t):
            return code
    return None


def normalize_cardmarket(text: str | None) -> str | None:
    """Cardmarket badges are already MT/NM/EX/GD/LP/PL/PO."""
    if not text:
        return None
    code = text.strip().upper()[:2]
    return code if code in CONDITIONS else normalize(text)
