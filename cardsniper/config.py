"""Static configuration: config.yaml (sources, paths) + environment secrets.

Values that you tune day to day (thresholds, scan interval, ...) live in the
database and are edited from the dashboard - see ``cardsniper.settings``.
The ``defaults`` block in config.yaml only seeds them on first start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_STORES: list[dict[str, Any]] = [
    # Shopify stores expose their whole catalogue as JSON, so they are scanned
    # in full every run. "auto" probes for Shopify and falls back to searching
    # the site card-by-card.
    {"name": "Total Cards", "url": "https://totalcards.net", "platform": "auto",
     "collection": "magic-the-gathering-single-cards"},
    {"name": "Axion Now", "url": "https://axionnow.com", "platform": "auto"},
    {"name": "Manaleak", "url": "https://www.manaleak.com", "platform": "auto"},
    {"name": "Magic Madhouse", "url": "https://magicmadhouse.co.uk", "platform": "auto"},
    {"name": "Chaos Cards", "url": "https://www.chaoscards.co.uk", "platform": "auto"},
    {"name": "Big Orbit Cards", "url": "https://www.bigorbitcards.co.uk", "platform": "auto",
     "search_url": "https://www.bigorbitcards.co.uk/search/?q={query}"},
    {"name": "Mage Cards", "url": "https://www.magecards.co.uk", "platform": "auto"},
]


@dataclass
class HttpConfig:
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    timeout_seconds: float = 30.0
    impersonate: str = "chrome"
    # Browser used to get past Cloudflare challenges: playwright | camoufox | none
    browser: str = "playwright"
    # Headful browsers pass bot checks far more often. On a server without a
    # screen run under xvfb-run (the Docker image and systemd unit do this).
    headless: bool = False
    challenge_timeout_seconds: float = 60.0
    # Optional path to a Chrome/Chromium binary (default: Playwright's own download)
    browser_executable: str | None = None


@dataclass
class CardmarketConfig:
    enabled: bool = True
    # auto: plain HTTP with browser fallback on challenge; browser: always browser; http: never browser
    fetch_mode: str = "auto"
    max_products_per_run: int = 400
    min_delay_seconds: float = 4.0
    max_delay_seconds: float = 9.0
    seller_country: int = 13  # Cardmarket's id for United Kingdom
    language: int = 1  # English
    base_url: str = "https://www.cardmarket.com/en/Magic"


@dataclass
class EbayConfig:
    enabled: bool = True
    marketplace: str = "EBAY_GB"
    category_ids: str = "183454"  # CCG Individual Cards
    item_location_country: str = "GB"
    condition_ids: str = "4000"  # 4000 = Ungraded (2750 = Graded)
    include_auctions: bool = True
    max_queries_per_run: int = 4000  # Browse API default quota is 5000 calls/day
    results_per_query: int = 100
    sandbox: bool = False


@dataclass
class StoreConfig:
    name: str
    url: str
    platform: str = "auto"  # auto | shopify | html
    enabled: bool = True
    collection: str | None = None  # shopify: limit crawl to /collections/<handle>
    product_type_pattern: str = r"(?i)(single|mtg|magic)"
    max_pages: int = 2000
    search_url: str | None = None  # html: e.g. https://shop/search?q={query}
    selectors: dict[str, str] = field(default_factory=dict)  # html: item/title/price/link/stock
    max_searches_per_run: int = 300
    assume_condition: str = "NM"  # when the store does not state a condition
    shipping_gbp: float | None = None
    shipping_note: str | None = None
    min_delay_seconds: float | None = None
    max_delay_seconds: float | None = None

    @property
    def key(self) -> str:
        return "store:" + "".join(c for c in self.name.lower() if c.isalnum())


@dataclass
class Secrets:
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_ssl: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    dashboard_password: str | None = None

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_to and (self.smtp_from or self.smtp_user))

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ebay_configured(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)


@dataclass
class Config:
    data_dir: Path = Path("./data")
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    timezone: str = "Europe/London"
    fallback_eur_to_gbp: float = 0.85
    defaults: dict[str, Any] = field(default_factory=dict)
    http: HttpConfig = field(default_factory=HttpConfig)
    cardmarket: CardmarketConfig = field(default_factory=CardmarketConfig)
    ebay: EbayConfig = field(default_factory=EbayConfig)
    stores: list[StoreConfig] = field(default_factory=list)
    secrets: Secrets = field(default_factory=Secrets)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cardsniper.db"


def _build(cls, raw: dict[str, Any] | None):
    raw = raw or {}
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} option(s): {', '.join(sorted(unknown))}")
    return cls(**raw)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def load_secrets() -> Secrets:
    port = _env("CARDSNIPER_SMTP_PORT")
    return Secrets(
        smtp_host=_env("CARDSNIPER_SMTP_HOST"),
        smtp_port=int(port) if port else 587,
        smtp_user=_env("CARDSNIPER_SMTP_USER"),
        smtp_password=_env("CARDSNIPER_SMTP_PASSWORD"),
        smtp_from=_env("CARDSNIPER_SMTP_FROM"),
        smtp_to=_env("CARDSNIPER_SMTP_TO"),
        smtp_ssl=(_env("CARDSNIPER_SMTP_SSL") or "").lower() in ("1", "true", "yes"),
        telegram_bot_token=_env("CARDSNIPER_TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("CARDSNIPER_TELEGRAM_CHAT_ID"),
        ebay_client_id=_env("CARDSNIPER_EBAY_CLIENT_ID"),
        ebay_client_secret=_env("CARDSNIPER_EBAY_CLIENT_SECRET"),
        dashboard_password=_env("CARDSNIPER_DASHBOARD_PASSWORD"),
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    load_dotenv(os.environ.get("CARDSNIPER_ENV_FILE", ".env"))
    path = Path(path or os.environ.get("CARDSNIPER_CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    web = raw.get("web") or {}
    sources = raw.get("sources") or {}
    stores_raw = sources.get("stores")
    if stores_raw is None:
        stores_raw = DEFAULT_STORES
    cfg = Config(
        data_dir=Path(os.environ.get("CARDSNIPER_DATA_DIR") or raw.get("data_dir") or "./data"),
        web_host=web.get("host", "0.0.0.0"),
        web_port=int(web.get("port", 8080)),
        timezone=raw.get("timezone", "Europe/London"),
        fallback_eur_to_gbp=float((raw.get("fx") or {}).get("fallback_eur_to_gbp", 0.85)),
        defaults=raw.get("defaults") or {},
        http=_build(HttpConfig, raw.get("http")),
        cardmarket=_build(CardmarketConfig, sources.get("cardmarket")),
        ebay=_build(EbayConfig, sources.get("ebay")),
        stores=[_build(StoreConfig, s) for s in stores_raw],
        secrets=load_secrets(),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
