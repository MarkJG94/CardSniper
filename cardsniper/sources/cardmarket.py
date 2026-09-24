"""Cardmarket product pages, filtered to UK sellers / English / minimum condition.

Cardmarket has no public API for new users, so this reads the same product
page you would see in a browser. The card's Cardmarket id comes from Scryfall,
so there is no guessing of URLs.
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..conditions import CARDMARKET_CONDITION_ID, normalize_cardmarket, rank
from ..currency import parse_price
from ..deals import RawListing
from ..fetch import Fetcher
from ..matching import norm
from ..models import Card
from .base import ScanContext, Source

LANGUAGES = {"English", "French", "German", "Spanish", "Italian", "S-Chinese", "Japanese",
             "Portuguese", "Russian", "Korean", "T-Chinese"}
LABEL_ATTRS = ("aria-label", "data-bs-original-title", "data-original-title", "title", "alt")
SHIPPING_NOTE = "seller's rate, added at checkout"


def _labels(el) -> list[str]:
    out = []
    for node in [el, *el.find_all(True)]:
        for attr in LABEL_ATTRS:
            v = node.get(attr)
            if v:
                out.append(v.strip())
    return out


def parse_offers(html: str) -> list[dict]:
    """Parse the offers table of a Cardmarket product page."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("div.article-row") or soup.select("[id^=articleRow]")
    offers = []
    for row in rows:
        article_id = re.sub(r"\D", "", row.get("id", "")) or None
        seller_el = row.select_one(".seller-name a[href*='/Users/']") or row.select_one("a[href*='/Users/']")
        seller = seller_el.get_text(strip=True) if seller_el else None
        labels = _labels(row)
        location = next((m.group(1).strip() for lab in labels
                         if (m := re.match(r"Item location:\s*(.+)", lab))), None)

        attrs_el = row.select_one(".product-attributes") or row
        attr_labels = _labels(attrs_el)
        language = next((lab for lab in attr_labels if lab in LANGUAGES), None)
        foil = any(lab.lower() in ("foil", "etched foil") for lab in attr_labels)
        special = {lab.lower() for lab in attr_labels} & {"altered", "playset", "signed"}

        cond_el = row.select_one(".article-condition")
        condition = normalize_cardmarket(cond_el.get_text(" ", strip=True) if cond_el else None)

        price = None
        for el in row.select(".price-container, .col-offer .price, [class*=price]"):
            price = parse_price(el.get_text(" ", strip=True), default_currency="EUR")
            if price:
                break
        if price is None:
            price = parse_price(row.get_text(" ", strip=True), default_currency="EUR")
        if price is None:
            continue

        qty_el = row.select_one(".item-count, .amount-container span")
        qty = int(re.sub(r"\D", "", qty_el.get_text()) or 0) if qty_el else None

        comment_el = row.select_one(".product-comments")
        offers.append({
            "article_id": article_id, "seller": seller, "location": location, "language": language,
            "foil": foil, "special": sorted(special), "condition": condition,
            "price": price[0], "currency": price[1], "quantity": qty or None,
            "comment": comment_el.get_text(" ", strip=True) if comment_el else None,
        })
    return offers


def looks_like_product_page(html: str) -> bool:
    return "article-row" in html or "articleRow" in html or "No offers" in html or "table-body" in html


