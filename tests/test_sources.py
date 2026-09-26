import json

from cardsniper.config import StoreConfig
from cardsniper.fetch import is_challenge
from cardsniper.sources.ebay import enrich_from_item, listing_from_summary
from cardsniper.sources.stores import StoreSource, split_title

from conftest import fixture_text


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
