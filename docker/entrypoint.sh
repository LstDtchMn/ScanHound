#!/bin/sh
set -e

# Virtual display so undetected-chromedriver can run a headful Chromium
# (needed to clear Cloudflare on HDEncode). Safe to ignore if Xvfb is absent.
if command -v Xvfb >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
fi

# --no-auth: the app has no built-in web login. Authentication MUST be provided
# by the reverse proxy in front of it (Cloudflare Access / Nginx Proxy Manager).
exec python -m backend.api --host 0.0.0.0 --port 9721 --no-auth
