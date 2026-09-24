"""Small web dashboard (FastAPI + server-rendered Jinja templates)."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import secrets as pysecrets
from dataclasses import fields
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_

from ..conditions import CONDITION_NAMES, CONDITIONS
from ..config import Config
from ..currency import get_fx
from ..db import Database
from ..matching import norm
from ..models import Alert, Card, Deal, PriceHistory, ScanRun, WatchRule, utcnow
from ..notify import Notifier
from ..scanner import Scanner
from ..settings import UserSettings, load_settings, save_settings

HERE = Path(__file__).parent


def sparkline(points: list[tuple[str, float]], width: int = 520, height: int = 120) -> str:
    """Inline SVG line chart of (label, value) points."""
    if len(points) < 2:
        return ""
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pad = 8
    step = (width - 2 * pad) / (len(points) - 1)
    coords = [(pad + i * step, height - pad - (v - lo) / span * (height - 2 * pad)) for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    return (f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" '
            f'aria-label="price from £{values[0]:.2f} to £{values[-1]:.2f}">'
            f'<path d="{path}" fill="none" stroke="currentColor" stroke-width="2"/>'
            f'<text x="{pad}" y="12" class="axis">£{hi:.2f}</text>'
            f'<text x="{pad}" y="{height - 2}" class="axis">£{lo:.2f} · {points[0][0]} → {points[-1][0]}</text></svg>')


def create_app(cfg: Config, db: Database, scanner: Scanner, jobs=None, start_jobs: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if jobs and start_jobs:
            jobs.start()
        yield
        if jobs:
            jobs.shutdown()

    app = FastAPI(title="CardSniper", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    tz = ZoneInfo(cfg.timezone)

    def localtime(dt, fmt="%d %b %H:%M"):
        if dt is None:
            return "—"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime(fmt)

    def ago(dt):
        if dt is None:
            return "never"
        secs = (utcnow() - dt.replace(tzinfo=None)).total_seconds()
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= size:
                return f"{int(secs // size)}{unit} ago"
        return "just now"

    templates.env.filters["localtime"] = localtime
    templates.env.filters["ago"] = ago
    templates.env.filters["gbp"] = lambda v: "—" if v is None else f"£{v:,.2f}"
    templates.env.globals.update(CONDITIONS=CONDITIONS, CONDITION_NAMES=CONDITION_NAMES)

    # -- optional password -------------------------------------------------------------
    password = cfg.secrets.dashboard_password
    if password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                    if pysecrets.compare_digest(given, password):
                        return await call_next(request)
                except ValueError:
                    pass
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="CardSniper"'})

    def render(request: Request, name: str, **ctx):
        ctx.update(request=request, current=scanner.current, msg=request.query_params.get("msg"),
                   err=request.query_params.get("err"))
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str | None = None, err: str | None = None):
        sep = "&" if "?" in url else "?"
        if msg:
            url += f"{sep}msg={quote(msg)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        return RedirectResponse(url, status_code=303)

    # -- pages ----------------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db.session() as s:
            since = utcnow() - timedelta(days=1)
            settings = load_settings(s, cfg.defaults)
            last_runs = {}
            for src in scanner.sources:
                last_runs[src.key] = (s.query(ScanRun).filter_by(kind="scan", source=src.key)
                                      .order_by(ScanRun.started_at.desc()).first())
            carddb = (s.query(ScanRun).filter_by(kind="carddb").order_by(ScanRun.started_at.desc()).first())
            stats = {
                "cards": s.query(func.count(Card.id)).scalar(),
                "deals_24h": s.query(func.count(Deal.id)).filter(Deal.first_seen >= since).scalar(),
                "alerts_24h": s.query(func.count(Alert.id)).filter(Alert.sent_at >= since).scalar(),
                "rules": s.query(func.count(WatchRule.id)).filter(WatchRule.enabled.is_(True)).scalar(),
            }
            recent = s.query(Deal).order_by(Deal.last_seen.desc()).limit(12).all()
            fx = get_fx(s, cfg.fallback_eur_to_gbp, fetch=False)
        notifier = Notifier(cfg, settings)
        return render(request, "dashboard.html", stats=stats, recent=recent, sources=scanner.sources,
                      last_runs=last_runs, carddb=carddb, settings=settings, fx=fx,
                      channels=[c.name for c in notifier.channels],
                      next_scan=jobs.next_run("scan") if jobs else None,
                      next_carddb=jobs.next_run("carddb") if jobs else None)

    @app.post("/run")
    def run_now(kind: str = Form("scan"), source: str = Form("")):
        if scanner.current:
            return back("/", err=f"Already running: {scanner.current['source']}")
        if jobs:
            jobs.run_now(kind, [source] if source else None)
        return back("/", msg="Card database refresh started" if kind == "carddb" else
                    f"Scan started{' for ' + source if source else ''} - refresh this page to follow progress")

    @app.get("/deals", response_class=HTMLResponse)
    def deals(request: Request, source: str = "", alerted: str = "", q: str = "", page: int = 1):
        with db.session() as s:
            query = s.query(Deal)
            if source:
                query = query.filter(Deal.source == source)
            if alerted == "1":
                query = query.filter(Deal.alerted.is_(True))
            if q:
                query = query.filter(Deal.card_name.ilike(f"%{q}%"))
            total = query.count()
            rows = query.order_by(Deal.last_seen.desc()).offset((page - 1) * 100).limit(100).all()
        return render(request, "deals.html", deals=rows, total=total, page=page, source=source,
                      alerted=alerted, q=q, sources=scanner.sources)

    @app.get("/cards", response_class=HTMLResponse)
    def cards(request: Request, q: str = ""):
        groups: dict[str, list[Card]] = {}
        with db.session() as s:
            fx = get_fx(s, cfg.fallback_eur_to_gbp, fetch=False)
            if q.strip():
                qn = norm(q)
                found = (s.query(Card).filter(or_(Card.name_norm.like(f"%{qn}%"), Card.front_norm.like(f"%{qn}%")))
                         .order_by(Card.name, Card.released_at.desc()).limit(400).all())
                for c in found:
                    groups.setdefault(c.name, []).append(c)
        return render(request, "cards.html", q=q, groups=groups, fx=fx)

    @app.get("/cards/{card_id}", response_class=HTMLResponse)
    def card_detail(request: Request, card_id: str):
        with db.session() as s:
            card = s.get(Card, card_id)
            if card is None:
                return back("/cards", err="Card not found")
            fx = get_fx(s, cfg.fallback_eur_to_gbp, fetch=False)
            charts = {}
            for finish in ("nonfoil", "foil"):
                pts = (s.query(PriceHistory.day, PriceHistory.ref_gbp)
                       .filter_by(card_id=card_id, finish=finish).order_by(PriceHistory.day).all())
                if len(pts) >= 2:
                    charts[finish] = sparkline([(d.strftime("%d %b"), v) for d, v in pts])
            others = (s.query(Card).filter(Card.name_norm == card.name_norm, Card.id != card.id)
                      .order_by(Card.released_at.desc()).all())
            rules = s.query(WatchRule).filter(or_(WatchRule.card_id == card.id,
                                                   WatchRule.oracle_name == card.name_norm)).all()
            card_deals = (s.query(Deal).filter(or_(Deal.card_id == card.id, Deal.card_key.like(f"{card.name_norm}|%")))
                          .order_by(Deal.last_seen.desc()).limit(50).all())
        return render(request, "card.html", card=card, fx=fx, charts=charts, others=others, rules=rules,
                      deals=card_deals)

    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist(request: Request):
        with db.session() as s:
            rules = s.query(WatchRule).order_by(WatchRule.display_name).all()
            for r in rules:
                _ = r.card  # load relationship before session closes
            settings = load_settings(s, cfg.defaults)
        return render(request, "watchlist.html", rules=rules, settings=settings)

    @app.post("/watchlist")
    def add_rule(card_id: str = Form(""), name: str = Form(""), finish: str = Form("any"),
                 min_condition: str = Form(""), discount_percent: str = Form(""), notes: str = Form(""),
                 next_url: str = Form("/watchlist")):
        with db.session() as s:
            rule = WatchRule(finish=finish if finish in ("any", "foil", "nonfoil") else "any",
                             min_condition=min_condition if min_condition in CONDITIONS else None,
                             notes=notes or None)
            try:
                rule.discount_percent = float(discount_percent) if discount_percent.strip() else None
            except ValueError:
                return back(next_url, err="Discount must be a number")
            if rule.discount_percent is not None and not 0 < rule.discount_percent < 100:
                return back(next_url, err="Discount must be between 0 and 100")
            if card_id:
                card = s.get(Card, card_id)
                if card is None:
                    return back(next_url, err="Card not found")
                rule.card_id = card.id
                rule.display_name = f"{card.name} ({card.set_name} #{card.collector_number})"
            else:
                card = (s.query(Card).filter(or_(Card.name_norm == norm(name), Card.front_norm == norm(name)))
                        .first())
                if card is None:
                    return back(next_url, err=f"No card called '{name}' - check the spelling on the Cards page")
                rule.oracle_name = card.name_norm
                rule.display_name = f"{card.name} (any printing)"
            s.add(rule)
        return back(next_url, msg=f"Watching {rule.display_name}")

    @app.post("/watchlist/{rule_id}/toggle")
    def toggle_rule(rule_id: int):
        with db.session() as s:
            rule = s.get(WatchRule, rule_id)
            if rule:
                rule.enabled = not rule.enabled
        return back("/watchlist")

    @app.post("/watchlist/{rule_id}/delete")
    def delete_rule(rule_id: int, next_url: str = Form("/watchlist")):
        with db.session() as s:
            rule = s.get(WatchRule, rule_id)
            if rule:
                s.delete(rule)
        return back(next_url, msg="Rule removed")

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        with db.session() as s:
            rows = s.query(ScanRun).order_by(ScanRun.started_at.desc()).limit(150).all()
        labels = {src.key: src.label for src in scanner.sources} | {"scryfall": "Card database"}
        return render(request, "runs.html", runs=rows, labels=labels)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        with db.session() as s:
            settings = load_settings(s, cfg.defaults)
        sec = cfg.secrets
        return render(request, "settings.html", settings=settings, sources=scanner.sources, secrets={
            "Email (SMTP)": sec.email_configured, "Telegram": sec.telegram_configured,
            "eBay API": sec.ebay_configured, "Dashboard password": bool(sec.dashboard_password)})

    @app.post("/settings")
    async def save_settings_page(request: Request):
        form = await request.form()
        values = {}
        for f in fields(UserSettings):
            if f.name == "enabled_sources":
                continue
            if isinstance(f.default, bool):
                values[f.name] = f.name in form
            elif f.name in form:
                values[f.name] = form[f.name]
        chosen = form.getlist("enabled_sources")
        values["enabled_sources"] = None if len(chosen) == len(scanner.sources) else list(chosen)
        try:
            new = UserSettings(**{k: (float(v) if isinstance(getattr(UserSettings, k, None), float) else v)
                                  for k, v in values.items()}).validate()
        except (ValueError, TypeError) as exc:
            return back("/settings", err=str(exc))
        with db.session() as s:
            save_settings(s, new)
        if jobs:
            jobs.reschedule(new)
        return back("/settings", msg="Settings saved")

    @app.post("/settings/test-notify")
    def test_notify():
        with db.session() as s:
            settings = load_settings(s, cfg.defaults)
        notifier = Notifier(cfg, settings)
        if not notifier.channels:
            return back("/settings", err="No notification channel configured (see .env)")
        results = notifier.send_test()
        failed = {k: v for k, v in results.items() if v}
        if failed:
            return back("/settings", err="; ".join(f"{k}: {v}" for k, v in failed.items()))
        return back("/settings", msg="Test sent via " + ", ".join(results))

    return app
