"""Cardmarket's official daily price guide (a public download - no scraping).

The file lists, per Cardmarket product, EU-wide figures: trend, the lowest
listing right now, and average sale prices over 1/7/30 days, for normal and
foil copies. It has no individual listings or seller countries.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import bindparam
from sqlalchemy.orm import Session

from .config import Config
from .models import Card, utcnow
from .settings import get_value, set_value

log = logging.getLogger(__name__)

# price guide field -> cards column
FIELDS = {
    "trend": "cm_trend", "low": "cm_low", "avg1": "cm_avg1", "avg7": "cm_avg7", "avg30": "cm_avg30",
    "trend-foil": "cm_trend_foil", "low-foil": "cm_low_foil", "avg1-foil": "cm_avg1_foil",
    "avg7-foil": "cm_avg7_foil", "avg30-foil": "cm_avg30_foil",
}
_STATE_KEY = "price_guide"


def _key(name: str) -> str:
    """'low-foil', 'low_foil', 'lowFoil' -> 'low-foil'."""
    name = re.sub(r"(?<=[a-z0-9])Foil$", "-foil", name).lower().replace("_", "-")
    return name


def _value(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_price_guide(data) -> tuple[str | None, list[dict]]:
    """Return (createdAt, rows) where each row has 'id' plus cm_* columns."""
    created = None
    rows = data
    if isinstance(data, dict):
        created = data.get("createdAt")
        rows = data.get("priceGuides") or data.get("priceguides") or []
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        norm = {_key(k): v for k, v in raw.items()}
        pid = norm.get("idproduct")
        if pid is None:
            continue
        row = {"id": int(pid)}
        for field, column in FIELDS.items():
            row[column] = _value(norm.get(field))
        out.append(row)
    return created, out


def download(cfg: Config) -> dict:
    from curl_cffi import requests as cffi_requests

    r = cffi_requests.get(cfg.cardmarket.price_guide_url, impersonate=cfg.http.impersonate,
                          timeout=180, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"Cardmarket price guide download failed: HTTP {r.status_code} "
                           f"from {cfg.cardmarket.price_guide_url}")
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"Cardmarket price guide is not JSON ({r.text[:120]!r})") from exc


def import_rows(session: Session, rows: list[dict]) -> int:
    """Write price guide figures onto every printing with a matching Cardmarket id."""
    now = utcnow()
    known = {pid for (pid,) in session.query(Card.cardmarket_id).filter(Card.cardmarket_id.isnot(None))}
    params = [{"pid": r["id"], "updated": now, **{c: r[c] for c in FIELDS.values()}}
              for r in rows if r["id"] in known]
    if not params:
        return 0
    table = Card.__table__
    stmt = (table.update().where(table.c.cardmarket_id == bindparam("pid"))
            .values(cm_updated=bindparam("updated"), **{c: bindparam(c) for c in FIELDS.values()}))
    conn = session.connection()
    for i in range(0, len(params), 5000):
        conn.execute(stmt, params[i:i + 5000])
    return len(params)


def refresh_price_guide(session: Session, cfg: Config, force: bool = False) -> dict | None:
    """Download and import the price guide unless it was fetched recently. Returns stats or None if skipped."""
    state = get_value(session, _STATE_KEY) or {}
    fetched = state.get("fetched_at")
    if fetched and not force:
        age = utcnow() - datetime.fromisoformat(fetched)
        if age < timedelta(hours=cfg.cardmarket.refresh_hours):
            return None
    created, rows = parse_price_guide(download(cfg))
    updated = import_rows(session, rows)
    set_value(session, _STATE_KEY, {"fetched_at": utcnow().isoformat(), "created_at": created,
                                    "products": len(rows), "printings_updated": updated})
    log.info("Cardmarket price guide %s: %d products, %d printings updated", created, len(rows), updated)
    return {"created_at": created, "products": len(rows), "printings_updated": updated}


def state(session: Session) -> dict:
    return get_value(session, _STATE_KEY) or {}
