"""Import every paper printing from Scryfall's daily bulk file."""

from __future__ import annotations

import gzip
import json
import logging
from datetime import date
from pathlib import Path
from typing import Callable, Iterator

import httpx
from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert

from .config import Config
from .currency import get_fx
from .db import Database
from .matching import norm, treatments_for
from .models import Card, PriceHistory, WatchRule, utcnow
from .settings import get_value, load_settings, set_value

log = logging.getLogger(__name__)

BULK_INDEX = "https://api.scryfall.com/bulk-data"
HEADERS = {"User-Agent": "CardSniper/0.1 (personal price tracker)", "Accept": "application/json"}
SKIP_LAYOUTS = {"art_series", "emblem", "vanguard", "scheme", "planar", "reversible_card"}
FRONT_NAME_LAYOUTS = {"transform", "modal_dfc", "adventure", "flip", "meld", "prototype", "battle"}
_UPDATE_COLUMNS = ["oracle_id", "name", "name_norm", "front_norm", "set_code", "set_name",
                   "collector_number", "rarity", "released_at", "finishes", "treatments",
                   "cardmarket_id", "eur", "eur_foil", "image_url", "scryfall_uri", "updated_at"]


def _price(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def card_row(obj: dict) -> dict | None:
    """Scryfall card object -> cards table row, or None to skip."""
    if obj.get("lang") != "en" or obj.get("digital") or "paper" not in (obj.get("games") or []):
        return None
    if obj.get("layout") in SKIP_LAYOUTS or obj.get("oversized"):
        return None
    prices = obj.get("prices") or {}
    faces = obj.get("card_faces") or []
    image = (obj.get("image_uris") or {}).get("normal")
    if not image and faces:
        image = (faces[0].get("image_uris") or {}).get("normal")
    front = None
    if faces and obj.get("layout") in FRONT_NAME_LAYOUTS:
        front = norm(faces[0].get("name"))
    finishes = [f for f in obj.get("finishes") or ["nonfoil"] if f in ("nonfoil", "foil", "etched")]
    return {
        "id": obj["id"],
        "oracle_id": obj.get("oracle_id") or (faces[0].get("oracle_id") if faces else None),
        "name": obj["name"],
        "name_norm": norm(obj["name"]),
        "front_norm": front,
        "set_code": obj["set"],
        "set_name": obj["set_name"],
        "collector_number": obj["collector_number"],
        "rarity": obj.get("rarity"),
        "released_at": obj.get("released_at"),
        "finishes": ",".join(finishes) or "nonfoil",
        "treatments": ",".join(treatments_for(obj)),
        "cardmarket_id": obj.get("cardmarket_id"),
        "eur": _price(prices.get("eur")),
        "eur_foil": _price(prices.get("eur_foil")),
        "image_url": image,
        "scryfall_uri": obj.get("scryfall_uri"),
        "updated_at": utcnow(),
    }


def download_bulk(data_dir: Path, force: bool = False) -> tuple[Path, str]:
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        index = client.get(BULK_INDEX).json()["data"]
        entry = next(d for d in index if d["type"] == "default_cards")
        url = entry.get("jsonl_download_uri") or entry.get("download_uri")
        target = data_dir / ("default-cards.jsonl.gz" if url.endswith(".gz") else "default-cards.json")
        marker = target.with_suffix(".updated_at")
        if target.exists() and marker.exists() and marker.read_text() == entry["updated_at"] and not force:
            log.info("Scryfall bulk file unchanged (%s)", entry["updated_at"])
            return target, entry["updated_at"]
        log.info("Downloading %s", url)
        tmp = target.with_suffix(".part")
        with client.stream("GET", url, timeout=600) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        tmp.replace(target)
        marker.write_text(entry["updated_at"])
        return target, entry["updated_at"]


def iter_bulk(path: Path) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        first = fh.read(1)
        fh.seek(0)
        if first == "[":  # classic JSON array
            yield from json.load(fh)
            return
        for line in fh:
            line = line.strip().rstrip(",")
            if line and line not in ("[", "]"):
                yield json.loads(line)


def import_cards(db: Database, cards: Iterator[dict], batch_size: int = 2000,
                 progress: Callable[[int], None] | None = None) -> int:
    count = 0
    batch: list[dict] = []

    def flush():
        if not batch:
            return
        stmt = insert(Card).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Card.id], set_={c: stmt.excluded[c] for c in _UPDATE_COLUMNS})
        with db.session() as s:
            s.execute(stmt)
        batch.clear()

    for obj in cards:
        row = card_row(obj)
        if row is None:
            continue
        batch.append(row)
        count += 1
        if len(batch) >= batch_size:
            flush()
            if progress:
                progress(count)
    flush()
    return count


def snapshot_prices(db: Database, cfg: Config) -> int:
    """Store today's reference prices for cards worth watching (dashboard charts)."""
    today = date.today()
    with db.session() as s:
        settings = load_settings(s, cfg.defaults)
        fx = get_fx(s, cfg.fallback_eur_to_gbp)
        floor_eur = settings.min_reference_gbp * 0.5 / fx.eur_to_gbp
        watched_ids = {r.card_id for r in s.query(WatchRule).filter(WatchRule.card_id.isnot(None))}
        watched_names = {r.oracle_name for r in s.query(WatchRule).filter(WatchRule.oracle_name.isnot(None))}
        q = select(Card.id, Card.eur, Card.eur_foil).where(or_(
            Card.eur >= floor_eur, Card.eur_foil >= floor_eur,
            Card.id.in_(watched_ids or [""]), Card.name_norm.in_(watched_names or [""])))
        rows = []
        for cid, eur, eur_foil in s.execute(q):
            for finish, value in (("nonfoil", eur), ("foil", eur_foil)):
                if value:
                    rows.append({"card_id": cid, "finish": finish, "day": today,
                                 "ref_gbp": round(value * fx.eur_to_gbp, 2)})
        for i in range(0, len(rows), 2000):
            stmt = insert(PriceHistory).values(rows[i:i + 2000])
            s.execute(stmt.on_conflict_do_update(
                index_elements=["card_id", "finish", "day"], set_={"ref_gbp": stmt.excluded.ref_gbp}))
        return len(rows)


def refresh_card_db(db: Database, cfg: Config, force: bool = False) -> dict:
    with db.session() as s:
        get_fx(s, cfg.fallback_eur_to_gbp, refresh=True)
    path, updated_at = download_bulk(cfg.data_dir, force=force)
    with db.session() as s:
        last = get_value(s, "carddb_imported")
    imported = 0
    if force or last != updated_at:
        imported = import_cards(db, iter_bulk(path),
                                progress=lambda n: log.info("imported %d printings", n) if n % 20000 == 0 else None)
        with db.session() as s:
            set_value(s, "carddb_imported", updated_at)
    snapshots = snapshot_prices(db, cfg)
    with db.session() as s:
        total = s.query(Card).count()
    log.info("Card DB refreshed: %d printings imported, %d in DB, %d price snapshots", imported, total, snapshots)
    return {"imported": imported, "total": total, "snapshots": snapshots, "scryfall_updated_at": updated_at}
