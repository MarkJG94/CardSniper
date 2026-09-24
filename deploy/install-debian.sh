#!/usr/bin/env bash
# Installs CardSniper as a systemd service on Debian 12+ (run as root from the repo folder).
set -euo pipefail

APP=/opt/cardsniper
apt-get update
apt-get install -y python3 python3-venv python3-pip xvfb xauth rsync

id -u cardsniper >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin cardsniper
mkdir -p "$APP"
rsync -a --exclude .venv --exclude data --exclude .git ./ "$APP/"

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install "$APP"
PLAYWRIGHT_BROWSERS_PATH="$APP/ms-playwright" "$APP/.venv/bin/playwright" install --with-deps chromium

[ -f "$APP/config.yaml" ] || cp "$APP/config.example.yaml" "$APP/config.yaml"
[ -f "$APP/.env" ] || { cp "$APP/.env.example" "$APP/.env"; chmod 600 "$APP/.env"; }
chown -R cardsniper:cardsniper "$APP"

cp "$APP/deploy/cardsniper.service" /etc/systemd/system/cardsniper.service
systemctl daemon-reload
systemctl enable cardsniper

echo
echo "Installed. Next:"
echo "  1. edit $APP/.env (email, Telegram, eBay keys) and $APP/config.yaml"
echo "  2. systemctl start cardsniper && journalctl -u cardsniper -f"
echo "  3. open http://<server-ip>:8080"
