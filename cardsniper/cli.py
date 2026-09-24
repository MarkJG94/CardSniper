"""Command line entry point: ``cardsniper <command>``."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .db import Database


def setup_logging(data_dir: Path, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file = RotatingFileHandler(data_dir / "cardsniper.log", maxBytes=5_000_000, backupCount=3)
    file.setFormatter(fmt)
    root.addHandler(file)
    for noisy in ("httpx", "httpcore", "apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(cfg, db, args):
    import uvicorn

    from .scanner import Scanner
    from .scheduler import Jobs
    from .web.app import create_app

    scanner = Scanner(cfg, db)
    jobs = Jobs(scanner)
    app = create_app(cfg, db, scanner, jobs, start_jobs=not args.no_scheduler)

    uvicorn.run(app, host=args.host or cfg.web_host, port=args.port or cfg.web_port, log_level="warning")


def cmd_refresh_cards(cfg, db, args):
    from .scanner import Scanner

    result = Scanner(cfg, db, sources=[]).refresh_cards(trigger="cli", force=args.force)
    print(result)


def cmd_scan(cfg, db, args):
    from .scanner import Scanner

    scanner = Scanner(cfg, db)
    with db.session() as s:
        from .models import Card
        if s.query(Card.id).first() is None:
            print("Card database is empty - refreshing it first")
            scanner.refresh_cards(trigger="cli")
    scanner.scan(only=args.source or None, trigger="cli")


def cmd_probe(cfg, db, args):
    """Check one source against the live site for one card, showing every decision."""
    from .scanner import Scanner

    scanner = Scanner(cfg, db)
    src = scanner.source(args.source)
    session = db.Session()
    try:
        ctx = scanner.context(session, probe=True)
        if src.available():
            print(f"{src.label}: {src.available()}")
            return 1
        count = 0
        for listing in src.probe(ctx, args.card, save_html=args.save_html):
            count += 1
            ev = ctx.evaluator.evaluate(listing)
            flag = "DEAL" if ev.is_deal else "    "
            ref = f"market £{ev.reference_gbp:.2f}" if ev.reference_gbp else ""
            printing = ""
            if ev.match:
                printing = ev.match.unique.printing if ev.match.unique else f"{len(ev.match.candidates)} possible printings"
            print(f"{flag} {listing.price:>8.2f} {listing.currency} | {listing.condition or '??'} | "
                  f"{(listing.finish or '?'):7} | {listing.title[:70]}")
            print(f"       -> {ev.reason} {ref} {printing}")
            print(f"       {listing.url}")
        print(f"\n{count} listings from {src.label}")
        if args.save_html:
            print(f"Raw page saved to {args.save_html}")
    finally:
        session.rollback()
        session.close()
    return 0


def cmd_test_notify(cfg, db, args):
    from .notify import Notifier
    from .settings import load_settings

    with db.session() as s:
        settings = load_settings(s, cfg.defaults)
    notifier = Notifier(cfg, settings)
    if not notifier.channels:
        print("No notification channels configured - see .env.example")
        return 1
    for channel, error in notifier.send_test().items():
        print(f"{channel}: {'OK' if error is None else 'FAILED - ' + error}")
    return 0


def cmd_init(cfg_path: Path):
    root = Path(__file__).resolve().parent.parent
    for src, dst in (("config.example.yaml", cfg_path), (".env.example", Path(".env"))):
        if dst.exists():
            print(f"{dst} already exists - leaving it alone")
        elif (root / src).exists():
            shutil.copy(root / src, dst)
            print(f"Created {dst} - edit it before starting")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cardsniper", description=__doc__)
    parser.add_argument("-c", "--config", help="path to config.yaml (default ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the dashboard and the scheduler")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-scheduler", action="store_true", help="dashboard only, no automatic scans")

    p = sub.add_parser("refresh-cards", help="download the latest card list and prices from Scryfall")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("scan", help="run a scan now")
    p.add_argument("--source", action="append", help="only this source (repeatable), e.g. ebay, cardmarket")

    p = sub.add_parser("probe", help="test a source against the live site for one card")
    p.add_argument("source", help="source name, e.g. cardmarket, ebay, 'Total Cards'")
    p.add_argument("card", help="exact card name")
    p.add_argument("--save-html", help="save the fetched page here for debugging")

    sub.add_parser("test-notify", help="send a test email / Telegram message")
    sub.add_parser("init", help="create config.yaml and .env from the examples")

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(Path(args.config or "config.yaml"))
    cfg = load_config(args.config)
    setup_logging(cfg.data_dir, args.verbose)
    db = Database(cfg.db_path)
    handler = {
        "serve": cmd_serve, "refresh-cards": cmd_refresh_cards, "scan": cmd_scan,
        "probe": cmd_probe, "test-notify": cmd_test_notify,
    }[args.command]
    return handler(cfg, db, args) or 0


if __name__ == "__main__":
    sys.exit(main())
