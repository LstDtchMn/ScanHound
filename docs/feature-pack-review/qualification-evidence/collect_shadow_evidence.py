#!/usr/bin/env python3
"""Daily RSS-shadow evidence collector for the 7-day qualification window.

Designed to run unattended from a Windows Scheduled Task. It reads the
production database READ-ONLY from a throwaway container -- it never execs
into the live ScanHound container and never writes to production.

    python "X:/Docker Apps/scanhound-qualification-evidence/collect_shadow_evidence.py"

What it does:
  1. Runs the qualification bundle's 05_shadow_evidence.py inside a fresh
     container with the scanhound_scanhound_db volume mounted read-only.
  2. Writes a timestamped JSON report into this evidence directory.
  3. Appends a one-line summary to shadow-window.log so the whole window can
     be read at a glance.
  4. Exits NON-ZERO if a mandatory stop condition is detected (any relevant
     RSS miss, or a database integrity failure), so the Scheduled Task shows
     as failed and the condition cannot pass unnoticed.

Auth is optional. The authoritative numbers are computed from the database,
so this keeps working even when the session token expires. If a file named
`auth-token.txt` exists beside this script, its contents are additionally
used to fetch the app's own GET /rss/status readiness and reconcile it
against the independent DB-derived computation.
"""
from __future__ import annotations
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
SCRIPTS = Path(r"X:/Docker Apps/ScanHound/docs/feature-pack-review/qualification/scripts")
TOKEN_FILE = EVIDENCE / "auth-token.txt"
# The evidence collector runs the app image via `docker run`, so 127.0.0.1
# inside that ephemeral container is ITS OWN loopback — nothing listens there.
# The scanhound container publishes no host port either (9721/tcp, unmapped), so
# there is no host address that would have worked; publishing one would not have
# fixed this. It must join the same Docker network and address the service by
# name — the pattern notify() below already uses for Gotify.
#
# Verified 2026-08-01: `docker run --entrypoint python scanhound:latest` against
# http://127.0.0.1:9721/health → URLError [Errno 111] Connection refused;
# with --network proxy against http://scanhound:9721/health → HTTP 200.
APP_NETWORK = "proxy"
BASE_URL = "http://scanhound:9721"
DB_VOLUME = "scanhound_scanhound_db"
IMAGE = "scanhound:latest"
LOG = EVIDENCE / "shadow-window.log"

# Gotify alerting. Gotify publishes no host port, so the push goes through a
# short-lived container on the internal `proxy` network (same pattern as the
# DB read above). The app token is read at runtime from the WUD compose file
# (an existing token under the admin account, which is the phone's account) --
# no additional copy of the secret is written anywhere.
WUD_COMPOSE = Path(r"X:/Docker Apps/Whats up docker/docker-compose.yml")
GOTIFY_URL = "http://gotify:80"


