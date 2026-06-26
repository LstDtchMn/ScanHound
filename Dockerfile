# syntax=docker/dockerfile:1

# ── Stage 1: build the Svelte frontend ──────────────────────────────────
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime with Chromium ───────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data \
    DISPLAY=:99 \
    SCANHOUND_FRONTEND_DIR=/app/frontend/build \
    CHROME_BIN=/usr/bin/chromium

# Chromium for HDEncode scraping + Xvfb (virtual display so
# undetected-chromedriver can run headful to clear Cloudflare) + the libraries
# Chromium needs + tini for clean signal handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver xvfb tini \
        fonts-liberation libnss3 libxss1 libasound2 libgbm1 libgtk-3-0 \
        ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-docker.txt ./
RUN pip install -r requirements-docker.txt

COPY backend/ ./backend/
COPY --from=frontend /app/frontend/build ./frontend/build
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh && mkdir -p /data

EXPOSE 9721
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
