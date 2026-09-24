from datetime import timedelta

import pytest

from cardsniper.deals import DealRecorder, Evaluator, RawListing
from cardsniper.models import Alert, Card, Deal, WatchRule, utcnow
from cardsniper.settings import UserSettings

from conftest import FakeNotifier


@pytest.fixture
def sheoldred(index):
    return next(c for c in index.by_name["sheoldred the apocalypse"]
                if c.set_code == "dmu" and c.collector_number == "107")


def listing(card, price, **kw):
    base = dict(source="cardmarket", source_label="Cardmarket", listing_key=kw.pop("key", "a1"),
                url="https://example/1", title=f"{card.name} [{card.set_name}]", price=price,
                currency="GBP", condition="NM", language="en", finish="nonfoil", card_id=card.id)
    base.update(kw)
    return RawListing(**base)


def ref_gbp(card, fx, finish="nonfoil"):
    return fx.to_gbp(card.eur_for(finish), "EUR")


def test_deal_found(index, fx, settings, sheoldred):
    ev = Evaluator(settings, index, fx, []).evaluate(listing(sheoldred, ref_gbp(sheoldred, fx) * 0.7))
    assert ev.is_deal
    assert ev.discount_pct == pytest.approx(30, abs=0.2)
    assert ev.card_key.endswith(f"|nonfoil|{sheoldred.id}")


def test_not_cheap_enough(index, fx, settings, sheoldred):
    ev = Evaluator(settings, index, fx, []).evaluate(listing(sheoldred, ref_gbp(sheoldred, fx) * 0.9))
    assert not ev.is_deal and "only 10% below" in ev.reason


@pytest.mark.parametrize("kw,reason", [
    ({"condition": "GD"}, "condition GD worse than EX"),
    ({"graded": True}, "graded card"),
    ({"language": "german"}, "language german"),
])
def test_rejections(index, fx, settings, sheoldred, kw, reason):
    ev = Evaluator(settings, index, fx, []).evaluate(listing(sheoldred, 1.0, **kw))
    assert not ev.is_deal and ev.reason == reason


def test_unknown_condition_setting(index, fx, sheoldred):
    price = ref_gbp(sheoldred, fx) * 0.5
    allow = Evaluator(UserSettings(allow_unknown_condition=True), index, fx, [])
    deny = Evaluator(UserSettings(allow_unknown_condition=False), index, fx, [])
    assert allow.evaluate(listing(sheoldred, price, condition=None)).is_deal
    assert not deny.evaluate(listing(sheoldred, price, condition=None)).is_deal


def test_min_reference_and_watch_rule_override(index, fx):
    opt = next(c for c in index.by_name["opt"] if c.eur)
    cheap = listing(opt, 0.01)
    assert "below minimum" in Evaluator(UserSettings(), index, fx, []).evaluate(cheap).reason
    rule = WatchRule(oracle_name="opt", display_name="Opt", finish="any", enabled=True)
    assert Evaluator(UserSettings(), index, fx, [rule]).evaluate(cheap).is_deal


def test_watchlist_mode(index, fx, sheoldred):
    s = UserSettings(scan_mode="watchlist")
    price = ref_gbp(sheoldred, fx) * 0.7
    assert Evaluator(s, index, fx, []).evaluate(listing(sheoldred, price)).reason == "not on watchlist"
    strict = WatchRule(card_id=sheoldred.id, display_name="x", finish="nonfoil", discount_percent=40, enabled=True)
    ev = Evaluator(s, index, fx, [strict]).evaluate(listing(sheoldred, price))
    assert not ev.is_deal and "need 40%" in ev.reason
    foil_only = WatchRule(card_id=sheoldred.id, display_name="x", finish="foil", enabled=True)
    assert Evaluator(s, index, fx, [foil_only]).evaluate(listing(sheoldred, price)).reason == "not on watchlist"


def test_rule_condition_override(index, fx, settings, sheoldred):
    rule = WatchRule(oracle_name="sheoldred the apocalypse", display_name="x", finish="any",
                     min_condition="NM", enabled=True)
    ev = Evaluator(settings, index, fx, [rule]).evaluate(listing(sheoldred, 1.0, condition="EX"))
    assert ev.reason == "condition EX worse than NM"


def test_auction_window(index, fx, settings, sheoldred):
    price = ref_gbp(sheoldred, fx) * 0.5
    ev = Evaluator(settings, index, fx, [])
    far = listing(sheoldred, price, listing_type="auction", auction_end=utcnow() + timedelta(days=5))
    soon = listing(sheoldred, price, listing_type="auction", auction_end=utcnow() + timedelta(hours=3))
    assert ev.evaluate(far).reason == "auction ends outside the alert window"
    assert ev.evaluate(soon).is_deal


def test_free_text_title_evaluation(index, fx, settings):
    ebay = RawListing(source="ebay", source_label="eBay", listing_key="1", url="u",
                      title="MTG Sheoldred, the Apocalypse DMU NM", price=1.0)
    ev = Evaluator(settings, index, fx, []).evaluate(ebay)
    assert ev.is_deal and ev.match.set_matched


def test_max_deal_price(index, fx, settings, sheoldred):
    ev = Evaluator(settings, index, fx, [])
    top = ev.max_deal_price([sheoldred])
    expected = max(ref_gbp(sheoldred, fx, f) for f in sheoldred.finishes) * 0.8
    assert top == pytest.approx(expected, abs=0.02)
    assert Evaluator(UserSettings(scan_mode="watchlist"), index, fx, []).max_deal_price([sheoldred]) is None


def test_recorder_only_alerts_when_better(db, index, fx, settings, sheoldred):
    ref = ref_gbp(sheoldred, fx)
    evaluator = Evaluator(settings, index, fx, [])
    notifier = FakeNotifier()
    now = utcnow()
    with db.session() as s:
        rec = DealRecorder(s, settings, notifier)

        def run(price, key, when=now):
            return rec.record(evaluator.evaluate(listing(sheoldred, price, key=key)), now=when)

        assert run(ref * 0.7, "a")[1] is True
        deal, sent = run(ref * 0.7, "a")
        assert not sent and "already alerted for this listing" in deal.suppressed_reason
        deal, sent = run(ref * 0.75, "b")
        assert not sent and "not cheaper" in deal.suppressed_reason
        assert run(ref * 0.6, "c")[1] is True
        # outside the re-alert window a worse deal alerts again
        assert run(ref * 0.78, "d", when=now + timedelta(days=8))[1] is True
    assert len(notifier.sent) == 3
    with db.session() as s:
        assert s.query(Alert).count() == 3
        assert s.query(Deal).count() == 4


def test_recorder_retries_after_send_failure(db, index, fx, settings, sheoldred):
    evaluator = Evaluator(settings, index, fx, [])
    ev = evaluator.evaluate(listing(sheoldred, 1.0))
    with db.session() as s:
        deal, sent = DealRecorder(s, settings, FakeNotifier(fail=True)).record(ev)
        assert not sent and deal.suppressed_reason.startswith("sending failed")
        deal, sent = DealRecorder(s, settings, FakeNotifier()).record(ev)
        assert sent


def test_recorder_without_channels(db, index, fx, settings, sheoldred):
    ev = Evaluator(settings, index, fx, []).evaluate(listing(sheoldred, 1.0))
    with db.session() as s:
        deal, sent = DealRecorder(s, settings, None).record(ev)
        assert not sent and deal.suppressed_reason == "no notification channel configured"
