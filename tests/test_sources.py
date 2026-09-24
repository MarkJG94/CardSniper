import json

from cardsniper.config import StoreConfig
from cardsniper.deals import Evaluator
from cardsniper.fetch import Page, is_challenge
from cardsniper.models import Card
from cardsniper.sources.base import ScanContext
from cardsniper.sources.cardmarket import CardmarketSource, parse_offers
from cardsniper.sources.ebay import enrich_from_item, listing_from_summary
from cardsniper.sources.stores import StoreSource, split_title

from conftest import fixture_text


def test_parse_cardmarket_offers():
    offers = parse_offers(fixture_text("cardmarket_product.html"))
    assert len(offers) == 4
    first = offers[0]
    assert first["article_id"] == "1600000001"
    assert first["seller"] == "BritCards"
    assert first["location"] == "United Kingdom"
    assert first["language"] == "English"
    assert first["condition"] == "NM"
    assert (first["price"], first["currency"]) == (49.0, "EUR")
    assert first["quantity"] == 2 and not first["foil"]
    assert offers[1]["foil"] and offers[1]["condition"] == "EX"
    assert offers[2]["special"] == ["playset"]
    assert offers[3]["location"] == "Germany"


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        for key, page in self.pages.items():
            if key in url:
                return page
        raise AssertionError(f"unexpected url {url}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def make_ctx(cfg, db, index, fx, settings, session):
    return ScanContext(cfg=cfg, session=session, settings=settings, fx=fx, index=index,
                       evaluator=Evaluator(settings, index, fx, []), rules=[], log=__import__("logging").getLogger())


def test_cardmarket_listings(cfg, db, index, fx, settings):
    session = db.Session()
    card = session.query(Card).filter_by(name="Sheoldred, the Apocalypse", set_code="dmu", collector_number="107").one()
    product = "https://www.cardmarket.com/en/Magic/Products/Singles/Dominaria-United/Sheoldred-the-Apocalypse"
    fetcher = FakeFetcher({  # first matching key wins
        "Sheoldred-the-Apocalypse": Page(product, 200, fixture_text("cardmarket_product.html")),
        "/Magic/Products": Page(product + "?idProduct=1", 200, "<html></html>"),  # idProduct redirect
    })
    src = CardmarketSource(cfg)
    ctx = make_ctx(cfg, db, index, fx, settings, session)
    listings = src.listings_for(ctx, fetcher, card)
    assert card.cardmarket_url == product
    # playset and German seller are dropped, cheapest first
    assert [l.seller for l in listings] == ["BritCards", "FoilFan"]
    assert listings[0].card_id == card.id and listings[0].currency == "EUR"
    assert listings[1].finish == "foil"
    assert "sellerCountry=13" in listings[0].url and "language=1" in listings[0].url
    assert fetcher.calls[-1][1]["minCondition"] == 3  # EX
    session.close()


def test_shopify_listings(cfg):
    store = StoreSource(cfg, StoreConfig(name="Test Shop", url="https://shop.example", shipping_gbp=1.5))
    products = json.loads(fixture_text("shopify_products.json"))["products"]
    listings = [l for p in products for l in store.shopify_listings(p)]
    by_key = {l.listing_key: l for l in listings}
    assert set(by_key) == {"101", "102", "104", "301"}  # sold-out + sealed product skipped
    assert by_key["101"].condition == "NM" and by_key["101"].price == 45.0
    assert by_key["102"].condition == "EX"
    assert by_key["104"].language == "japanese"
    assert by_key["301"].condition == "NM"  # assumed when not stated
    assert by_key["101"].url == "https://shop.example/products/sheoldred-the-apocalypse-dominaria-united?variant=101"
    assert by_key["101"].shipping == 1.5 and by_key["101"].source == "store:testshop"


def test_store_search_json_ld(cfg):
    store = StoreSource(cfg, StoreConfig(name="Shop", url="https://shop.example", platform="html"))
    out = store.parse_search(fixture_text("store_search_jsonld.html"), "https://shop.example/search?q=x",
                             "Sheoldred, the Apocalypse")
    assert [(l.url, l.price) for l in out] == [("https://shop.example/products/sheoldred-dmu", 44.99)]


def test_store_search_heuristic(cfg):
    store = StoreSource(cfg, StoreConfig(name="Shop", url="https://shop.example", platform="html"))
    out = store.parse_search(fixture_text("store_search_plain.html"), "https://shop.example/search?q=x",
                             "Sheoldred, the Apocalypse")
    assert len(out) == 1
    assert out[0].price == 41.5
    assert out[0].title == "Sheoldred, the Apocalypse - Dominaria United"
    assert out[0].url == "https://shop.example/mtg/sheoldred-the-apocalypse-dmu"


def test_store_search_selectors(cfg):
    store = StoreSource(cfg, StoreConfig(name="Shop", url="https://shop.example", platform="html", selectors={
        "item": ".product-card", "title": "h3 a", "link": "h3 a", "price": ".price", "stock": ".stock"}))
    out = store.parse_search(fixture_text("store_search_plain.html"), "https://shop.example/s",
                             "Sheoldred, the Apocalypse")
    assert [l.price for l in out] == [41.5]


def test_split_title():
    assert split_title("Ugin, Eye of the Storms (Borderless) [Tarkir: Dragonstorm]") == \
        ("Ugin, Eye of the Storms", "Tarkir: Dragonstorm")
    assert split_title("Sheoldred, the Apocalypse - Dominaria United") == ("Sheoldred, the Apocalypse", None)


def test_ebay_summary_and_item():
    items = json.loads(fixture_text("ebay_search.json"))["itemSummaries"]
    fixed, auction, group = (listing_from_summary(i) for i in items)
    assert group is None
    assert fixed.listing_type == "fixed" and fixed.price == 40.0 and fixed.shipping == 1.2
    assert fixed.seller_location == "GB" and not fixed.graded
    assert auction.listing_type == "auction" and auction.auction_end.year == 2030
    enriched = enrich_from_item(fixed, json.loads(fixture_text("ebay_item.json")))
    assert enriched.condition == "NM"
    assert enriched.language == "en"
    assert enriched.set_hint == "Dominaria United"
    assert enriched.finish == "nonfoil"
    assert enriched.shipping == 0.95


def test_challenge_detection():
    assert is_challenge(403, "<title>Just a moment...</title>")
    assert is_challenge(200, "", {"cf-mitigated": "challenge"})
    assert not is_challenge(200, "<title>Just a moment...</title>")
    assert not is_challenge(404, "not found")
