from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

from sqlalchemy.orm import Session

from ..config import Config
from ..currency import Fx
from ..deals import Evaluator, RawListing
from ..fetch import Fetcher
from ..matching import CardIndex
from ..models import WatchRule
from ..settings import UserSettings
from .. import targets as targets_mod


@dataclass
class ScanContext:
    cfg: Config
    session: Session
    settings: UserSettings
    fx: Fx
    index: CardIndex
    evaluator: Evaluator
    rules: list[WatchRule]
    log: logging.Logger
    probe: bool = False  # probe runs don't update check state
    max_consecutive_failures: int = 5
    stats: dict = field(default_factory=lambda: {"items": 0, "errors": 0, "error_messages": []})
    _failures_in_a_row: int = 0

    def name_targets(self, source: str, limit: int):
        return targets_mod.name_targets(self.session, source, self.settings, self.fx, self.rules, limit)

    def counted(self) -> None:
        """An item was checked (without per-target rotation state)."""
        self.stats["items"] += 1
        self._failures_in_a_row = 0

    def checked(self, source: str, target: str) -> None:
        self.stats["items"] += 1
        self._failures_in_a_row = 0
        if not self.probe:
            targets_mod.mark_checked(self.session, source, target)

    def failed(self, message: str) -> bool:
        """Record a failed request; True means give up on this source for this run."""
        self.error(message)
        self._failures_in_a_row += 1
        if self._failures_in_a_row >= self.max_consecutive_failures:
            self.error(f"Stopping after {self._failures_in_a_row} failures in a row - site down or blocking us?")
            return True
        return False

    def error(self, message: str) -> None:
        self.stats["errors"] += 1
        msgs = self.stats["error_messages"]
        if len(msgs) < 20:
            msgs.append(message[:300])
        self.log.warning(message)


class Source:
    key: str = ""
    label: str = ""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def available(self) -> str | None:
        """Return a reason if the source cannot run (e.g. missing API keys)."""
        return None

    def scan(self, ctx: ScanContext) -> Iterator[RawListing]:
        raise NotImplementedError

    def probe(self, ctx: ScanContext, card_name: str, fetcher: Fetcher | None = None) -> Iterator[RawListing]:
        """Look up one card by name (debugging a source against the live site)."""
        raise NotImplementedError

    def make_fetcher(self, **kw) -> Fetcher:
        return Fetcher(self.cfg.http, self.cfg.data_dir, **kw)
