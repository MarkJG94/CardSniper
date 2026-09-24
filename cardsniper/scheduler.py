"""Background schedule: card database refresh + scans, intervals set from the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .models import Card, ScanRun
from .scanner import Scanner
from .settings import UserSettings, load_settings

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, scanner: Scanner):
        self.scanner = scanner
        self.cfg = scanner.cfg
        self.db = scanner.db
        self.sched = BackgroundScheduler(timezone=self.cfg.timezone,
                                         job_defaults={"coalesce": True, "max_instances": 1,
                                                       "misfire_grace_time": 3600})

    def _last(self, kind: str) -> datetime | None:
        with self.db.session() as s:
            run = (s.query(ScanRun).filter(ScanRun.kind == kind, ScanRun.status != "running")
                   .order_by(ScanRun.started_at.desc()).first())
            return run.started_at.replace(tzinfo=timezone.utc) if run else None

    def _next_time(self, kind: str, hours: float) -> datetime:
        now = datetime.now(timezone.utc)
        last = self._last(kind)
        if last is None:
            return now + timedelta(seconds=30)
        return max(last + timedelta(hours=hours), now + timedelta(seconds=30))

    def start(self) -> None:
        with self.db.session() as s:
            settings = load_settings(s, self.cfg.defaults)
            empty = s.query(Card.id).first() is None
            # runs cut short by a restart
            for run in s.query(ScanRun).filter(ScanRun.status == "running"):
                run.status, run.message = "error", "interrupted (app restarted)"
        self._schedule(settings, bootstrapping=empty)
        if empty:
            # first start: build the card database, then scan straight away
            self.sched.add_job(self._bootstrap, id="bootstrap", next_run_time=datetime.now(timezone.utc))
        self.sched.start()
        log.info("Scheduler started: scan every %sh, card DB every %sh",
                 settings.scan_interval_hours, settings.card_db_refresh_hours)

    def _bootstrap(self):
        self.scanner.refresh_cards(trigger="startup")
        self.scanner.scan(trigger="startup")

    def _retry_later(self, func, **kwargs):
        when = datetime.now(timezone.utc) + timedelta(minutes=30)
        log.info("Another job is running - retrying %s at %s", func.__name__, when.strftime("%H:%M"))
        self.sched.add_job(func, kwargs=kwargs, next_run_time=when)

    def _carddb_job(self, trigger: str = "schedule", force: bool = False):
        if self.scanner.refresh_cards(trigger=trigger, force=force) is None and self.scanner.current:
            self._retry_later(self._carddb_job, trigger=trigger, force=force)

    def _scan_job(self, trigger: str = "schedule", only: list[str] | None = None):
        if self.scanner.scan(only=only, trigger=trigger) is None:
            self._retry_later(self._scan_job, trigger=trigger, only=only)

    def _schedule(self, settings: UserSettings, bootstrapping: bool = False) -> None:
        def first(kind: str, hours: float) -> datetime:
            if bootstrapping:
                return datetime.now(timezone.utc) + timedelta(hours=hours)
            return self._next_time(kind, hours)

        self.sched.add_job(self._carddb_job, IntervalTrigger(hours=settings.card_db_refresh_hours),
                           id="carddb", replace_existing=True,
                           next_run_time=first("carddb", settings.card_db_refresh_hours))
        self.sched.add_job(self._scan_job, IntervalTrigger(hours=settings.scan_interval_hours),
                           id="scan", replace_existing=True,
                           next_run_time=first("scan", settings.scan_interval_hours))

    def reschedule(self, settings: UserSettings) -> None:
        self._schedule(settings)

    def run_now(self, kind: str, only: list[str] | None = None) -> None:
        now = datetime.now(timezone.utc)
        if kind == "carddb":
            self.sched.add_job(self._carddb_job, kwargs={"trigger": "manual", "force": True}, next_run_time=now)
        else:
            self.sched.add_job(self._scan_job, kwargs={"trigger": "manual", "only": only}, next_run_time=now)

    def next_run(self, job_id: str) -> datetime | None:
        job = self.sched.get_job(job_id)
        return job.next_run_time if job else None

    def shutdown(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)
