"""Alert delivery: email (SMTP) and Telegram."""

from __future__ import annotations

import html
import logging
import smtplib
from datetime import timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx

from ..conditions import CONDITION_NAMES
from ..config import Config
from ..models import Deal
from ..settings import UserSettings

log = logging.getLogger(__name__)

FINISH_LABEL = {"nonfoil": "Non-foil", "foil": "Foil", "etched": "Etched foil"}


def _local(dt, tz: str) -> str:
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M")


def deal_lines(deal: Deal, tz: str) -> list[tuple[str, str]]:
    if deal.indicative:
        return [
            ("Printing", f"{deal.printing or '?'} · {FINISH_LABEL.get(deal.finish, deal.finish)}"),
            ("Lowest listing", f"£{deal.price_gbp:.2f} from any EU seller, any condition/language"),
            ("Market", f"£{deal.reference_gbp:.2f} trend - lowest listing is {deal.discount_pct:.0f}% below"),
            ("Next step", "open the link: it shows UK sellers, English, your minimum condition"),
            ("Where", deal.source_label),
        ]
    postage = (f"£{deal.shipping_gbp:.2f} extra" if deal.shipping_gbp is not None
               else (deal.shipping_note or "see listing"))
    if deal.shipping_gbp is not None and deal.shipping_note:
        postage += f" ({deal.shipping_note})"
    seller = deal.seller or "?"
    if deal.seller_location:
        seller += f" ({deal.seller_location})"
    lines = [
        ("Printing", f"{deal.printing or '?'} · {FINISH_LABEL.get(deal.finish, deal.finish)}"),
        ("Price", f"£{deal.price_gbp:.2f}  (market £{deal.reference_gbp:.2f}, {deal.discount_pct:.0f}% below)"),
        ("Postage", postage),
        ("Condition", CONDITION_NAMES.get(deal.condition or "", "not stated")),
        ("Seller", seller + (f" · qty {deal.quantity}" if deal.quantity else "")),
        ("Where", deal.source_label),
    ]
    if deal.listing_type == "auction":
        ends = _local(deal.auction_end, tz) if deal.auction_end else "unknown"
        lines.insert(2, ("Auction", f"current bid, ends {ends}"))
    return lines


def button_label(deal: Deal) -> str:
    return "Check UK sellers on Cardmarket" if deal.indicative else f"Open listing on {deal.source_label}"


def format_deal(deal: Deal, tz: str) -> tuple[str, str, str]:
    if deal.indicative:
        subject = (f"Cardmarket: {deal.card_name} listed {deal.discount_pct:.0f}% below trend "
                   f"(from £{deal.price_gbp:.2f}) - check UK sellers")
    else:
        subject = f"£{deal.price_gbp:.2f} {deal.card_name} ({deal.discount_pct:.0f}% below market) - {deal.source_label}"
    lines = deal_lines(deal, tz)
    text = "\n".join([f"{'Possible deal' if deal.indicative else 'Deal'}: {deal.card_name}", ""] +
                     [f"{k}: {v}" for k, v in lines] +
                     ["", f"{'Check UK sellers' if deal.indicative else 'Buy'}: {deal.url}", "", f"Listing: {deal.title}"])
    rows = "".join(f"<tr><td style='color:#666;padding:2px 12px 2px 0'>{html.escape(k)}</td>"
                   f"<td>{html.escape(v)}</td></tr>" for k, v in lines)
    image = (f"<img src='{html.escape(deal.image_url)}' width='160' style='float:right;margin-left:12px;"
             f"border-radius:8px'>" if deal.image_url else "")
    body = (f"<div style='font-family:sans-serif;max-width:560px'>{image}"
            f"<h2 style='margin:0 0 8px'>{html.escape(deal.card_name)}</h2>"
            f"<table style='font-size:14px'>{rows}</table>"
            f"<p><a href='{html.escape(deal.url)}' style='display:inline-block;background:#1f6feb;color:#fff;"
            f"padding:10px 16px;border-radius:6px;text-decoration:none'>{html.escape(button_label(deal))}</a></p>"
            f"<p style='color:#888;font-size:12px'>{html.escape(deal.title)}</p></div>")
    return subject, text, body


class EmailChannel:
    name = "email"

    def __init__(self, cfg: Config):
        self.s = cfg.secrets

    def send(self, subject: str, text: str, html_body: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = self.s.smtp_to
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        if self.s.smtp_ssl or self.s.smtp_port == 465:
            server = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
            server.starttls()
        with server:
            if self.s.smtp_user and self.s.smtp_password:
                server.login(self.s.smtp_user, self.s.smtp_password)
            server.send_message(msg)


class TelegramChannel:
    name = "telegram"

    def __init__(self, cfg: Config):
        self.token = cfg.secrets.telegram_bot_token
        self.chat_id = cfg.secrets.telegram_chat_id

    def send(self, html_text: str, button: tuple[str, str] | None = None) -> None:
        payload = {"chat_id": self.chat_id, "text": html_text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        r = httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram {r.status_code}: {r.text[:200]}")


def telegram_text(deal: Deal, tz: str) -> str:
    if deal.indicative:
        lines = [f"🔎 <b>{html.escape(deal.card_name)}</b> - Cardmarket listing {deal.discount_pct:.0f}% below trend"]
    else:
        lines = [f"🎯 <b>{html.escape(deal.card_name)}</b> - {deal.discount_pct:.0f}% below market"]
    lines += [f"<b>{html.escape(k)}:</b> {html.escape(v)}" for k, v in deal_lines(deal, tz)]
    lines.append(f'\n<a href="{html.escape(deal.url)}">{html.escape(button_label(deal))}</a>')
    return "\n".join(lines)


class Notifier:
    def __init__(self, cfg: Config, settings: UserSettings):
        self.tz = cfg.timezone
        self.channels: list = []
        if cfg.secrets.email_configured and settings.notify_email:
            self.channels.append(EmailChannel(cfg))
        if cfg.secrets.telegram_configured and settings.notify_telegram:
            self.channels.append(TelegramChannel(cfg))

    def send_deal(self, deal: Deal) -> dict[str, str | None]:
        subject, text, body = format_deal(deal, self.tz)
        results: dict[str, str | None] = {}
        for ch in self.channels:
            try:
                if isinstance(ch, EmailChannel):
                    ch.send(subject, text, body)
                else:
                    ch.send(telegram_text(deal, self.tz), (button_label(deal), deal.url))
                results[ch.name] = None
            except Exception as exc:
                log.warning("%s alert failed: %s", ch.name, exc)
                results[ch.name] = str(exc)[:300]
        return results

    def send_test(self) -> dict[str, str | None]:
        results: dict[str, str | None] = {}
        for ch in self.channels:
            try:
                if isinstance(ch, EmailChannel):
                    ch.send("CardSniper test alert", "If you can read this, email alerts are working.")
                else:
                    ch.send("✅ <b>CardSniper</b> test alert - Telegram alerts are working.")
                results[ch.name] = None
            except Exception as exc:
                results[ch.name] = str(exc)[:300]
        return results
