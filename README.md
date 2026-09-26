# CardSniper

Watches **eBay UK**, popular **UK MTG shops** and **Cardmarket** for Magic: The Gathering cards listed
below market value, and sends you an **email and/or Telegram alert** with a link to the listing.
It runs on your own server and comes with a small web dashboard.

- **Every card**: a local database of every paper printing (~100k), refreshed daily from
  [Scryfall](https://scryfall.com), plus Cardmarket's official daily price guide (trend, lowest listing,
  1/7/30-day sale averages). The Cardmarket trend price is used as "market value".
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
| Cardmarket | Cardmarket's official **daily price guide download**. There's no page scraping, so Cloudflare doesn't come into it. | The file only has EU-wide figures, not individual listings or seller countries. A Cardmarket alert means *"the cheapest listing from any EU seller is at least X% below trend"* (30% by default). It links to that card's page filtered to **UK sellers, English and your minimum condition**, so you can see whether a UK copy is on offer. At most once a day. |
| eBay UK | Official Browse API (free). One search per card name, limited to UK-located, ungraded listings under the deal price. Includes auctions. | Needs free API keys. Auctions only alert if they end within the alert window (24h by default). |
| UK shops (Total Cards, Axion Now, Manaleak, Magic Madhouse, Chaos Cards, Big Orbit, Mage Cards) | Shopify shops are read in full from their product feed. Other shops are searched card by card. | Edit the shop list in `config.yaml`. See the "Checking a source" section below. |

> ⚠️ **Honest caveats**
> - CardSniper was written in an environment that could not reach Cardmarket, eBay or the shops, so the
>   integrations were built against realistic sample data rather than the live sites. After installing, run
>   `cardsniper probe` for each source once (see the "Checking a source" section below). If a shop's layout
>   differs, the fix is usually a CSS selector in `config.yaml`, or send the saved HTML page back to Claude.
> - Cardmarket alerts are **leads, not confirmed UK deals**. The cheapest EU listing might be from a German
>   seller, in poor condition or in another language. The alert link filters to what you want, but the UK
>   copy may cost more, or may not exist.
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
cardsniper probe cardmarket "Sheoldred, the Apocalypse"   # price guide figures for each printing
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

## Cardmarket alerts

- The price guide downloads with the daily card database refresh. Each scan also re-downloads it if it's more
  than 6 hours old.
- The dashboard shows when it was last downloaded. Each card's page shows Cardmarket's trend, lowest listing and
  sale averages.
- If the download fails (for example if Cardmarket moves the file), the run log says so and the last downloaded
  copy is used. The file's address is `price_guide_url` in `config.yaml`. The current one is listed on
  Cardmarket's downloads page.
- If a shop shows a bot check, CardSniper can still use a real browser for it (`http.browser` in `config.yaml`).

## How a deal is decided

1. The listing is matched to a card printing (Cardmarket signals already know it). If the title doesn't identify the printing exactly, it is compared with the
   **cheapest** printing it could be, so a cheap reprint never looks like a discounted original.
2. The listing is rejected if it is graded, non-English, a proxy or a lot/playset, below your condition, or an auction ending too late.
3. Discount = (market − price) / market. It's a deal if the discount is ≥ your threshold (or the card's watch-rule threshold). Postage is shown
   separately. Cardmarket signals use the separate Cardmarket threshold, because their price is the cheapest listing in any condition.
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
- `priceguide.py`: Cardmarket price guide download and import.
- `sources/`: Cardmarket (price guide signals), eBay and shops.
- `fetch.py`: HTTP requests with the browser fallback for bot checks (used for shops).
- `scanner.py` / `scheduler.py`: running scans and scheduling them.
- `web/`: the dashboard.
