"""UK shops.

* Shopify shops (common for UK singles stores) publish their catalogue at
  /products.json, so the whole singles range is read in one pass per run.
* Other shops are searched card by card. The search-result parser reads
  schema.org JSON-LD when present, then optional CSS selectors from
  config.yaml, then a generic "link + £ price" heuristic.

Shop sites were not reachable from the environment this was written in, so
use ``cardsniper probe "<shop name>" "<card>"`` to check each one works and
set ``selectors`` in config.yaml if the heuristic picks the wrong prices.
"""

from __future__ import annotations

import json
import re
from typing import Iterator
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..conditions import normalize
from ..config import Config, StoreConfig
from ..currency import parse_price
from ..deals import RawListing
from ..fetch import Fetcher
from ..matching import NON_ENGLISH, detect_finish, norm
from .base import ScanContext, Source

_OUT_OF_STOCK = re.compile(r"out of stock|sold out|unavailable|notify me", re.I)
_BRACKETS = re.compile(r"\[([^\]]+)\]")
_NON_FOIL_WORDS = re.compile(r"\bnon[\s-]?foil\b|\bnonfoil\b", re.I)


def _stated_finish(text: str) -> str | None:
    """Foil/etched if the listing says so; otherwise let the matcher decide (foil-only printings)."""
    finish = detect_finish(text)
    return None if finish == "nonfoil" and not _NON_FOIL_WORDS.search(text) else finish


def _contains_words(haystack: str, phrase: str) -> bool:
    return f" {phrase} " in f" {haystack} "


def split_title(title: str) -> tuple[str, str | None]:
    """'Ugin, Eye of the Storms (Borderless) [Tarkir: Dragonstorm]' -> (name, set)."""
    set_match = _BRACKETS.search(title)
    name = re.split(r"\s+[\[(]|\s+-\s+|\s+\|\s+", title, maxsplit=1)[0].strip()
    return name, (set_match.group(1).strip() if set_match else None)


