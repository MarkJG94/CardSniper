"""End-to-end: fake source -> scanner -> deals/alerts in the DB, plus targets and the dashboard."""

from fastapi.testclient import TestClient

from cardsniper import scanner as scanner_mod
from cardsniper.cardsdb import card_row
from cardsniper.currency import Fx
from cardsniper.deals import RawListing
from cardsniper.models import Alert, Card, CheckState, Deal, ScanRun, WatchRule
from cardsniper.notify import format_deal, telegram_text
from cardsniper.scanner import Scanner
from cardsniper.settings import UserSettings, save_settings
from cardsniper.sources.base import Source
from cardsniper.targets import card_targets, name_targets
from cardsniper.web.app import create_app

from conftest import FakeNotifier, scryfall_sample


class FakeSource(Source):
    key = "fake"
    label = "Fake Shop"

    def scan(self, ctx):
        for t in ctx.name_targets(self.key, 50):
            ctx.checked(self.key, t.name_norm)
        yield RawListing(source="fake", source_label="Fake Shop", listing_key="1", url="https://fake/1",
                         title="Sheoldred, the Apocalypse [Dominaria United]", price=1.00, condition="NM",
                         shipping=1.5)
        yield RawListing(source="fake", source_label="Fake Shop", listing_key="2", url="https://fake/2",
                         title="Sheoldred, the Apocalypse [Dominaria United]", price=500.0, condition="NM")
        raise_after = RawListing(source="fake", source_label="Fake Shop", listing_key="3", url="u",
                                 title="garbage listing", price=1.0)
        yield raise_after


class BrokenSource(Source):
    key = "broken"
    label = "Broken"

    def scan(self, ctx):
        raise RuntimeError("site changed")
        yield  # pragma: no cover


def test_scan_end_to_end(cfg, db, monkeypatch):
    fake_notifier = FakeNotifier()
    monkeypatch.setattr(scanner_mod, "Notifier", lambda cfg, settings: fake_notifier)
    sc = Scanner(cfg, db, sources=[FakeSource(cfg), BrokenSource(cfg)])
    run_ids = sc.scan(trigger="test")
    assert len(run_ids) == 2
    with db.session() as s:
        ok, broken = (s.get(ScanRun, i) for i in run_ids)
        assert ok.status == "ok" and ok.listings_seen == 3 and ok.deals_found == 1 and ok.alerts_sent == 1
        assert ok.items_checked > 0
        assert broken.status == "error" and "site changed" in broken.message
        deal = s.query(Deal).one()
        assert deal.alerted and deal.shipping_gbp == 1.5 and deal.url == "https://fake/1"
        assert s.query(Alert).count() == 1
        assert s.query(CheckState).filter_by(source="fake").count() == ok.items_checked
    assert fake_notifier.sent[0][0] == "Sheoldred, the Apocalypse"
    # second run: same listing, same price -> no new alert
    sc.scan(trigger="test", only=["fake"])
    assert len(fake_notifier.sent) == 1


def test_scan_lock(cfg, db):
    sc = Scanner(cfg, db, sources=[])
    sc.lock.acquire()
    assert sc.scan() is None
    sc.lock.release()


def test_targets_rotate(db):
    fx = Fx(0.85)
    settings = UserSettings(min_reference_gbp=5)
    with db.session() as s:
        first = card_targets(s, "cardmarket", settings, fx, [], 3)
        assert len(first) == 3
        from cardsniper.targets import mark_checked
        for c in first:
            mark_checked(s, "cardmarket", c.id)
        second = card_targets(s, "cardmarket", settings, fx, [], 3)
        assert not {c.id for c in first} & {c.id for c in second}


def test_watched_targets_first(db):
    fx = Fx(0.85)
    rule = WatchRule(oracle_name="opt", display_name="Opt", finish="any", enabled=True)
    with db.session() as s:
        names = name_targets(s, "ebay", UserSettings(), fx, [rule], 2)
        assert names[0].name_norm == "opt"
        only = name_targets(s, "ebay", UserSettings(scan_mode="watchlist"), fx, [rule], 50)
        assert [t.name_norm for t in only] == ["opt"]
        dfc = name_targets(s, "ebay", UserSettings(min_reference_gbp=0), fx, [], 500)
        assert any(t.name == "Bonecrusher Giant" for t in dfc)  # searched by front face


def test_card_row_filters():
    sample = next(scryfall_sample())
    assert card_row(sample)["name"] == sample["name"]
    assert card_row({**sample, "lang": "ja"}) is None
    assert card_row({**sample, "digital": True}) is None
    assert card_row({**sample, "layout": "art_series"}) is None


def test_alert_formatting(db):
    deal = Deal(source="ebay", source_label="eBay", listing_key="1", card_key="k", card_name="Sheoldred",
                printing="Dominaria United #107", url="https://ebay/1", title="Sheoldred NM", finish="foil",
                condition="NM", price_gbp=40, shipping_gbp=None, shipping_note="see listing",
                reference_gbp=60, discount_pct=33.3, seller="bob", seller_location="GB", listing_type="fixed")
    subject, text, html = format_deal(deal, "Europe/London")
    assert "£40.00 Sheoldred (33% below market)" in subject
    assert "https://ebay/1" in text and "Postage: see listing" in text
    assert "Near Mint" in text and "Foil" in text
    assert "Open listing on eBay" in html
    assert "<b>Sheoldred</b>" in telegram_text(deal, "Europe/London")


def test_dashboard_pages(cfg, db):
    sc = Scanner(cfg, db, sources=[FakeSource(cfg)])
    client = TestClient(create_app(cfg, db, sc))
    with db.session() as s:
        card = s.query(Card).filter_by(set_code="dmu", name="Sheoldred, the Apocalypse").first()
        save_settings(s, UserSettings())
    for url in ["/", "/deals", "/cards", "/cards?q=sheoldred", f"/cards/{card.id}", "/watchlist", "/runs",
                "/settings", "/healthz"]:
        assert client.get(url).status_code == 200, url
    r = client.post("/watchlist", data={"name": "Opt", "discount_percent": "30"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    with db.session() as s:
        rule = s.query(WatchRule).one()
        assert rule.oracle_name == "opt" and rule.discount_percent == 30
    r = client.post("/settings", data={"scan_mode": "watchlist", "discount_percent": "25", "min_condition": "NM",
                                       "min_reference_gbp": "2", "scan_interval_hours": "6",
                                       "card_db_refresh_hours": "24", "auction_window_hours": "12",
                                       "realert_days": "3", "include_foils": "on", "enabled_sources": ["fake"]},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"]
    page = client.get("/settings").text
    assert 'value="watchlist" selected' in page and 'value="25.0"' in page


def test_dashboard_password(cfg, db):
    cfg.secrets.dashboard_password = "hunter2"
    client = TestClient(create_app(cfg, db, Scanner(cfg, db, sources=[])))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("me", "hunter2")).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_scheduler_start_and_reschedule(cfg, db):
    from cardsniper.scheduler import Jobs

    with db.session() as s:
        s.add(ScanRun(kind="scan", source="x", status="running"))
    jobs = Jobs(Scanner(cfg, db, sources=[]))
    jobs.start()
    try:
        assert jobs.next_run("scan") is not None and jobs.next_run("carddb") is not None
        jobs.reschedule(UserSettings(scan_interval_hours=2))
        assert jobs.sched.get_job("scan").trigger.interval.total_seconds() == 7200
        with db.session() as s:
            assert s.query(ScanRun).filter_by(status="running").count() == 0
    finally:
        jobs.shutdown()
