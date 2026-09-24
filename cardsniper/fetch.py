"""Polite HTTP fetching with a real-browser fallback for Cloudflare challenges.

Strategy (what works best against Cardmarket from a home connection):
1. curl_cffi impersonating Chrome - same TLS/HTTP2 fingerprint as a real browser,
   cheap and fast.
2. If a Cloudflare challenge comes back, open the page in a real (headful)
   browser with a persistent profile, wait for the challenge to clear, then copy
   the clearance cookies + user agent back into the HTTP session so the next
   requests are cheap again.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import HttpConfig

log = logging.getLogger(__name__)

_CHALLENGE_MARKERS = re.compile(
    r"cf-chl|challenge-platform|cf_chl_opt|Just a moment\.\.\.|Checking your browser|"
    r"Attention Required! \| Cloudflare|cf-turnstile|DDoS protection by",
    re.I,
)


class FetchError(RuntimeError):
    pass


class BlockedError(FetchError):
    """The site served a bot challenge we could not get past."""


@dataclass
class Page:
    url: str
    status: int
    text: str

    def json(self):
        return json.loads(self.text)


def is_challenge(status: int, text: str, headers: dict | None = None) -> bool:
    if headers and str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    return status in (403, 429, 503) and bool(_CHALLENGE_MARKERS.search(text[:20000]))


class Fetcher:
    def __init__(self, http: HttpConfig, data_dir: Path, min_delay: float | None = None,
                 max_delay: float | None = None, mode: str = "auto"):
        self.http = http
        self.mode = mode  # auto | http | browser
        self.min_delay = http.min_delay_seconds if min_delay is None else min_delay
        self.max_delay = http.max_delay_seconds if max_delay is None else max_delay
        self.data_dir = data_dir
        self._last_request: dict[str, float] = {}
        self._session = None
        self._browser: BrowserSession | None = None
        self.requests_made = 0

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    @property
    def session(self):
        if self._session is None:
            from curl_cffi import requests as cffi_requests

            self._session = cffi_requests.Session(impersonate=self.http.impersonate)
            self._load_cookies()
        return self._session

    # -- cookies persisted between runs ------------------------------------
    @property
    def _cookie_file(self) -> Path:
        return self.data_dir / "cookies.json"

    def _load_cookies(self):
        try:
            saved = json.loads(self._cookie_file.read_text())
        except (OSError, ValueError):
            return
        for c in saved.get("cookies", []):
            self._session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        if saved.get("user_agent"):
            self._session.headers["User-Agent"] = saved["user_agent"]

    def _save_cookies(self, cookies: list[dict], user_agent: str | None):
        try:
            self._cookie_file.write_text(json.dumps({"cookies": cookies, "user_agent": user_agent}))
        except OSError as exc:
            log.debug("could not save cookies: %s", exc)

    # -- politeness ------------------------------------------------------
    def _wait_turn(self, url: str):
        host = urlparse(url).netloc
        last = self._last_request.get(host)
        delay = random.uniform(self.min_delay, self.max_delay)
        if last is not None:
            remaining = last + delay - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    # -- fetching --------------------------------------------------------
    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            retries: int = 3, allow_browser: bool = True) -> Page:
        if self.mode == "browser" and allow_browser:
            self._wait_turn(url)
            return self._browser_get(url, params)

        last_exc: Exception | None = None
        for attempt in range(retries):
            self._wait_turn(url)
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.http.timeout_seconds, allow_redirects=True)
                self.requests_made += 1
            except Exception as exc:  # network error
                last_exc = exc
                time.sleep(2 ** attempt * 3)
                continue
            text = resp.text
            if is_challenge(resp.status_code, text, dict(resp.headers)):
                if self.mode == "http" or not allow_browser or self.http.browser == "none":
                    raise BlockedError(f"Bot challenge at {url} (browser fallback disabled)")
                log.info("Challenge at %s - solving in browser", urlparse(url).netloc)
                return self._browser_get(url, params)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = int(resp.headers.get("Retry-After", 0) or 0) or 2 ** attempt * 10
                log.warning("HTTP %s from %s, retrying in %ss", resp.status_code, url, wait)
                time.sleep(min(wait, 120))
                last_exc = FetchError(f"HTTP {resp.status_code}")
                continue
            return Page(str(resp.url), resp.status_code, text)
        raise FetchError(f"GET {url} failed: {last_exc}")

    def _browser_get(self, url: str, params: dict | None) -> Page:
        if params:
            from urllib.parse import urlencode

            url = url + ("&" if "?" in url else "?") + urlencode(params)
        if self._browser is None:
            self._browser = BrowserSession(self.http, self.data_dir)
        page = self._browser.get(url)
        cookies, ua = self._browser.cookies_and_ua()
        # hand clearance back to the fast HTTP session
        for c in cookies:
            self.session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        if ua:
            self.session.headers["User-Agent"] = ua
        self._save_cookies(cookies, ua)
        self.requests_made += 1
        return page


class BrowserSession:
    """A real browser (Playwright Chromium or Camoufox) with a persistent profile."""

    def __init__(self, http: HttpConfig, data_dir: Path):
        self.http = http
        self.profile = data_dir / f"browser-{http.browser}"
        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._cm = None
        self.context = None
        self._start()

    def _start(self):
        headless = self.http.headless
        if not headless and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            log.warning("No display available - running the browser headless. Run under xvfb-run "
                        "for a much better chance of passing bot checks.")
            headless = True
        if self.http.browser == "camoufox":
            from camoufox.sync_api import Camoufox  # optional dependency

            self._cm = Camoufox(headless=headless, persistent_context=True,
                                user_data_dir=str(self.profile), humanize=True)
            self.context = self._cm.__enter__()
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            str(self.profile),
            headless=headless,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
            executable_path=self.http.browser_executable or None,
        )
        self.context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    def get(self, url: str) -> Page:
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=int(self.http.timeout_seconds * 1000))
        deadline = time.monotonic() + self.http.challenge_timeout_seconds
        status = resp.status if resp else 0
        while True:
            html = page.content()
            if not is_challenge(403, html):
                break
            if time.monotonic() > deadline:
                raise BlockedError(f"Could not pass bot challenge at {url} within "
                                   f"{self.http.challenge_timeout_seconds:.0f}s")
            self._try_click_turnstile(page)
            page.wait_for_timeout(2500)
            status = 200
        page.wait_for_timeout(random.randint(500, 1500))
        return Page(page.url, status, page.content())

    @staticmethod
    def _try_click_turnstile(page):
        try:
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    box = frame.frame_element().bounding_box()
                    if box:
                        page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                    return
        except Exception:
            pass

    def cookies_and_ua(self) -> tuple[list[dict], str | None]:
        cookies = self.context.cookies()
        ua = None
        try:
            page = self.context.pages[0] if self.context.pages else None
            ua = page.evaluate("navigator.userAgent") if page else None
        except Exception:
            pass
        return cookies, ua

    def close(self):
        try:
            if self._cm is not None:
                self._cm.__exit__(None, None, None)
            elif self.context is not None:
                self.context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
