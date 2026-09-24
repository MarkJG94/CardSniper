"""Database tables (SQLite via SQLAlchemy)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no time zones)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Card(Base):
    """One paper printing of a card, imported from Scryfall."""

    __tablename__ = "cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # Scryfall id
    oracle_id: Mapped[str | None] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(300))
    name_norm: Mapped[str] = mapped_column(String(300), index=True)
    front_norm: Mapped[str | None] = mapped_column(String(300), index=True)
    set_code: Mapped[str] = mapped_column(String(10), index=True)
    set_name: Mapped[str] = mapped_column(String(200))
    collector_number: Mapped[str] = mapped_column(String(20))
    rarity: Mapped[str | None] = mapped_column(String(20))
    released_at: Mapped[str | None] = mapped_column(String(10))
    finishes: Mapped[str] = mapped_column(String(50), default="nonfoil")  # csv
    treatments: Mapped[str] = mapped_column(String(300), default="")  # csv, see matching.treatments_for
    cardmarket_id: Mapped[int | None] = mapped_column(Integer)
    eur: Mapped[float | None] = mapped_column(Float)
    eur_foil: Mapped[float | None] = mapped_column(Float)
    image_url: Mapped[str | None] = mapped_column(String(500))
    scryfall_uri: Mapped[str | None] = mapped_column(String(500))
    cardmarket_url: Mapped[str | None] = mapped_column(String(500))  # resolved product page
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def finish_list(self) -> list[str]:
        return [f for f in self.finishes.split(",") if f]

    def eur_for(self, finish: str) -> float | None:
        if finish == "nonfoil":
            return self.eur
        return self.eur_foil if self.eur_foil is not None else None


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (UniqueConstraint("card_id", "finish", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    finish: Mapped[str] = mapped_column(String(10))
    day: Mapped[date] = mapped_column(Date)
    ref_gbp: Mapped[float] = mapped_column(Float)


class WatchRule(Base):
    """Overrides for specific cards: a printing (card_id) or every printing (oracle_name)."""

    __tablename__ = "watch_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"))
    oracle_name: Mapped[str | None] = mapped_column(String(300))  # normalised name
    display_name: Mapped[str] = mapped_column(String(300))
    finish: Mapped[str] = mapped_column(String(10), default="any")  # any | nonfoil | foil
    min_condition: Mapped[str | None] = mapped_column(String(2))
    discount_percent: Mapped[float | None] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    card: Mapped[Card | None] = relationship()


class Deal(Base):
    """A listing that met the alert criteria (whether or not an alert went out)."""

    __tablename__ = "deals"
    __table_args__ = (UniqueConstraint("source", "listing_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    source_label: Mapped[str] = mapped_column(String(100))
    listing_key: Mapped[str] = mapped_column(String(200))
    card_key: Mapped[str] = mapped_column(String(400), index=True)
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id", ondelete="SET NULL"))
    card_name: Mapped[str] = mapped_column(String(300))
    printing: Mapped[str | None] = mapped_column(String(300))  # "Set Name #123" or "any printing"
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    finish: Mapped[str] = mapped_column(String(10))
    condition: Mapped[str | None] = mapped_column(String(2))
    price_gbp: Mapped[float] = mapped_column(Float)
    shipping_gbp: Mapped[float | None] = mapped_column(Float)
    shipping_note: Mapped[str | None] = mapped_column(String(300))
    reference_gbp: Mapped[float] = mapped_column(Float)
    discount_pct: Mapped[float] = mapped_column(Float)
    seller: Mapped[str | None] = mapped_column(String(200))
    seller_location: Mapped[str | None] = mapped_column(String(100))
    listing_type: Mapped[str] = mapped_column(String(10), default="fixed")  # fixed | auction
    auction_end: Mapped[datetime | None] = mapped_column(DateTime)
    quantity: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(String(500))
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    suppressed_reason: Mapped[str | None] = mapped_column(String(300))
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    card: Mapped[Card | None] = relationship()


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_card_key_sent", "card_key", "sent_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    card_key: Mapped[str] = mapped_column(String(400))
    source: Mapped[str] = mapped_column(String(50))
    listing_key: Mapped[str] = mapped_column(String(200))
    price_gbp: Mapped[float] = mapped_column(Float)
    channels: Mapped[str] = mapped_column(String(100))  # csv of channels that succeeded
    errors: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    deal: Mapped[Deal] = relationship()


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # scan | carddb
    source: Mapped[str] = mapped_column(String(50))
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|ok|partial|error
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    items_checked: Mapped[int] = mapped_column(Integer, default=0)
    listings_seen: Mapped[int] = mapped_column(Integer, default=0)
    deals_found: Mapped[int] = mapped_column(Integer, default=0)
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)


class CheckState(Base):
    """When a source last looked at a card/name - lets big scans rotate over several runs."""

    __tablename__ = "check_state"

    source: Mapped[str] = mapped_column(String(50), primary_key=True)
    target: Mapped[str] = mapped_column(String(400), primary_key=True)
    last_checked: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
