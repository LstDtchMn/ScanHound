#!/usr/bin/env python3
"""Probe a remote ScanHound deployment the way the Android APK will reach it.

Run this from a machine/network that is already *past* any edge access layer
(e.g. your own browser session / device that's authenticated through Cloudflare
Access), pointing at the public hostname. It checks the things a bundled,
non-same-origin client depends on:

  1. Reachability  - GET /health returns 200 (you're past the edge; the app is up)
  2. CORS          - the API allows the Tauri app origins (simple + preflight),
                     and does NOT allow an arbitrary origin (negative control)
  3. Auth          - (optional) the Bearer token satisfies the app's own auth
  4. WebSocket     - (optional) wss://host/ws?token=... upgrades and says hello

Only the standard library is needed for 1-3. The WebSocket check (4) uses the
`websockets` package if it's installed (it already is in ScanHound's
requirements); otherwise that one check is skipped.

Usage:
  python scripts/probe_remote.py https://scanhound.turtleland.us
  python scripts/probe_remote.py https://scanhound.turtleland.us --token YOURNONCE
  python scripts/probe_remote.py https://scanhound.turtleland.us --token YOURNONCE --ws

  # token may also come from the environment:
  SCANHOUND_AUTH_NONCE=... python scripts/probe_remote.py https://scanhound.turtleland.us --ws

Exit code is non-zero if any non-optional check fails, so it's CI/cron friendly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# The origins backend/api/main.py's CORSMiddleware is configured to allow.
TAURI_ORIGINS = [
    "tauri://localhost",        # Tauri custom protocol (Linux/macOS)
    "http://tauri.localhost",   # Tauri >=2.x default scheme (Windows + Android)
    "https://tauri.localhost",  # Tauri production webview (useHttpsScheme)
]
DISALLOWED_ORIGIN = "https://evil.example.com"  # negative control

PASS, FAIL, WARN, INFO = "[ PASS ]", "[ FAIL ]", "[ WARN ]", "[ INFO ]"

failures = 0


def mark(ok: bool, msg: str, *, optional: bool = False) -> None:
    """Print a PASS/FAIL/WARN line; count hard failures toward the exit code."""
    global failures
    if ok:
        print(f"{PASS} {msg}")
    elif optional:
        print(f"{WARN} {msg}")
    else:
        print(f"{FAIL} {msg}")
        failures += 1


def fetch(url: str, *, method: str = "GET", headers: dict | None = None):
    """Return (status, headers, body_bytes) for any method, even on 4xx/5xx."""
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:  # 4xx/5xx still carry headers + body
        return e.code, dict(e.headers), e.read()
    except urllib.error.URLError as e:
        return None, {}, str(e.reason).encode()


def header_ci(headers: dict, name: str) -> str | None:
    """Case-insensitive header lookup."""
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_reachability(base: str) -> bool:
    section("1. Reachability  (GET /health, no auth)")
    status, headers, body = fetch(f"{base}/health")
    if status is None:
        mark(False, f"could not connect: {body.decode(errors='replace')}")
        return False
    if status == 200:
        try:
            data = json.loads(body)
            mark(True, f"/health 200 OK  (version={data.get('version', '?')}, "
                       f"status={data.get('status', '?')})")
        except json.JSONDecodeError:
            mark(True, "/health 200 OK (non-JSON body)")
        return True
    if status in (401, 403):
        mark(False, f"/health returned {status} — an edge layer (Cloudflare "
                    f"Access / WAF?) is blocking before the request reaches "
                    f"ScanHound. /health is auth-exempt in the app, so a non-200 "
                    f"here means you're not past the edge.")
    else:
        mark(False, f"/health returned {status} (expected 200)")
    return False


def check_cors(base: str) -> None:
    section("2. CORS  (does the API allow the Tauri app origins?)")

    # 2a. Simple-request reflection: GET /health with an Origin header.
    for origin in TAURI_ORIGINS:
        _, headers, _ = fetch(f"{base}/health", headers={"Origin": origin})
        acao = header_ci(headers, "access-control-allow-origin")
        mark(acao in (origin, "*"),
             f"simple GET  Origin: {origin:<24} -> "
             f"Access-Control-Allow-Origin: {acao!r}")

    # 2b. Preflight: OPTIONS with the headers a real authed XHR would trigger.
    for origin in TAURI_ORIGINS:
        _, headers, _ = fetch(
            f"{base}/results",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        acao = header_ci(headers, "access-control-allow-origin")
        acam = header_ci(headers, "access-control-allow-methods")
        acah = header_ci(headers, "access-control-allow-headers")
        acac = header_ci(headers, "access-control-allow-credentials")
        ok = acao in (origin, "*")
        detail = f"allow-origin={acao!r}"
        if acam:
            detail += f", allow-methods={acam!r}"
        if acah:
            detail += f", allow-headers={acah!r}"
        if acac:
            detail += f", allow-credentials={acac!r}"
        mark(ok, f"preflight    Origin: {origin:<24} -> {detail}")

    # 2c. Negative control: an arbitrary origin must NOT be reflected.
    _, headers, _ = fetch(
        f"{base}/results",
        method="OPTIONS",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    acao = header_ci(headers, "access-control-allow-origin")
    mark(acao not in (DISALLOWED_ORIGIN, "*"),
         f"negative ctl Origin: {DISALLOWED_ORIGIN:<24} -> "
         f"Access-Control-Allow-Origin: {acao!r} (should be absent/blank)")


def check_auth(base: str, token: str | None) -> None:
    section("3. Auth  (does the Bearer token satisfy the app's own auth?)")
    if not token:
        print(f"{INFO} no --token / SCANHOUND_AUTH_NONCE given; skipping. "
              f"(If the server runs with no token auth, that's fine.)")
        return

    # Protected endpoint without a token should be 401 (proves you're past the
    # edge AND that auth is enforced). Skip this assertion if it's 403 (edge).
    status_no, _, _ = fetch(f"{base}/results/dismissed")
    if status_no == 403:
        mark(False, "/results/dismissed without a token returned 403 — still the "
                    "edge layer, not the app. Token can't be validated until "
                    "you're past it.", optional=True)
        return
    mark(status_no == 401,
         f"/results/dismissed without token -> {status_no} (expected 401)")

    status_ok, _, body = fetch(
        f"{base}/results/dismissed",
        headers={"Authorization": f"Bearer {token}"},
    )
    mark(status_ok == 200,
         f"/results/dismissed with Bearer token -> {status_ok} (expected 200)")


def check_ws(base: str, token: str | None) -> None:
    section("4. WebSocket  (wss://host/ws upgrade)")
    try:
        import asyncio
        import websockets  # type: ignore
    except ImportError:
        print(f"{INFO} `websockets` not installed; skipping. "
              f"(pip install websockets to enable this check.)")
        return

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    if token:
        ws_url += f"?token={token}"

    async def _probe() -> tuple[bool, str]:
        try:
            async with websockets.connect(ws_url, open_timeout=15) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                try:
                    data = json.loads(msg)
                    return data.get("type") == "connected", (
                        f"first message type={data.get('type')!r}")
                except json.JSONDecodeError:
                    return True, "connected (non-JSON first frame)"
        except Exception as e:  # noqa: BLE001 - report any failure verbatim
            return False, f"{type(e).__name__}: {e}"

    ok, detail = asyncio.run(_probe())
    mark(ok, f"{ws_url.split('?')[0]} -> {detail}", optional=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", help="e.g. https://scanhound.turtleland.us")
    parser.add_argument("--token", default=os.environ.get("SCANHOUND_AUTH_NONCE"),
                        help="Bearer token (or set SCANHOUND_AUTH_NONCE)")
    parser.add_argument("--ws", action="store_true",
                        help="also test the WebSocket upgrade")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    print(f"Probing {base}")

    reachable = check_reachability(base)
    check_cors(base)
    check_auth(base, args.token)
    if args.ws:
        check_ws(base, args.token)

    section("Summary")
    if not reachable:
        print(f"{INFO} /health wasn't reachable past the edge, so the CORS/auth "
              f"results above reflect the edge layer, not ScanHound itself. "
              f"Fix the access layer first (see docs/ANDROID_BUILD.md).")
    if failures:
        print(f"{FAIL} {failures} check(s) failed.")
        return 1
    print(f"{PASS} all required checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
