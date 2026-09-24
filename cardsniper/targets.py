"""Pick which cards / names each source checks this run.

Watch-list cards always come first. In "all cards" mode the remaining budget
goes to cards worth at least the minimum, least-recently-checked first, so a
per-run cap still covers the whole list over successive runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, aliased

from .currency import Fx
from .models import Card, CheckState, WatchRule, utcnow
from .settings import UserSettings


@dataclass
class NameTarget:
    name_norm: str
    name: str  # display / search name (front face for double-faced cards)


def _price_floor(settings: UserSettings, fx: Fx):
    floor_eur = settings.min_reference_gbp / fx.eur_to_gbp
    conds = []
    if settings.include_nonfoils:
        conds.append(Card.eur >= floor_eur)
    if settings.include_foils:
        conds.append(Card.eur_foil >= floor_eur)
    return or_(*conds) if conds else None


def _watched(rules: list[WatchRule]) -> tuple[set[str], set[str]]:
    ids = {r.card_id for r in rules if r.enabled and r.card_id}
    names = {r.oracle_name for r in rules if r.enabled and r.oracle_name}
    return ids, names


def card_targets(session: Session, source: str, settings: UserSettings, fx: Fx,
                 rules: list[WatchRule], limit: int) -> list[Card]:
    ids, names = _watched(rules)
    cs = aliased(CheckState)
    order = (cs.last_checked.is_(None).desc(), cs.last_checked.asc(),
             func.max(func.coalesce(Card.eur, 0), func.coalesce(Card.eur_foil, 0)).desc())
    base = (select(Card).outerjoin(cs, and_(cs.source == source, cs.target == Card.id))
            .where(Card.cardmarket_id.isnot(None)))

    out: list[Card] = []
    if ids or names:
        out += session.scalars(base.where(or_(Card.id.in_(ids), Card.name_norm.in_(names)))
                               .order_by(*order).limit(limit)).all()
    if settings.scan_mode == "all" and len(out) < limit:
        floor = _price_floor(settings, fx)
        if floor is not None:
            seen = {c.id for c in out}
            more = session.scalars(base.where(floor).order_by(*order).limit(limit - len(out) + len(seen))).all()
            out += [c for c in more if c.id not in seen][: limit - len(out)]
    return out


def name_targets(session: Session, source: str, settings: UserSettings, fx: Fx,
                 rules: list[WatchRule], limit: int) -> list[NameTarget]:
    ids, names = _watched(rules)
    if ids:
        names |= set(session.scalars(select(Card.name_norm).where(Card.id.in_(ids))))
    cs = aliased(CheckState)
    base = (select(Card.name_norm, func.min(Card.name).label("name"))
            .outerjoin(cs, and_(cs.source == source, cs.target == Card.name_norm))
            .group_by(Card.name_norm)
            .order_by(func.max(cs.last_checked).is_(None).desc(), func.max(cs.last_checked).asc(),
                      func.max(func.max(func.coalesce(Card.eur, 0), func.coalesce(Card.eur_foil, 0))).desc()))

    out: list[NameTarget] = []
    seen: set[str] = set()

    def add(rows):
        for name_norm, name in rows:
            if len(out) >= limit:
                return
            if name_norm not in seen:
                seen.add(name_norm)
                # search by the front face for "A // B" double-faced cards
                out.append(NameTarget(name_norm, name.split(" // ")[0] if " // " in name else name))

    if names:
        add(session.execute(base.where(Card.name_norm.in_(names)).limit(limit)))
    if settings.scan_mode == "all" and len(out) < limit:
        floor = _price_floor(settings, fx)
        if floor is not None:
            add(session.execute(base.where(floor).limit(limit + len(out))))
    return out


def mark_checked(session: Session, source: str, target: str) -> None:
    stmt = insert(CheckState).values(source=source, target=target, last_checked=utcnow())
    session.execute(stmt.on_conflict_do_update(index_elements=["source", "target"],
                                               set_={"last_checked": stmt.excluded.last_checked}))
