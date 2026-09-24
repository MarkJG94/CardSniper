FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CARDSNIPER_DATA_DIR=/data \
    CARDSNIPER_CONFIG=/config/config.yaml \
    CARDSNIPER_ENV_FILE=/config/.env

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY cardsniper ./cardsniper
RUN pip install . && playwright install --with-deps chromium

VOLUME ["/data"]
EXPOSE 8080
# xvfb-run gives the browser a virtual screen so it can run headful (better at passing bot checks)
ENTRYPOINT ["tini", "--", "xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", "cardsniper"]
CMD ["serve"]