class CardmarketSource(Source):
    key = "cardmarket"
    label = "Cardmarket"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cm = cfg.cardmarket

    def make_fetcher(self, **kw) -> Fetcher:
        return Fetcher(self.cfg.http, self.cfg.data_dir, min_delay=self.cm.min_delay_seconds,
                       max_delay=self.cm.max_delay_seconds, mode=self.cm.fetch_mode)

    # -- urls ----------------------------------------------------------------
    def product_url(self, fetcher: Fetcher, card: Card) -> str:
        if card.cardmarket_url:
            return card.cardmarket_url
        page = fetcher.get(f"{self.cm.base_url}/Products", params={"idProduct": card.cardmarket_id})
        parts = urlsplit(page.url)
        if "/Products/" not in parts.path or "/Search" in parts.path:
            raise LookupError(f"Cardmarket did not resolve product {card.cardmarket_id} ({page.url})")
        card.cardmarket_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return card.cardmarket_url

    def filters(self, ctx: ScanContext, card: Card) -> dict:
        rules = [r for r in ctx.rules if r.enabled and (r.card_id == card.id or r.oracle_name == card.name_norm)]
        worst = max([rank(ctx.settings.min_condition)] +
                    [rank(r.min_condition) for r in rules if r.min_condition])
        params = {"sellerCountry": self.cm.seller_country, "language": self.cm.language,
                  "minCondition": list(CARDMARKET_CONDITION_ID.values())[worst]}
        wants = {"foil": ctx.settings.include_foils, "nonfoil": ctx.settings.include_nonfoils}
        for r in rules:
            if r.finish == "any":
                wants = {"foil": True, "nonfoil": True}
            else:
                wants[r.finish] = True
        if wants["foil"] and not wants["nonfoil"]:
            params["isFoil"] = "Y"
        elif wants["nonfoil"] and not wants["foil"]:
            params["isFoil"] = "N"
        return params

    # -- scanning ----------------------------------------------------------------
    def listings_for(self, ctx: ScanContext, fetcher: Fetcher, card: Card, save_html: str | None = None
                     ) -> list[RawListing]:
        url = self.product_url(fetcher, card)
        params = self.filters(ctx, card)
        page = fetcher.get(url, params=params)
        if save_html:
            with open(save_html, "w", encoding="utf-8") as fh:
                fh.write(page.text)
        if not looks_like_product_page(page.text):
            raise LookupError(f"Unexpected page layout at {page.url} - Cardmarket may have changed its HTML")
        link = f"{url}?{urlencode(params)}"
        out = []
        for o in parse_offers(page.text):
            if o["special"]:
                continue
            if o["location"] and "united kingdom" not in o["location"].lower():
                continue
            key = o["article_id"] or f"{card.id}:{o['seller']}:{o['price']}:{o['condition']}:{o['foil']}"
            out.append(RawListing(
                source=self.key, source_label=self.label, listing_key=key, url=link,
                title=f"{card.name} [{card.set_name}]", price=o["price"], currency=o["currency"],
                shipping_note=SHIPPING_NOTE, condition=o["condition"],
                language=("en" if (o["language"] or "English") == "English" else o["language"]),
                finish="foil" if o["foil"] else "nonfoil", card_id=card.id, seller=o["seller"],
                seller_location=o["location"] or "United Kingdom", quantity=o["quantity"],
                image_url=card.image_url, extra={"comment": o["comment"]},
            ))
        return sorted(out, key=lambda l: l.price)

    def scan(self, ctx: ScanContext) -> Iterator[RawListing]:
        cards = ctx.card_targets(self.key, self.cm.max_products_per_run)
        ctx.log.info("Cardmarket: checking %d products", len(cards))
        with self.make_fetcher() as fetcher:
            for card in cards:
                ref = ctx.index.by_id.get(card.id)
                if ref is None or ctx.evaluator.max_deal_price([ref]) is None:
                    ctx.checked(self.key, card.id)
                    continue
                try:
                    listings = self.listings_for(ctx, fetcher, card)
                except Exception as exc:
                    if ctx.failed(f"Cardmarket {card.name} ({card.set_code}): {exc}"):
                        return
                    continue
                ctx.checked(self.key, card.id)
                yield from listings

    def probe(self, ctx: ScanContext, card_name: str, fetcher: Fetcher | None = None,
              save_html: str | None = None) -> Iterator[RawListing]:
        printings = ctx.index.by_name.get(norm(card_name))
        if not printings:
            raise LookupError(f"No card called {card_name!r} in the database")
        ids = [c.id for c in sorted(printings, key=lambda c: -(c.eur or c.eur_foil or 0))[:3]]
        cards = [ctx.session.get(Card, i) for i in ids]
        with (fetcher or self.make_fetcher()) as f:
            for card in cards:
                if card.cardmarket_id:
                    yield from self.listings_for(ctx, f, card, save_html=save_html)
