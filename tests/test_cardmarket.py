import logging

import pytest
from sqlalchemy import create_engine, text

from cardsniper import priceguide
from cardsniper.db import Database
from cardsniper.deals import DealRecorder, Evaluator
from cardsniper.matching import CardIndex
from cardsniper.models import Card, Deal
from cardsniper.notify import format_deal, telegram_text
from cardsniper.settings import UserSettings
from cardsniper.sources.base import ScanContext
from cardsniper.sources.cardmarket import CardmarketSource

from conftest import FakeNotifier


def sheoldred(session) -> Card:
    return session.query(Card).filter_by(name="Sheoldred, the Apocalypse", set_code="dmu",
                                         collector_number="107").one()


def guide_for(card: Card, **values) -> dict:
    row = {"idProduct": card.cardmarket_id, "idCategory": 1, "avg": 60.0, "low": 58.0, "trend": 60.0,
           "avg1": 59.0, "avg7": 60.5, "avg30": 61.0, "low-foil": 74.0, "trend-foil": 75.0}
    row.update(values)
    return {"version": 1, "createdAt": "2026-09-26T02:00:00+0200", "priceGuides": [row]}


@pytest.fixture
def load_guide(db, cfg, monkeypatch):
    def load(**values):
        with db.session() as s:
            data = guide_for(sheoldred(s), **values)
        monkeypatch.setattr(priceguide, "download", lambda cfg: data)
        with db.session() as s:
            return priceguide.refresh_price_guide(s, cfg, force=True)
    return load


def test_parse_variants():
    created, rows = priceguide.parse_price_guide(
        {"createdAt": "x", "priceGuides": [{"idProduct": 5, "trend": 2.5, "lowFoil": 1, "low_foil": 1, "low": 0}]})
    assert created == "x"
    assert rows[0]["id"] == 5 and rows[0]["cm_trend"] == 2.5 and rows[0]["cm_low_foil"] == 1.0
    assert rows[0]["cm_low"] is None  # zero means "no data"
    assert priceguide.parse_price_guide([{"idProduct": 7, "trend": 1}])[1][0]["id"] == 7


def test_import_and_reference_price(db, load_guide):
    result = load_guide(trend=100.0)
    assert result["printings_updated"] == 1
    with db.session() as s:
        card = sheoldred(s)
        assert card.cm_trend == 100.0 and card.cm_low == 58.0 and card.cm_updated is not None
        assert card.ref_eur == 100.0  # price guide beats Scryfall's copy
        index = CardIndex.from_session(s)
    assert index.by_id[card.id].eur == 100.0


def test_refresh_skips_when_recent(db, cfg, load_guide, monkeypatch):
    load_guide()
    monkeypatch.setattr(priceguide, "download", lambda cfg: pytest.fail("should not download"))
    with db.session() as s:
        assert priceguide.refresh_price_guide(s, cfg) is None


def make_ctx(cfg, session, settings):
    from cardsniper.currency import Fx

    fx = Fx(0.85)
    index = CardIndex.from_session(session)
    return ScanContext(cfg=cfg, session=session, settings=settings, fx=fx, index=index,
                       evaluator=Evaluator(settings, index, fx, []), rules=[], log=logging.getLogger())


def test_signal_when_lowest_listing_far_below_trend(db, cfg, load_guide, monkeypatch):
    load_guide(low=36.0, trend=60.0)  # 40% below trend
    monkeypatch.setattr(priceguide, "download", lambda cfg: pytest.fail("fresh guide, no download"))
    settings = UserSettings(allow_unknown_condition=False, min_condition="NM")
    with db.session() as s:
        ctx = make_ctx(cfg, s, settings)
        listings = list(CardmarketSource(cfg).scan(ctx))
        assert [l.finish for l in listings] == ["nonfoil"]  # foil: lowest 74 vs trend 75 is filtered out early
        signal = listings[0]
        assert signal.indicative and signal.currency == "EUR" and signal.price == 36.0
        assert "sellerCountry=13" in signal.url and "language=1" in signal.url
        assert "minCondition=2" in signal.url and "isFoil=N" in signal.url
        ev = ctx.evaluator.evaluate(signal)
        assert ev.is_deal, ev.reason  # condition unknown is fine for signals
        assert ev.threshold_pct == 30.0 and ev.discount_pct == pytest.approx(40, abs=0.5)
        deal, sent = DealRecorder(s, settings, FakeNotifier()).record(ev)
        assert sent and deal.indicative and deal.seller == "Any EU seller"


def test_signal_below_cardmarket_threshold_is_not_a_deal(db, cfg, load_guide):
    load_guide(low=48.0, trend=60.0)  # only 20% below: fine for eBay, not for the Cardmarket threshold
    with db.session() as s:
        ctx = make_ctx(cfg, s, UserSettings())
        [signal] = list(CardmarketSource(cfg).scan(ctx))
        ev = ctx.evaluator.evaluate(signal)
        assert not ev.is_deal and "need 30%" in ev.reason


def test_probe_shows_card_without_prefilter(db, cfg, load_guide):
    load_guide(low=59.0, trend=60.0)
    with db.session() as s:
        ctx = make_ctx(cfg, s, UserSettings())
        out = list(CardmarketSource(cfg).probe(ctx, "Sheoldred, the Apocalypse"))
        assert {l.finish for l in out} == {"nonfoil", "foil"}


def test_scan_without_price_guide_reports_error(db, cfg, monkeypatch):
    monkeypatch.setattr(priceguide, "download", lambda cfg: (_ for _ in ()).throw(RuntimeError("HTTP 403")))
    with db.session() as s:
        ctx = make_ctx(cfg, s, UserSettings())
        assert list(CardmarketSource(cfg).scan(ctx)) == []
        assert any("HTTP 403" in m for m in ctx.stats["error_messages"])
        assert any("No Cardmarket price guide" in m for m in ctx.stats["error_messages"])


def test_indicative_alert_wording():
    deal = Deal(source="cardmarket", source_label="Cardmarket", listing_key="1:nonfoil", card_key="k",
                card_name="Sheoldred", printing="Dominaria United #107", url="https://cm/x", title="t",
                finish="nonfoil", condition=None, price_gbp=30, reference_gbp=51, discount_pct=41,
                seller="Any EU seller", listing_type="fixed", indicative=True)
    subject, body, html = format_deal(deal, "Europe/London")
    assert subject.startswith("Cardmarket: Sheoldred listed 41% below trend") and "check UK sellers" in subject
    assert "any EU seller" in body and "Check UK sellers: https://cm/x" in body
    assert "Check UK sellers on Cardmarket" in html
    assert "Cardmarket listing 41% below trend" in telegram_text(deal, "Europe/London")


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE cards (id VARCHAR(36) PRIMARY KEY, name VARCHAR(300))"))
    engine.dispose()
    db = Database(path)
    with db.engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(cards)"))}
    assert {"cm_trend", "cm_low_foil", "set_code"} <= cols
