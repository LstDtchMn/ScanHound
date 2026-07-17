#!/bin/sh
set -e

# Virtual display so undetected-chromedriver can run a headful Chromium
# (needed to clear Cloudflare on HDEncode). SUPERVISED — a crashed Xvfb
# otherwise leaves DISPLAY :99 dead and silently breaks ALL scraping: every
# grab then fails at "session not created: chrome not reachable", so no links
# are extracted and nothing is ever handed to JDownloader, until the container
# is restarted. And a restart alone doesn't even recover it, because the stale
# /tmp/.X99-lock survives `docker restart` (same writable layer) and makes the
# next Xvfb abort with "Server is already active for display 99". So: clear the
# stale lock/socket on every (re)start, respawn Xvfb if it ever exits, and keep
# its stderr (not /dev/null) so a future crash is visible in `docker logs`.
if command -v Xvfb >/dev/null 2>&1; then
  ( while :; do
      rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
      echo "[entrypoint] starting Xvfb on :99" >&2
      Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp || true
      echo "[entrypoint] Xvfb exited — cleared lock, restarting in 2s" >&2
      sleep 2
    done ) &
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
