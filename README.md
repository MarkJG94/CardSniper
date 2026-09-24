# CardSniper

Watches **Cardmarket (UK sellers only)**, **eBay UK** and popular **UK MTG shops** for Magic: The Gathering
cards listed below market value, and sends you an **email and/or Telegram alert** with a link to the listing.
It runs on your own server and comes with a small web dashboard.

- **Every card**: a local database of every paper printing (~100k), refreshed daily from
  [Scryfall](https://scryfall.com). This includes each printing's Cardmarket trend price, which is used as "market value".
- **Exact matching**: specific printing/set (including borderless, showcase and extended art), foil vs non-foil,
  minimum condition, English only, graded cards excluded.
- **Deal rule**: the listing is at least *X%* below market (default 20%), set globally or per card.
- **Prices in GBP**: EUR prices are converted using the daily ECB rate. Postage is shown in every alert but
  not included in the discount.
- **Alerts arrive instantly** when a deal is found. After that, the same card only alerts again if the new deal is
  **cheaper** (inside a configurable window, 7 days by default).
- **Scan interval** is configurable (24h by default).

## How each source works

| Source | Method | Notes |
|---|---|---|
| Cardmarket | Loads each card's product page, filtered to *seller country = UK, English, min condition*, and reads the offers table. | Cardmarket has no public API for new users and uses Cloudflare bot protection. CardSniper requests pages with a real Chrome network fingerprint, and when a bot check appears it opens a real (headful) Chromium, passes the check, and reuses the clearance cookie. A home broadband IP is the best place to run this. It checks up to 400 product pages per run (configurable) and rotates through the rest over the following runs. |
| eBay UK | Official Browse API (free). One search per card name, limited to UK-located, ungraded listings under the deal price. Includes auctions. | Needs free API keys. Auctions only alert if they end within the alert window (24h by default). |
| UK shops (Total Cards, Axion Now, Manaleak, Magic Madhouse, Chaos Cards, Big Orbit, Mage Cards) | Shopify shops are read in full from their product feed. Other shops are searched card by card. | Edit the shop list in `config.yaml`. See the "Checking a source" section below. |

> ⚠️ **Honest caveats**
> - CardSniper was written in an environment that could not reach Cardmarket, eBay or the shops, so the
>   scrapers were built against realistic sample pages rather than the live sites. After installing, run
>   `cardsniper probe` for each source once (see the "Checking a source" section below). If a site's layout differs, the fix is usually a
>   CSS selector in `config.yaml`, or send the saved HTML page back to Claude.
> - Scraping Cardmarket is against its terms of service, and it may block you despite the precautions.
>   Keep the request rate low (the defaults are deliberately slow). This is for personal use only.
> - "Market" is the Cardmarket trend, which is an EU-wide price. UK sellers are often a bit above it, so a 20%
>   discount is a genuinely good deal.

## Install on Debian (Docker, recommended)

```bash
sudo apt install docker.io docker-compose-plugin git
git clone https://github.com/MarkJG94/CardSniper.git && cd CardSniper
cp config.example.yaml config.yaml
cp .env.example .env && chmod 600 .env
nano .env            # email / Telegram / eBay keys (see below)
docker compose up -d --build
docker compose logs -f
```

Open `http://<server-ip>:8080`. On first start it downloads the card database (about 1 minute),
then runs the first scan.

### Or without Docker (systemd)

```bash
sudo ./deploy/install-debian.sh      # installs to /opt/cardsniper and creates a systemd service
sudo nano /opt/cardsniper/.env
sudo systemctl start cardsniper && journalctl -u cardsniper -f
```

## Setting up alerts and eBay

**Telegram (free push notifications):**
1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `CARDSNIPER_TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `"chat":{"id": ...}` into `CARDSNIPER_TELEGRAM_CHAT_ID`.

**Email via Gmail:**
1. Turn on 2-step verification.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. Put the app password in `CARDSNIPER_SMTP_PASSWORD`, and your address in `CARDSNIPER_SMTP_USER`, `CARDSNIPER_SMTP_FROM` and `CARDSNIPER_SMTP_TO`.

**eBay:**
1. Sign up at <https://developer.ebay.com>.
2. Create a **Production** keyset.
3. Copy the App ID into `CARDSNIPER_EBAY_CLIENT_ID` and the Cert ID into `CARDSNIPER_EBAY_CLIENT_SECRET`.

The free tier allows 5,000 calls a day, which covers a daily scan of every card worth over about £5.

Test alerts with **Settings → Send a test alert**, or `cardsniper test-notify` (with Docker:
`docker compose exec cardsniper cardsniper test-notify`).

## Using the dashboard

- **Dashboard**: status of each source, the next scheduled scan, the latest deals, and a *Scan now* button.
- **Deals**: every listing that met your threshold, with price, postage, market value, discount and a
  **Buy** link. Deals marked "held" were not sent because you had already been alerted to a cheaper one.
- **Cards**: search the database, see every printing's GBP market price and price history, and add a card to your watchlist.
- **Watchlist**: per-card overrides, for either a specific printing or any printing. Each can set a finish, a minimum condition and
  its own discount %. Watched cards are always checked first, even when they're below the minimum value.
- **Run log**: what each scan checked, found and sent, with any errors.
- **Settings**: discount %, minimum card value, worst acceptable condition, foils, scan interval, auction window,
  re-alert window, which sources and channels are on, and *all cards* vs *watchlist only* mode.

## Checking a source (do this once after installing)

```bash
cardsniper probe cardmarket "Sheoldred, the Apocalypse"
cardsniper probe ebay "Sheoldred, the Apocalypse"
cardsniper probe "Magic Madhouse" "Sheoldred, the Apocalypse" --save-html /tmp/mm.html
```

The command prints every listing it found and what CardSniper decided about each one (deal, wrong condition,
couldn't identify the printing, and so on). With Docker, run it as
`docker compose exec cardsniper xvfb-run -a cardsniper probe ...` so the browser gets a virtual screen.

If a shop returns nothing, or returns the wrong prices, open the saved HTML and add `selectors` for that shop in `config.yaml`:

```yaml
    - name: Magic Madhouse
      url: https://magicmadhouse.co.uk
      platform: html
      search_url: "https://magicmadhouse.co.uk/search?q={query}"
      selectors: {item: ".product-item", title: ".product-name a", link: ".product-name a", price: ".price", stock: ".stock"}
```

## If Cardmarket blocks you

- Leave `headless: false`. The Docker image and systemd unit run the browser on a virtual screen.
- Lower `max_products_per_run` and/or raise the delays.
- Try Firefox-based Camoufox, which is often better at getting past bot checks:
  `pip install "cardsniper[camoufox]" && camoufox fetch`, then set `http: {browser: camoufox}`.
- The run log shows `Stopping after 5 failures in a row` when a site is refusing requests. The next scheduled
  run tries again.

## How a deal is decided

1. The listing is matched to a card printing. If the title doesn't identify the printing exactly, it is compared with the
   **cheapest** printing it could be, so a cheap reprint never looks like a discounted original.
2. The listing is rejected if it is graded, non-English, a proxy or a lot/playset, below your condition, or an auction ending too late.
3. Discount = (market − price) / market. It's a deal if the discount is ≥ your threshold (or the card's watch-rule threshold). Postage is shown
   separately.
4. An alert is sent unless you were already alerted to this card at the same or a lower price within the re-alert
   window.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && playwright install chromium
pytest
cardsniper serve --no-scheduler     # dashboard only
```

Code layout (all under `cardsniper/`):

- `cardsdb.py`: Scryfall import.
- `matching.py`: title → printing matcher.
- `deals.py`: the deal rule and alert de-duplication.
- `sources/`: Cardmarket, eBay and shops.
- `fetch.py`: HTTP requests with the browser fallback for bot checks.
- `scanner.py` / `scheduler.py`: running scans and scheduling them.
- `web/`: the dashboard.
