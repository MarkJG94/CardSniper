"""eBay UK via the official Browse API (free developer account required).

One search per card name, with eBay filtering server-side to UK-located,
ungraded items priced at or below the highest price that could be a deal.
Promising results are then fetched individually for card condition,
language, set and finish.
"""

from __future__ import annotations

import base64
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterator

import httpx

from ..conditions import normalize
from ..deals import RawListing
from ..fetch import Fetcher
from ..matching import norm
from .base import ScanContext, Source

# eBay "Card Condition" descriptor value ids for ungraded trading cards
DESCRIPTOR_CONDITIONS = {"400010": "NM", "400011": "EX", "400012": "GD", "400013": "PL"}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _money(obj: dict | None) -> tuple[float, str] | None:
    if not obj or obj.get("value") is None:
        return None
    return float(obj["value"]), obj.get("currency", "GBP")


def listing_from_summary(item: dict) -> RawListing | None:
    if item.get("itemGroupType") or item.get("itemGroupHref"):
        return None  # multi-variation "pick your card" listings
    options = item.get("buyingOptions") or []
    if "FIXED_PRICE" in options:
        listing_type, money = "fixed", _money(item.get("price"))
    elif "AUCTION" in options:
        listing_type, money = "auction", _money(item.get("currentBidPrice")) or _money(item.get("price"))
    else:
        return None
    if money is None:
        return None
    shipping = None
    for opt in item.get("shippingOptions") or []:
        cost = _money(opt.get("shippingCost"))
        if cost and (shipping is None or cost[0] < shipping):
            shipping = cost[0]
    condition_text = item.get("condition") or ""
    graded = item.get("conditionId") == "2750" or ("graded" in condition_text.lower()
                                                   and "ungraded" not in condition_text.lower())
    return RawListing(
        source="ebay", source_label="eBay", listing_key=str(item["itemId"]),
        url=item.get("itemWebUrl") or f"https://www.ebay.co.uk/itm/{item['itemId']}",
        title=item.get("title", ""), price=money[0], currency=money[1],
        shipping=shipping, shipping_note=None if shipping is not None else "see listing",
        seller=(item.get("seller") or {}).get("username"),
        seller_location=(item.get("itemLocation") or {}).get("country"),
        listing_type=listing_type, auction_end=_parse_dt(item.get("itemEndDate")),
        image_url=(item.get("image") or {}).get("imageUrl"), graded=graded,
    )


def enrich_from_item(listing: RawListing, item: dict) -> RawListing:
    """Add condition / language / set / finish from the full item record."""
    for desc in item.get("conditionDescriptors") or []:
        for value in desc.get("values") or []:
            code = DESCRIPTOR_CONDITIONS.get(str(value.get("content"))) or normalize(value.get("content"))
            if code:
                listing.condition = code
    aspects = {a.get("name", "").lower(): a.get("value", "") for a in item.get("localizedAspects") or []}
    lang = aspects.get("language")
    if lang:
        listing.language = "en" if "english" in lang.lower() else lang.lower()
    if aspects.get("graded", "").lower() in ("yes", "true"):
        listing.graded = True
    listing.set_hint = aspects.get("set") or listing.set_hint
    listing.name_hint = aspects.get("card name") or listing.name_hint
    finish = (aspects.get("finish") or aspects.get("features") or "").lower()
    if "etched" in finish:
        listing.finish = "etched"
    elif "non" in finish or "regular" in finish or "normal" in finish:
        listing.finish = "nonfoil"
    elif "foil" in finish:
        listing.finish = "foil"
    for opt in item.get("shippingOptions") or []:
        cost = _money(opt.get("shippingCost"))
        if cost and (listing.shipping is None or cost[0] < listing.shipping):
            listing.shipping, listing.shipping_note = cost[0], None
    return listing


