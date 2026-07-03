#!/bin/sh
set -e

# Virtual display so undetected-chromedriver can run a headful Chromium
# (needed to clear Cloudflare on HDEncode). Safe to ignore if Xvfb is absent.
if command -v Xvfb >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
fi

# --no-auth only disables the desktop-sidecar nonce (SCANHOUND_AUTH_NONCE) —
# it does NOT disable the app's own password gate. The bearer-token
# middleware (backend/api/main.py) now fails CLOSED when neither a nonce nor
# a DB password exists (e.g. first boot, or a DB reset that wiped the
# auth_credentials row): protected routes 401 until a password is set via
# POST /auth/set-password (reachable pre-auth for exactly this bootstrap
# case). Reverse-proxy auth (Cloudflare Access / NPM) can still sit in front
# of this as defense-in-depth, but is no longer load-bearing for it.
#
# To intentionally run fully open (headless/dev only — no proxy auth either),
# set SCANHOUND_ALLOW_OPEN=1 in the container environment. Left unset
# (the default), the app is fail-closed as described above.
exec python -m backend.api --host 0.0.0.0 --port 9721 --no-auth