class StoreSource(Source):
    def __init__(self, cfg: Config, store: StoreConfig):
        super().__init__(cfg)
        self.store = store
        self.key = store.key
        self.label = store.name
        self.base = store.url.rstrip("/")
        self._platform: str | None = None if store.platform == "auto" else store.platform

    def make_fetcher(self, **kw) -> Fetcher:
        return Fetcher(self.cfg.http, self.cfg.data_dir,
                       min_delay=self.store.min_delay_seconds, max_delay=self.store.max_delay_seconds)

    def platform(self, fetcher: Fetcher) -> str:
        if self._platform is None:
            try:
                page = fetcher.get(f"{self.base}/products.json", params={"limit": 1}, retries=1)
            except Exception:
                return "html"  # couldn't tell (network trouble) - decide again next run
            self._platform = "shopify" if '"products"' in page.text[:200] else "html"
        return self._platform

    def _listing(self, **kw) -> RawListing:
        return RawListing(source=self.key, source_label=self.label, shipping=self.store.shipping_gbp,
                          shipping_note=self.store.shipping_note or "see shop website",
                          seller=self.store.name, seller_location="United Kingdom", **kw)

    # -- Shopify ---------------------------------------------------------------
    def shopify_products_url(self) -> str:
        if self.store.collection:
            return f"{self.base}/collections/{self.store.collection}/products.json"
        return f"{self.base}/products.json"

    def shopify_listings(self, product: dict) -> list[RawListing]:
        ptype = f"{product.get('product_type', '')} {' '.join(product.get('tags') or [])}"
        if self.store.product_type_pattern and not re.search(self.store.product_type_pattern, ptype):
            return []
        title = product.get("title", "")
        name, set_name = split_title(title)
        images = product.get("images") or []
        out = []
        for v in product.get("variants") or []:
            if not v.get("available", True):
                continue
            vtitle = v.get("title") or ""
            text = f"{title} {vtitle}"
            language = "en"
            m = NON_ENGLISH.search(vtitle)
            if m:
                language = m.group(1).lower()
            condition = normalize(vtitle) if vtitle.lower() != "default title" else None
            try:
                price = float(v["price"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(self._listing(
                listing_key=str(v.get("id")),
                url=f"{self.base}/products/{product.get('handle')}?variant={v.get('id')}",
                title=title if vtitle.lower() == "default title" else f"{title} - {vtitle}",
                price=price, condition=condition or self.store.assume_condition,
                language=language, finish=_stated_finish(text), name_hint=name, name_hint_exact=True,
                set_hint=set_name,
                image_url=images[0].get("src") if images else None,
            ))
        return out

    def scan_shopify(self, ctx: ScanContext, fetcher: Fetcher) -> Iterator[RawListing]:
        url = self.shopify_products_url()
        for page_no in range(1, self.store.max_pages + 1):
            try:
                page = fetcher.get(url, params={"limit": 250, "page": page_no})
                products = page.json().get("products") or []
            except Exception as exc:
                ctx.error(f"{self.label}: page {page_no}: {exc}")
                break
            if not products:
                break
            for product in products:
                for listing in self.shopify_listings(product):
                    yield listing
            ctx.checked(self.key, f"page:{page_no}")

    # -- generic HTML search -----------------------------------------------------
    def search_url(self, query: str) -> str:
        template = self.store.search_url or f"{self.base}/search?q={{query}}"
        return template.format(query=quote_plus(query))

    def parse_search(self, html: str, page_url: str, card_name: str) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        found = self._from_selectors(soup, page_url) if self.store.selectors else []
        if not found:
            found = self._from_json_ld(soup, page_url)
        if not found:
            found = self._from_heuristic(soup, page_url, card_name)
        target = norm(card_name)
        best: dict[str, tuple] = {}  # url -> entry; keep the most descriptive title per product
        for entry in found:
            title, url, price, in_stock = entry
            if not in_stock or not _contains_words(norm(title), target):
                continue
            if url not in best or len(title) > len(best[url][0]):
                best[url] = entry
        out = []
        for title, url, price, _ in best.values():
            name, set_name = split_title(title)
            out.append(self._listing(
                listing_key=url, url=url, title=title, price=price[0], currency=price[1],
                condition=normalize(title) or self.store.assume_condition, language=None,
                finish=_stated_finish(title), name_hint=card_name, name_hint_exact=norm(name) == target,
                set_hint=set_name,
            ))
        return out

    def _from_selectors(self, soup, page_url):
        sel = self.store.selectors
        out = []
        for item in soup.select(sel.get("item", "")):
            title_el = item.select_one(sel["title"]) if sel.get("title") else None
            link_el = item.select_one(sel.get("link", "a[href]"))
            price_el = item.select_one(sel["price"]) if sel.get("price") else None
            if not (title_el and link_el and price_el):
                continue
            price = parse_price(price_el.get_text(" ", strip=True))
            if not price:
                continue
            stock_el = item.select_one(sel["stock"]) if sel.get("stock") else None
            in_stock = not (stock_el and _OUT_OF_STOCK.search(stock_el.get_text(" ", strip=True)))
            if not sel.get("stock"):
                in_stock = not _OUT_OF_STOCK.search(item.get_text(" ", strip=True))
            out.append((title_el.get_text(" ", strip=True), urljoin(page_url, link_el["href"]), price, in_stock))
        return out

    def _from_json_ld(self, soup, page_url):
        out = []

        def visit(node):
            if isinstance(node, list):
                for n in node:
                    visit(n)
            elif isinstance(node, dict):
                if node.get("@type") == "Product" and node.get("offers"):
                    offers = node["offers"]
                    offers = offers if isinstance(offers, list) else [offers]
                    for offer in offers:
                        if offer.get("@type") == "AggregateOffer":
                            continue
                        try:
                            amount = float(offer.get("price"))
                        except (TypeError, ValueError):
                            continue
                        in_stock = "InStock" in str(offer.get("availability", "InStock"))
                        url = urljoin(page_url, offer.get("url") or node.get("url") or page_url)
                        out.append((node.get("name", ""), url, (amount, offer.get("priceCurrency", "GBP")), in_stock))
                for key in ("itemListElement", "item", "@graph"):
                    if key in node:
                        visit(node[key])

        for script in soup.select('script[type="application/ld+json"]'):
            try:
                visit(json.loads(script.string or ""))
            except ValueError:
                continue
        return out

    def _from_heuristic(self, soup, page_url, card_name):
        target = norm(card_name)
        out = []
        for a in soup.select("a[href]"):
            text = a.get_text(" ", strip=True)
            if not text and a.find("img"):
                text = a.find("img").get("alt", "")
            if not text or target not in norm(text):
                continue
            node = a
            for _ in range(6):
                node = node.parent
                if node is None:
                    break
                blob = node.get_text(" ", strip=True)
                if "£" in blob:
                    price = parse_price(blob[blob.index("£"):])
                    if price:
                        out.append((text, urljoin(page_url, a["href"]), price, not _OUT_OF_STOCK.search(blob)))
                    break
        return out

    def scan_html(self, ctx: ScanContext, fetcher: Fetcher) -> Iterator[RawListing]:
        targets = ctx.name_targets(self.key, self.store.max_searches_per_run)
        for t in targets:
            if ctx.evaluator.max_deal_price(ctx.index.by_name.get(t.name_norm) or []) is None:
                ctx.checked(self.key, t.name_norm)
                continue
            try:
                page = fetcher.get(self.search_url(t.name))
                yield from self.parse_search(page.text, page.url, t.name)
            except Exception as exc:
                if ctx.failed(f"{self.label} search {t.name!r}: {exc}"):
                    return
                continue
            ctx.checked(self.key, t.name_norm)

    # -- entry points ---------------------------------------------------------------
    def scan(self, ctx: ScanContext) -> Iterator[RawListing]:
        with self.make_fetcher() as fetcher:
            platform = self.platform(fetcher)
            ctx.log.info("%s: %s shop", self.label, platform)
            if platform == "shopify":
                yield from self.scan_shopify(ctx, fetcher)
            else:
                yield from self.scan_html(ctx, fetcher)

    def probe(self, ctx: ScanContext, card_name: str, fetcher: Fetcher | None = None,
              save_html: str | None = None) -> Iterator[RawListing]:
        with (fetcher or self.make_fetcher()) as f:
            platform = self.platform(f)
            if platform == "shopify":
                page = f.get(f"{self.base}/search/suggest.json",
                             params={"q": card_name, "resources[type]": "product", "resources[limit]": 10})
                handles = [p.get("handle") for p in
                           page.json().get("resources", {}).get("results", {}).get("products", [])]
                for handle in handles:
                    product = f.get(f"{self.base}/products/{handle}.json").json().get("product") or {}
                    yield from self.shopify_listings(product)
            else:
                page = f.get(self.search_url(card_name))
                if save_html:
                    with open(save_html, "w", encoding="utf-8") as fh:
                        fh.write(page.text)
                yield from self.parse_search(page.text, page.url, card_name)


def make_store_source(cfg: Config, store: StoreConfig) -> StoreSource:
    return StoreSource(cfg, store)