def _gotify_token():
    try:
        import re
        m = re.search(r"WUD_TRIGGER_GOTIFY_MYGOTIFY_TOKEN=(\S+)",
                      WUD_COMPOSE.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def notify(title, message, priority):
    """Send a Gotify push. Must never break collection -- best-effort only."""
    token = _gotify_token()
    if not token:
        log_line("notify: no gotify token available; skipping push")
        return
    code = (
        "import json,sys,urllib.request;"
        "d=json.dumps({'title':sys.argv[1],'message':sys.argv[2],"
        "'priority':int(sys.argv[3])}).encode();"
        f"r=urllib.request.urlopen(urllib.request.Request("
        f"'{GOTIFY_URL}/message?token='+sys.argv[4],data=d,"
        "headers={'Content-Type':'application/json'}),timeout=15);"
        "print(r.status)"
    )
    try:
        p = subprocess.run(
            ["docker", "run", "--rm", "--network", "proxy",
             "--entrypoint", "python", IMAGE, "-c", code,
             title, message, str(priority), token],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=60)
        ok = p.returncode == 0 and p.stdout.strip().endswith("200")
        log_line(f"notify: {'sent' if ok else 'FAILED: ' + p.stdout.strip()[:200]}")
    except Exception as e:  # never let alerting kill collection
        log_line(f"notify: FAILED: {e}")


def log_line(text):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp}  {text}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    cmd = [
        "docker", "run", "--rm",
        "--network", APP_NETWORK,
        "-v", f"{DB_VOLUME}:/dbvol:ro",
        "-v", f"{SCRIPTS}:/scripts:ro",
        "-v", f"{EVIDENCE}:/out",
        "--entrypoint", "python", IMAGE,
        "/scripts/05_shadow_evidence.py",
        "--db", "/dbvol/crawler.db",
        "--evidence-dir", "/out",
    ]
    # Reconcile against the app's own readiness report. Requested whenever a
    # token exists; once requested it is REQUIRED, not advisory — see below.
    reconciliation_requested = False
    if TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            cmd += ["--base-url", BASE_URL, "--token", token]
            reconciliation_requested = True

    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    if p.returncode != 0 and not p.stdout.strip().startswith("{"):
        log_line(f"COLLECT FAILED (exit {p.returncode}): {p.stdout.strip()[:400]}")
        notify("ScanHound QUAL: collection failed",
               f"Evidence collection failed (exit {p.returncode}). The window is "
               "not gathering data until this is fixed. See shadow-window.log.", 6)
        return 2

    try:
        summary = json.loads(p.stdout)
    except json.JSONDecodeError:
        log_line(f"COLLECT FAILED: unparseable output: {p.stdout.strip()[:400]}")
        notify("ScanHound QUAL: collection failed",
               "Evidence collector produced unparseable output. See "
               "shadow-window.log.", 6)
        return 2

    r = summary.get("readiness", {})
    integrity = summary.get("integrity_check")
    misses = r.get("relevant_misses", 0)
    cycles = r.get("successful_cycles", 0)
    days = r.get("observed_days", 0) or 0
    recovery = r.get("recovery_cycles", 0)
    reduction = r.get("request_reduction_pct", 0)
    feeds = r.get("normal_feeds_healthy")
    ready = r.get("ready")

    log_line(
        f"cycles={cycles}/20 days={days:.2f}/7 misses={misses} recovery={recovery} "
        f"reduction={reduction}% feeds_healthy={feeds} integrity={integrity} ready={ready}"
    )
    app = summary.get("app_readiness")
    log_line(
        "reconciliation: "
        + ("not requested (no auth token)" if not reconciliation_requested
           else f"error={app.get('error')}" if isinstance(app, dict) and app.get("error")
           else f"ready_matches={app.get('ready_matches')}" if isinstance(app, dict)
           else "no result")
    )

    # ── fail-closed reconciliation (design rev 2.1 §10) ──────────────────
    # The DB-derived readiness and the app's own /rss/status readiness are two
    # independent computations of the same thing. Once reconciliation is
    # requested, a missing or disagreeing cross-check must BLOCK the gate.
    #
    # It was previously advisory: `app_readiness` was recorded and then never
    # consulted, so `if ready:` could pass the gate on the DB computation alone.
    # Combined with the broken container networking above, that cross-check had
    # in fact NEVER succeeded — and nothing said so. An unverifiable pass is not
    # a pass.
    reconciliation_blockers = []
    if reconciliation_requested:
        if not isinstance(app, dict):
            reconciliation_blockers.append(
                "readiness reconciliation produced no result")
        elif app.get("error"):
            reconciliation_blockers.append(
                f"readiness reconciliation failed: {app['error']}")
        elif app.get("ready_matches") is False:
            reconciliation_blockers.append(
                "DB-derived readiness DISAGREES with the app's own /rss/status "
                "readiness — one of the two is wrong")
        elif app.get("ready_matches") is not True:
            reconciliation_blockers.append(
                "readiness reconciliation did not report a comparison")

    stop = []
    if misses:
        stop.append(f"RELEVANT RSS MISS x{misses}")
    if integrity != "ok":
        stop.append(f"DB INTEGRITY {integrity}")
    stop.extend(reconciliation_blockers)
    safety = summary.get("safety") or {}
    for violation in safety.get("violations") or []:
        stop.append(str(violation))
    if stop:
        log_line("!! MANDATORY STOP CONDITION: " + "; ".join(stop))
        log_line("!! Per the runbook: stop and roll back. Do not continue the window.")
        notify("ScanHound QUAL: STOP CONDITION",
               "Mandatory stop condition detected: " + "; ".join(stop)
               + ". Per the runbook: stop and roll back. Do not continue the window.",
               8)
        return 3

    if ready:
        log_line("** READINESS GATE PASSED -- window complete, ready for review. **")
        marker = EVIDENCE / "gate-passed.notified"
        if not marker.exists():
            notify("ScanHound QUAL: gate PASSED",
                   f"Readiness gate passed: {cycles} cycles over {days:.1f} days, "
                   f"0 misses, reduction {reduction}%. Window complete — evidence "
                   "ready for final review.", 5)
            marker.write_text(dt.datetime.now(dt.timezone.utc).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
