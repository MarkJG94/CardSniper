"""Runs sources, evaluates their listings, records deals and sends alerts."""

from __future__ import annotations

import logging
import threading
from typing import Iterable

from .cardsdb import refresh_card_db
from .config import Config
from .currency import get_fx
from .db import Database
from .deals import DealRecorder, Evaluator
from .matching import CardIndex
from .models import ScanRun, WatchRule, utcnow
from .notify import Notifier
from .settings import get_value, load_settings
from .sources import Source, build_sources
from .sources.base import ScanContext

log = logging.getLogger("cardsniper.scan")


class Scanner:
    def __init__(self, cfg: Config, db: Database, sources: list[Source] | None = None):
        self.cfg = cfg
        self.db = db
        self.sources = sources if sources is not None else build_sources(cfg)
        self.lock = threading.Lock()
        self.current: dict | None = None
        self._index: CardIndex | None = None
        self._index_version = None

    # -- helpers ------------------------------------------------------------------
    def index(self, session) -> CardIndex:
        version = get_value(session, "carddb_imported")
        if self._index is None or version != self._index_version:
            log.info("Building card index")
            self._index = CardIndex.from_session(session)
            self._index_version = version
        return self._index

    def source(self, key_or_label: str) -> Source:
        wanted = key_or_label.lower()
        for src in self.sources:
            if wanted in (src.key.lower(), src.label.lower()):
                return src
        raise KeyError(f"No source {key_or_label!r}. Known: {', '.join(s.label for s in self.sources)}")

    def context(self, session, probe: bool = False) -> ScanContext:
        settings = load_settings(session, self.cfg.defaults)
        fx = get_fx(session, self.cfg.fallback_eur_to_gbp)
        rules = session.query(WatchRule).filter(WatchRule.enabled.is_(True)).all()
        index = self.index(session)
        return ScanContext(cfg=self.cfg, session=session, settings=settings, fx=fx, index=index,
                           evaluator=Evaluator(settings, index, fx, rules), rules=rules, log=log, probe=probe)

    # -- jobs -------------------------------------------------------------------------
    def refresh_cards(self, trigger: str = "schedule", force: bool = False) -> dict | None:
        if not self.lock.acquire(blocking=False):
            log.info("Busy - card DB refresh skipped")
            return None
        try:
            self.current = {"kind": "carddb", "source": "Scryfall", "started": utcnow()}
            with self.db.session() as s:
                run = ScanRun(kind="carddb", source="scryfall", trigger=trigger)
                s.add(run)
            try:
                result = refresh_card_db(self.db, self.cfg, force=force)
                status, message = "ok", (f"{result['total']} printings in database "
                                         f"({result['imported']} updated, {result['snapshots']} price snapshots)")
            except Exception as exc:
                log.exception("Card DB refresh failed")
                result, status, message = None, "error", str(exc)
            with self.db.session() as s:
                run = s.get(ScanRun, run.id)
                run.status, run.message, run.finished_at = status, message, utcnow()
                if result:
                    run.items_checked = result["imported"]
            return result
        finally:
            self.current = None
            self.lock.release()

    def scan(self, only: Iterable[str] | None = None, trigger: str = "schedule") -> list[int] | None:
        if not self.lock.acquire(blocking=False):
            log.info("Busy - scan skipped")
            return None
        run_ids = []
        try:
            wanted = {o.lower() for o in only} if only else None
            with self.db.session() as s:
                settings = load_settings(s, self.cfg.defaults)
            for src in self.sources:
                if wanted and src.key.lower() not in wanted and src.label.lower() not in wanted:
                    continue
                if not wanted and not settings.source_enabled(src.key):
                    continue
                run_ids.append(self._scan_source(src, trigger))
            return run_ids
        finally:
            self.current = None
            self.lock.release()

    def _scan_source(self, src: Source, trigger: str) -> int:
        with self.db.session() as s:
            run = ScanRun(kind="scan", source=src.key, trigger=trigger)
            s.add(run)
        unavailable = src.available()
        if unavailable:
            with self.db.session() as s:
                r = s.get(ScanRun, run.id)
                r.status, r.message, r.finished_at = "skipped", unavailable, utcnow()
            log.info("%s skipped: %s", src.label, unavailable)
            return run.id

        self.current = {"kind": "scan", "source": src.label, "started": utcnow(), "listings": 0, "deals": 0}
        log.info("Scanning %s", src.label)
        session = self.db.Session()
        seen = deals = alerts = 0
        status, fatal = "ok", None
        ctx = None
        try:
            ctx = self.context(session)
            recorder = DealRecorder(session, ctx.settings, Notifier(self.cfg, ctx.settings))
            for listing in src.scan(ctx):
                seen += 1
                self.current["listings"] = seen
                try:
                    ev = ctx.evaluator.evaluate(listing)
                    if ev.is_deal:
                        deal, sent = recorder.record(ev)
                        deals += 1
                        alerts += int(sent)
                        self.current["deals"] = deals
                        session.commit()
                        log.info("Deal: %s £%.2f (%s) %s", deal.card_name, deal.price_gbp, src.label,
                                 "ALERTED" if sent else f"[{deal.suppressed_reason}]")
                except Exception as exc:
                    session.rollback()
                    ctx.error(f"{listing.title[:80]}: {exc}")
                if seen % 250 == 0:
                    session.commit()
            session.commit()
        except Exception as exc:
            session.rollback()
            log.exception("%s scan failed", src.label)
            status, fatal = "error", str(exc)
        finally:
            session.close()

        stats = ctx.stats if ctx else {"items": 0, "errors": 0, "error_messages": []}
        if status == "ok" and stats["errors"]:
            status = "partial"
        message_parts = []
        if fatal:
            message_parts.append(fatal)
        message_parts += stats["error_messages"]
        with self.db.session() as s:
            r = s.get(ScanRun, run.id)
            r.status, r.finished_at = status, utcnow()
            r.items_checked, r.listings_seen = stats["items"], seen
            r.deals_found, r.alerts_sent, r.errors = deals, alerts, stats["errors"] + (1 if fatal else 0)
            r.message = "\n".join(message_parts)[:5000] or None
        log.info("%s done: %d checked, %d listings, %d deals, %d alerts, %d errors",
                 src.label, stats["items"], seen, deals, alerts, stats["errors"])
        return run.id
