"""Price parsing and EUR->GBP conversion."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date

import httpx
from sqlalchemy.orm import Session

from .settings import get_value, set_value

log = logging.getLogger(__name__)

_FX_KEY = "fx_eur_gbp"
_PRICE_RE = re.compile(
    r"(?P<pre>£|€|GBP|EUR)?\s*(?P<num>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?P<post>£|€|GBP|EUR)?",
    re.I,
)


def parse_price(text: str | None, default_currency: str = "GBP") -> tuple[float, str] | None:
    """Parse '£12.50', '12,50 €', 'EUR 1.234,56' into (amount, currency).

    Amounts with a currency marker win over bare numbers ("3 in stock £2.50" -> 2.50).
    """
    if not text:
        return None
    bare: tuple[float, str] | None = None
    for m in _PRICE_RE.finditer(text):
        sym = (m.group("pre") or m.group("post") or "").upper()
        amount = _to_float(m.group("num"))
        if amount is None:
            continue
        if sym:
            return amount, {"£": "GBP", "€": "EUR"}.get(sym, sym)
        if bare is None:
            bare = (amount, default_currency)
    return bare


def _to_float(num: str) -> float | None:
    num = num.replace(" ", "")
    if "," in num and "." in num:
        # whichever separator comes last is the decimal point
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else num.replace(",", "")
    try:
        return float(num)
    except ValueError:
        return None


class Fx:
    """EUR->GBP rate, refreshed once a day from the ECB."""

    def __init__(self, eur_to_gbp: float):
        self.eur_to_gbp = eur_to_gbp

    def to_gbp(self, amount: float | None, currency: str = "GBP") -> float | None:
        if amount is None:
            return None
        currency = currency.upper()
        if currency == "GBP":
            return round(amount, 2)
        if currency == "EUR":
            return round(amount * self.eur_to_gbp, 2)
        raise ValueError(f"Unsupported currency {currency}")

    def eur_to_gbp_amount(self, amount: float | None) -> float | None:
        return None if amount is None else amount * self.eur_to_gbp


def fetch_rate() -> float:
    urls = [
        ("https://api.frankfurter.dev/v1/latest?base=EUR&symbols=GBP", "json"),
        ("https://api.frankfurter.app/latest?from=EUR&to=GBP", "json"),
        ("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", "xml"),
    ]
    last_error: Exception | None = None
    for url, kind in urls:
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            r.raise_for_status()
            if kind == "json":
                return float(r.json()["rates"]["GBP"])
            for el in ET.fromstring(r.text).iter():
                if el.attrib.get("currency") == "GBP":
                    return float(el.attrib["rate"])
        except Exception as exc:  # try the next provider
            last_error = exc
    raise RuntimeError(f"Could not fetch EUR/GBP rate: {last_error}")


def get_fx(session: Session, fallback: float, refresh: bool = False, fetch: bool = True) -> Fx:
    """Today's rate. Fetched at most once a day (``refresh`` forces it); ``fetch=False`` never
    touches the network and just uses the last known rate."""
    stored = get_value(session, _FX_KEY)
    today = date.today().isoformat()
    if stored and (not fetch or (stored.get("date") == today and not refresh)):
        return Fx(stored["rate"])
    if not fetch:
        return Fx(fallback)
    try:
        rate = fetch_rate()
    except Exception as exc:
        log.warning("%s - using %s", exc, "last known rate" if stored else "fallback rate")
        rate = stored["rate"] if stored else fallback
    # remember the attempt so failures don't retry on every call today
    set_value(session, _FX_KEY, {"date": today, "rate": rate})
    return Fx(rate)
