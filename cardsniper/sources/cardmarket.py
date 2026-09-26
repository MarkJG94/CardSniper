"""Cardmarket via its official daily price guide - no page scraping, so no Cloudflare.

The price guide only has EU-wide figures (no seller countries or individual
listings). CardSniper raises a Cardmarket alert when the *lowest listing from
any EU seller* is far enough below trend, and links to the card's page filtered
to UK sellers / English / your minimum condition so you can check for a UK copy.
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlencode

from sqlalchemy import func, or_

from ..conditions import CARDMARKET_CONDITION_ID, rank
from ..deals import RawListing
from ..matching import norm
from ..models import Card
from ..priceguide import refresh_price_guide
from .base import ScanContext, Source

# only bother evaluating cards whose lowest listing is at least this far under trend
PREFILTER = 0.95


def _eur(v: float | None) -> str:
    return f"€{v:.2f}" if v is not None else "?"


class CardmarketSource(Source):
    key = "cardmarket"
    label = "Cardmarket"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cm = cfg.cardmarket

    def link(self, ctx: ScanContext, card: Card, finish: str) -> str:
        """Product page filtered to UK sellers, English and your worst acceptable condition."""
        rules = [r for r in ctx.rules if r.enabled and (r.card_id == card.id or r.oracle_name == card.name_norm)]
        worst = max([rank(ctx.settings.min_condition)] + [rank(r.min_condition) for r in rules if r.min_condition])
        params = {"idProduct": card.cardmarket_id, "sellerCountry": self.cm.seller_country,
                  "language": self.cm.language, "minCondition": list(CARDMARKET_CONDITION_ID.values())[worst],
                  "isFoil": "N" if finish == "nonfoil" else "Y"}
        return f"{self.cm.base_url}/Products?{urlencode(params)}"

    def signals(self, ctx: ScanContext, card: Card, prefilter: bool = True) -> list[RawListing]:
        out = []
        for finish in ("nonfoil", "foil"):
            if finish == "foil" and not ({"foil", "etched"} & set(card.finish_list)):
                continue
            if finish == "nonfoil" and "nonfoil" not in card.finish_list:
                continue
            low, trend = card.cm_stat("low", finish), card.cm_stat("trend", finish)
            if low is None or trend is None or (prefilter and low > trend * PREFILTER):
                continue
            avg1, avg7 = card.cm_stat("avg1", finish), card.cm_stat("avg7", finish)
            out.append(RawListing(
                source=self.key, source_label=self.label, listing_key=f"{card.cardmarket_id}:{finish}",
                url=self.link(ctx, card, finish),
                title=(f"{card.name} [{card.set_name}] - lowest EU listing {_eur(low)}, trend {_eur(trend)}, "
                       f"avg sale 1d {_eur(avg1)} / 7d {_eur(avg7)}"),
                price=low, currency="EUR", shipping_note="varies by seller", finish=finish, card_id=card.id,
                seller="Any EU seller", seller_location="EU-wide (check the link for UK sellers)",
                image_url=card.image_url, indicative=True,
            ))
        return out

    def scan(self, ctx: ScanContext) -> Iterator[RawListing]:
        try:
            result = refresh_price_guide(ctx.session, self.cfg)
            ctx.session.commit()
            if result:
                ctx.log.info("Cardmarket price guide refreshed (%s products)", result["products"])
        except Exception as exc:
            ctx.error(f"{exc} - using the last downloaded price guide")
        q = (ctx.session.query(Card)
             .filter(Card.cardmarket_id.isnot(None))
             # most valuable first, so the per-run alert limit keeps the best leads
             .order_by(func.max(func.coalesce(Card.cm_trend, 0), func.coalesce(Card.cm_trend_foil, 0)).desc())
             .filter(or_(Card.cm_low <= Card.cm_trend * PREFILTER, Card.cm_low_foil <= Card.cm_trend_foil * PREFILTER)))
        if ctx.session.query(Card.id).filter(Card.cm_updated.isnot(None)).first() is None:
            ctx.error("No Cardmarket price guide data yet - it downloads with the card database refresh")
            return
        # load candidates up front: deals are written (and committed) while we iterate
        for card in q.all():
            ctx.counted()
            yield from self.signals(ctx, card)

    def probe(self, ctx: ScanContext, card_name: str, fetcher=None, save_html: str | None = None
              ) -> Iterator[RawListing]:
        printings = ctx.index.by_name.get(norm(card_name))
        if not printings:
            raise LookupError(f"No card called {card_name!r} in the database")
        for ref in printings:
            card = ctx.session.get(Card, ref.id)
            if card.cardmarket_id and card.cm_updated:
                yield from self.signals(ctx, card, prefilter=False)