class EbaySource(Source):
    key = "ebay"
    label = "eBay"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.eb = cfg.ebay
        host = "api.sandbox.ebay.com" if self.eb.sandbox else "api.ebay.com"
        self.api = f"https://{host}"
        self._token: str | None = None
        self._token_expiry = 0.0
        self._client: httpx.Client | None = None
        self.calls = 0

    def available(self) -> str | None:
        if not self.cfg.secrets.ebay_configured:
            return "eBay API keys not set (CARDSNIPER_EBAY_CLIENT_ID / CARDSNIPER_EBAY_CLIENT_SECRET)"
        return None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        s = self.cfg.secrets
        basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
        r = self.client.post(f"{self.api}/identity/v1/oauth2/token",
                             headers={"Authorization": f"Basic {basic}",
                                      "Content-Type": "application/x-www-form-urlencoded"},
                             data={"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"})
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            self.calls += 1
            r = self.client.get(f"{self.api}{path}", params=params, headers={
                "Authorization": f"Bearer {self.token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.eb.marketplace,
                "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=GB",
            })
            if r.status_code == 401:
                self._token = None
                continue
            if r.status_code == 429:
                raise RuntimeError("eBay API daily quota exhausted")
            if r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"eBay API request failed: {path}")

    def search(self, query: str, max_price: float | None) -> list[dict]:
        buying = "FIXED_PRICE|AUCTION" if self.eb.include_auctions else "FIXED_PRICE"
        filters = [f"buyingOptions:{{{buying}}}", "priceCurrency:GBP"]
        if max_price is not None:
            filters.append(f"price:[..{max_price:.2f}]")
        if self.eb.item_location_country:
            filters.append(f"itemLocationCountry:{self.eb.item_location_country}")
        if self.eb.condition_ids:
            filters.append(f"conditionIds:{{{self.eb.condition_ids}}}")
        params = {"q": query, "filter": ",".join(filters), "sort": "newlyListed",
                  "limit": str(self.eb.results_per_query)}
        if self.eb.category_ids:
            params["category_ids"] = self.eb.category_ids
        return self._get("/buy/browse/v1/item_summary/search", params).get("itemSummaries") or []

    def item(self, item_id: str) -> dict:
        return self._get(f"/buy/browse/v1/item/{item_id}")

    def listings_for(self, ctx: ScanContext, name: str, name_norm: str) -> Iterator[RawListing]:
        printings = ctx.index.by_name.get(name_norm) or []
        max_price = ctx.evaluator.max_deal_price(printings)
        if max_price is None or max_price < 0.5:
            return
        for summary in self.search(name, max_price):
            listing = listing_from_summary(summary)
            if listing is None:
                continue
            listing.name_hint = name
            # quick check on the summary; condition is only known after fetching the item
            if not ctx.evaluator.evaluate(replace(listing, condition=listing.condition or "NM")).is_deal:
                continue
            try:
                listing = enrich_from_item(listing, self.item(summary["itemId"]))
            except Exception as exc:
                ctx.error(f"eBay item {summary.get('itemId')}: {exc}")
            yield listing

    def scan(self, ctx: ScanContext) -> Iterator[RawListing]:
        targets = ctx.name_targets(self.key, self.eb.max_queries_per_run)
        ctx.log.info("eBay: searching %d card names", len(targets))
        try:
            for t in targets:
                if self.calls >= self.eb.max_queries_per_run:
                    ctx.log.info("eBay: call budget for this run used up")
                    break
                try:
                    yield from self.listings_for(ctx, t.name, t.name_norm)
                except RuntimeError as exc:
                    if "quota" in str(exc):
                        ctx.error(str(exc))
                        return
                    if ctx.failed(str(exc)):
                        return
                    continue
                except httpx.HTTPError as exc:
                    if ctx.failed(f"eBay search {t.name!r}: {exc}"):
                        return
                    continue
                ctx.checked(self.key, t.name_norm)
        finally:
            if self._client:
                self._client.close()
                self._client = None

    def probe(self, ctx: ScanContext, card_name: str, fetcher: Fetcher | None = None,
              save_html: str | None = None) -> Iterator[RawListing]:
        name_norm = norm(card_name)
        if name_norm not in ctx.index.by_name:
            raise LookupError(f"No card called {card_name!r} in the database")
        # probe without the price filter so you can see what eBay returns
        for summary in self.search(card_name, None)[:25]:
            listing = listing_from_summary(summary)
            if listing:
                listing.name_hint = card_name
                yield listing
