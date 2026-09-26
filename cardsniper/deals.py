"""Decide whether a listing is a deal, store it and decide whether to alert."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func

from .conditions import meets
from .currency import Fx
from .matching import CardIndex, CardRef, Match
from .models import Alert, Deal, WatchRule, utcnow
from .settings import UserSettings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .notify import Notifier

log = logging.getLogger(__name__)


@dataclass
class RawListing:
    """A listing as a source found it, before matching and evaluation."""

    source: str  # e.g. "cardmarket", "ebay", "store:totalcards"
    source_label: str  # e.g. "Cardmarket", "eBay", "Total Cards"
    listing_key: str  # unique within the source
    url: str
    title: str
    price: float
    currency: str = "GBP"
    shipping: float | None = None
    shipping_note: str | None = None
    condition: str | None = None  # MT..PO or None if not stated
    language: str | None = None  # "en", other, or None if not stated
    finish: str | None = None  # set when the source knows it
    card_id: str | None = None  # set when the source knows the exact printing
    name_hint: str | None = None
    name_hint_exact: bool = False  # name_hint is the full card name (structured shop titles)
    set_hint: str | None = None
    seller: str | None = None
    seller_location: str | None = None
    listing_type: str = "fixed"  # fixed | auction
    auction_end: datetime | None = None
    quantity: int | None = None
    image_url: str | None = None
    graded: bool = False
    # Price is an indicator (Cardmarket's lowest EU listing), not a specific UK offer you can buy.
    indicative: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class Evaluation:
    listing: RawListing
    is_deal: bool
    reason: str
    match: Match | None = None
    reference_card: CardRef | None = None
    price_gbp: float | None = None
    shipping_gbp: float | None = None
    reference_gbp: float | None = None
    discount_pct: float | None = None
    threshold_pct: float | None = None
    rule: WatchRule | None = None

    @property
    def finish_key(self) -> str:
        return "nonfoil" if self.match and self.match.finish == "nonfoil" else "foil"

    @property
    def card_key(self) -> str:
        m = self.match
        assert m is not None
        which = m.unique.id if m.unique else "any"
        return f"{m.name_norm}|{self.finish_key}|{which}"


def finish_allowed(rule_finish: str, finish: str) -> bool:
    return rule_finish == "any" or rule_finish == finish or (rule_finish == "foil" and finish == "etched")


class Evaluator:
    def __init__(self, settings: UserSettings, index: CardIndex, fx: Fx, rules: list[WatchRule]):
        self.settings = settings
        self.index = index
        self.fx = fx
        self.rules = [r for r in rules if r.enabled]

    # -- rules ------------------------------------------------------------
    def rules_for(self, match: Match) -> list[WatchRule]:
        ids = {c.id for c in match.candidates}
        out = []
        for r in self.rules:
            if not finish_allowed(r.finish, match.finish):
                continue
            if r.card_id and r.card_id in ids and (match.unique or match.set_matched):
                out.append(r)
            elif r.oracle_name and r.oracle_name == match.name_norm:
                out.append(r)
        # printing-specific rules beat whole-name rules
        return sorted(out, key=lambda r: 0 if r.card_id else 1)

    def threshold_for(self, rule: WatchRule | None, indicative: bool = False) -> float:
        if rule and rule.discount_percent is not None:
            return rule.discount_percent
        return self.settings.cardmarket_discount_percent if indicative else self.settings.discount_percent

    def finish_wanted(self, finish: str) -> bool:
        if finish == "nonfoil":
            return self.settings.include_nonfoils
        return self.settings.include_foils

    def max_deal_price(self, cards: list[CardRef]) -> float | None:
        """Highest price (GBP) that could still be a deal for any of these printings.

        Used by sources to skip work (eBay price filter, Cardmarket short-circuit).
        """
        best = None
        for card in cards:
            for finish in card.finishes:
                match = Match(card.name_norm, card.name, finish, [card], set_matched=True)
                rules = self.rules_for(match)
                if not rules and (self.settings.scan_mode == "watchlist" or not self.finish_wanted(finish)):
                    continue
                ref = self.fx.eur_to_gbp_amount(card.eur_for(finish))
                if not ref or (not rules and ref < self.settings.min_reference_gbp):
                    continue
                price = ref * (1 - self.threshold_for(rules[0] if rules else None) / 100)
                best = price if best is None else max(best, price)
        return round(best, 2) if best is not None else None

    # -- evaluation -----------------------------------------------------
    def evaluate(self, listing: RawListing, now: datetime | None = None) -> Evaluation:
        now = now or utcnow()
        s = self.settings

        def reject(reason: str, **kw) -> Evaluation:
            return Evaluation(listing, False, reason, **kw)

        if listing.graded:
            return reject("graded card")
        if listing.language and listing.language != "en":
            return reject(f"language {listing.language}")

        if listing.card_id:
            card = self.index.by_id.get(listing.card_id)
            if card is None:
                return reject("card not in database")
            match = Match(card.name_norm, card.name, listing.finish or "nonfoil", [card], set_matched=True)
        else:
            match, why = self.index.match(listing.title, listing.name_hint, listing.set_hint, listing.finish,
                                          hint_exact=listing.name_hint_exact)
            if match is None:
                return reject(why or "no match")

        rules = self.rules_for(match)
        rule = rules[0] if rules else None
        if rule is None:
            if s.scan_mode == "watchlist":
                return reject("not on watchlist", match=match)
            if not self.finish_wanted(match.finish):
                return reject(f"{match.finish} cards excluded in settings", match=match)

        ref = match.reference_eur()
        if ref is None:
            return reject("no market price known", match=match)
        ref_eur, ref_card = ref
        ref_gbp = self.fx.to_gbp(ref_eur, "EUR")
        if rule is None and ref_gbp < s.min_reference_gbp:
            return reject(f"market price £{ref_gbp:.2f} below minimum £{s.min_reference_gbp:.2f}",
                          match=match, reference_gbp=ref_gbp)

        price_gbp = self.fx.to_gbp(listing.price, listing.currency)
        shipping_gbp = self.fx.to_gbp(listing.shipping, listing.currency) if listing.shipping is not None else None
        discount = round((ref_gbp - price_gbp) / ref_gbp * 100, 1)
        threshold = self.threshold_for(rule, listing.indicative)
        common = dict(match=match, reference_card=ref_card, price_gbp=price_gbp, shipping_gbp=shipping_gbp,
                      reference_gbp=ref_gbp, discount_pct=discount, threshold_pct=threshold, rule=rule)

        min_condition = (rule.min_condition if rule and rule.min_condition else s.min_condition)
        if listing.indicative:
            pass  # condition is unknown by nature; the alert link filters to your minimum condition
        elif listing.condition is None:
            if not s.allow_unknown_condition:
                return reject("condition not stated", **common)
        elif not meets(listing.condition, min_condition):
            return reject(f"condition {listing.condition} worse than {min_condition}", **common)

        if listing.listing_type == "auction":
            if listing.auction_end and listing.auction_end > now + timedelta(hours=s.auction_window_hours):
                return reject("auction ends outside the alert window", **common)

        if discount < threshold:
            return reject(f"only {discount:.0f}% below market (need {threshold:.0f}%)", **common)
        return Evaluation(listing, True, f"{discount:.0f}% below market", **common)


class DealRecorder:
    """Persists deals and sends alerts, applying the 'only if better' rule."""

    def __init__(self, session: "Session", settings: UserSettings, notifier: "Notifier | None"):
        self.session = session
        self.settings = settings
        self.notifier = notifier
        self.sent = 0

    def record(self, ev: Evaluation, now: datetime | None = None) -> tuple[Deal, bool]:
        now = now or utcnow()
        l, m = ev.listing, ev.match
        assert ev.is_deal and m is not None
        deal = self.session.query(Deal).filter_by(source=l.source, listing_key=l.listing_key).one_or_none()
        if deal is None:
            deal = Deal(source=l.source, listing_key=l.listing_key, first_seen=now)
            self.session.add(deal)
        unique = m.unique
        deal.source_label = l.source_label
        deal.card_key = ev.card_key
        deal.card_id = unique.id if unique else (ev.reference_card.id if ev.reference_card else None)
        deal.card_name = m.display_name
        deal.printing = unique.printing if unique else f"unconfirmed printing (priced as {ev.reference_card.printing})"
        deal.url = l.url
        deal.title = l.title[:500]
        deal.finish = m.finish
        deal.condition = l.condition
        deal.price_gbp = ev.price_gbp
        deal.shipping_gbp = ev.shipping_gbp
        deal.shipping_note = l.shipping_note
        deal.reference_gbp = ev.reference_gbp
        deal.discount_pct = ev.discount_pct
        deal.seller = l.seller
        deal.seller_location = l.seller_location
        deal.listing_type = l.listing_type
        deal.auction_end = l.auction_end
        deal.quantity = l.quantity
        deal.image_url = l.image_url
        deal.indicative = l.indicative
        deal.last_seen = now
        self.session.flush()

        reason = self.suppression_reason(deal, now)
        if reason:
            deal.suppressed_reason = reason
            return deal, False
        if self.notifier is None or not self.notifier.channels:
            deal.suppressed_reason = "no notification channel configured"
            return deal, False
        if self.sent >= self.settings.max_alerts_per_run:
            deal.suppressed_reason = (f"alert limit of {self.settings.max_alerts_per_run} per run reached "
                                      f"(change it in Settings)")
            return deal, False
        results = self.notifier.send_deal(deal)
        ok = [ch for ch, err in results.items() if err is None]
        errors = "; ".join(f"{ch}: {err}" for ch, err in results.items() if err)
        if not ok:
            deal.suppressed_reason = f"sending failed ({errors})"
            log.error("Alert for %s failed: %s", deal.card_name, errors)
            return deal, False
        self.session.add(Alert(deal_id=deal.id, card_key=deal.card_key, source=deal.source,
                               listing_key=deal.listing_key, price_gbp=deal.price_gbp,
                               channels=",".join(ok), errors=errors or None, sent_at=now))
        deal.alerted = True
        deal.suppressed_reason = None
        self.sent += 1
        return deal, True

    def suppression_reason(self, deal: Deal, now: datetime) -> str | None:
        same = (self.session.query(func.min(Alert.price_gbp))
                .filter(Alert.source == deal.source, Alert.listing_key == deal.listing_key).scalar())
        if same is not None and deal.price_gbp >= same - 0.005:
            return f"already alerted for this listing at £{same:.2f}"
        since = now - timedelta(days=self.settings.realert_days)
        best = (self.session.query(func.min(Alert.price_gbp))
                .filter(Alert.card_key == deal.card_key, Alert.sent_at >= since).scalar())
        if best is not None and deal.price_gbp >= best - 0.005:
            return f"not cheaper than the £{best:.2f} deal alerted in the last {self.settings.realert_days:g} days"
        return None
