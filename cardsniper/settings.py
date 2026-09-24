"""Settings you can change from the dashboard (stored in the database)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

from sqlalchemy.orm import Session

from .conditions import CONDITIONS
from .models import Setting

SETTINGS_KEY = "user_settings"


@dataclass
class UserSettings:
    # "all": every card in the database worth >= min_reference_gbp. "watchlist": only watch rules.
    scan_mode: str = "all"
    scan_interval_hours: float = 24.0
    card_db_refresh_hours: float = 24.0
    # Alert when a listing is at least this % below the reference (market) price.
    discount_percent: float = 20.0
    # Ignore cards whose reference price is below this (avoids alerts on 20p bulk).
    min_reference_gbp: float = 5.0
    # Worst acceptable condition on the Cardmarket scale: MT NM EX GD LP PL PO
    min_condition: str = "EX"
    allow_unknown_condition: bool = True
    include_foils: bool = True
    include_nonfoils: bool = True
    # Auctions only alert if they end within this many hours (bids climb near the end).
    auction_window_hours: float = 24.0
    # After alerting on a card, only alert again inside this window if the new deal is cheaper.
    realert_days: float = 7.0
    enabled_sources: list[str] | None = None  # None = all configured sources
    notify_email: bool = True
    notify_telegram: bool = True

    def validate(self) -> "UserSettings":
        if self.scan_mode not in ("all", "watchlist"):
            raise ValueError("scan_mode must be 'all' or 'watchlist'")
        if self.min_condition not in CONDITIONS:
            raise ValueError(f"min_condition must be one of {CONDITIONS}")
        if not 0 < self.discount_percent < 100:
            raise ValueError("discount_percent must be between 0 and 100")
        if self.scan_interval_hours < 0.25:
            raise ValueError("scan_interval_hours must be at least 0.25")
        if self.card_db_refresh_hours < 1:
            raise ValueError("card_db_refresh_hours must be at least 1")
        if self.min_reference_gbp < 0 or self.realert_days < 0 or self.auction_window_hours < 0:
            raise ValueError("values cannot be negative")
        return self

    def source_enabled(self, key: str) -> bool:
        return self.enabled_sources is None or key in self.enabled_sources


def _coerce(values: dict[str, Any]) -> dict[str, Any]:
    known = {f.name: f for f in fields(UserSettings)}
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k not in known:
            continue
        default = getattr(UserSettings, k, None)
        if isinstance(default, bool):
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(default, float):
            v = float(v)
        out[k] = v
    return out


def load_settings(session: Session, defaults: dict[str, Any] | None = None) -> UserSettings:
    base = _coerce(defaults or {})
    row = session.get(Setting, SETTINGS_KEY)
    if row:
        base.update(_coerce(json.loads(row.value)))
    return UserSettings(**base).validate()


def save_settings(session: Session, settings: UserSettings) -> None:
    settings.validate()
    payload = json.dumps(asdict(settings))
    row = session.get(Setting, SETTINGS_KEY)
    if row:
        row.value = payload
    else:
        session.add(Setting(key=SETTINGS_KEY, value=payload))


def get_value(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    return json.loads(row.value) if row else default


def set_value(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = json.dumps(value)
    else:
        session.add(Setting(key=key, value=json.dumps(value)))
