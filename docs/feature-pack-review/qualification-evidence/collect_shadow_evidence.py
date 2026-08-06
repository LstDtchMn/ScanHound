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
# The qualification window boundary, read from the same file the app uses
# so the collector and the app scope identically. Empty until a fresh
# window is deliberately started; empty means BLOCKED, never "count
# everything ever recorded".
WINDOW_FILE = EVIDENCE / "window-start-at.txt"

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


def miss_stop_conditions(graded, *, raw_misses):
    """Return the reasons the MISS record must stop qualification.

    Pure and importable, for the same reason reconciliation_blockers is: the
    previous version of this gate lived inline in main() and its only proof was
    a code read.

    WHY THIS IS NOT A LOOSENING. The old rule was `if raw_misses: stop`, which
    treats every miss as permanent coverage loss. It is not: a "miss" here means
    the listing had a release the RSS feed did not have YET. Jesse's tiered
    criterion (2026-07-24) already defines the classes -- <=6h GREEN, 6-24h
    YELLOW (acceptable, non-failing), >24h or never RED. This applies that
    criterion instead of ignoring it. Measured on 2026-08-05: 149 GREEN,
    0 YELLOW, 0 RED, 1 AMBIGUOUS out of 150, i.e. the raw rule stopped the
    window 150 times over 14.9 days for one unprovable case and zero real
    losses.

    What still stops the window:
      RED       a miss never provably resolved -- real coverage loss.
      PENDING   still missing, window too short to judge.
      AMBIGUOUS left listing_only but never OBSERVED in feed_only. Not evidence
                of failure, but not proof of success either, and an
                unverifiable pass is not a pass.
      grading unavailable -- fails CLOSED, exactly like a missing cross-check.

    YELLOW does NOT stop it. That is the whole point of the tier existing.
    """
    if graded is None:
        return [f"MISS GRADING UNAVAILABLE (raw misses={raw_misses}) - the "
                "tiered classification could not be computed, so no miss can "
                "be shown to be mere polling lag"]
    if not isinstance(graded, dict):
        return [f"MISS GRADING MALFORMED (raw misses={raw_misses})"]

    stop = []
    red = int(graded.get("red") or 0)
    pending = int(graded.get("pending") or 0)
    ambiguous = int(graded.get("ambiguous") or 0)
    if red:
        stop.append(f"RSS MISS NEVER RESOLVED x{red} (real coverage loss)")
    if pending:
        stop.append(f"RSS MISS STILL UNRESOLVED x{pending} (window too short)")
    if ambiguous:
        stop.append(f"RSS MISS UNPROVABLE x{ambiguous} (left the listing but "
                    "was never observed in the feed; the intersection stores "
                    "only a count)")
    return stop


def grade_misses():
    """Run the tiered miss grader and return its verdict dict, or None.

    None means "could not grade", and miss_stop_conditions treats that as a
    blocker. Deliberately returns None rather than an empty verdict: an absent
    grading must never read as "no bad misses".
    """
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{DB_VOLUME}:/dbvol:ro",
        "-v", f"{EVIDENCE}:/ev:ro",
        "--entrypoint", "python", IMAGE,
        "/ev/miss_resolution.py", "--json",
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=300,
                           text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        log_line(f"miss grading could not run: {e}")
        return None
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("JSON_VERDICT "):
            try:
                return json.loads(line[len("JSON_VERDICT "):])
            except json.JSONDecodeError:
                log_line("miss grading emitted unparseable JSON")
                return None
    log_line(f"miss grading produced no verdict (exit {p.returncode}): "
             f"{(p.stdout or '').strip()[:200]}")
    return None


def reconciliation_blockers(app_readiness, *, missing_credentials,
                            reconciliation=None,
                            token_name="auth-token.txt"):
    """Return the reasons the readiness cross-check must stop qualification.

    Pure and importable so it can be tested directly rather than inferred from
    the surrounding script — a gate whose only proof is a code read is the
    situation that produced this defect in the first place.

    The DB-derived readiness and the app's own /rss/status readiness are two
    INDEPENDENT computations of the same thing. A missing, failed or disagreeing
    cross-check blocks — unconditionally.

    Two separate ways this used to fail open, both caught in review:
      1. `app_readiness` was recorded and then never consulted, so `if ready:`
         passed the gate on the DB computation alone. Combined with the broken
         container networking, that cross-check had in fact NEVER succeeded —
         and nothing said so.
      2. The check was then made conditional on a token existing, so a missing
         auth-token.txt silently downgraded the run to DB-only.

    Independence is the entire value of the cross-check, so "we had no
    credentials" is not a lesser failure than "the check disagreed". An
    unverifiable pass is not a pass.

      3. THE THIRD WAY, and this one failed CLOSED for 14.9 days: the verdict
         was read out of the wrong dictionary. 05_shadow_evidence.py emits TWO
         separate keys -- `app_readiness` (the raw /rss/status payload: ready,
         reasons, successful_cycles, relevant_misses, ...) and `reconciliation`
         (the comparison: ready_matches, successful_cycles_delta,
         relevant_misses_delta). `ready_matches` only ever existed in the
         second. Reading it from the first therefore always yielded None, which
         `is not True`, so every single run reported "did not report a
         comparison" and tripped the mandatory stop -- while the real recorded
         value was `ready_matches: True` with both deltas at 0, i.e. the two
         independent computations agreed EXACTLY.

         Failing closed meant no false promotion, which is why it went
         unnoticed: the gate looked strict rather than broken. But a gate that
         can never pass is not a gate, it is a wall, and it produced an alert
         every six hours for two weeks.
    """
    if missing_credentials:
        return [f"NO AUTH TOKEN at {token_name} — the independent readiness "
                "cross-check could not run; qualification requires it"]
    if not isinstance(app_readiness, dict):
        return ["readiness reconciliation produced no result"]
    if app_readiness.get("error"):
        return [f"readiness reconciliation failed: {app_readiness['error']}"]
    # The COMPARISON lives in `reconciliation`, not in `app_readiness`. Reading
    # it from the payload being compared is what broke this.
    if not isinstance(reconciliation, dict):
        return ["readiness reconciliation did not report a comparison"]
    if reconciliation.get("ready_matches") is False:
        return ["DB-derived readiness DISAGREES with the app's own /rss/status "
                "readiness — one of the two is wrong"]
    if reconciliation.get("ready_matches") is not True:
        return ["readiness reconciliation did not report a comparison"]
    return []


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
    if WINDOW_FILE.is_file():
        window = WINDOW_FILE.read_text(encoding="utf-8").strip()
        if window:
            cmd += ["--window-start-at", window]
    # Reconcile against the app's own readiness report. This is a PREREQUISITE
    # of qualification, not an enhancement: the whole value of the cross-check
    # is that it is independent of the DB-derived computation, so "no
    # credentials" must fail closed exactly like "check failed".
    #
    # It was previously conditional on a token existing, which meant a missing
    # auth-token.txt silently downgraded the run to DB-only and could still
    # report ready=True. Caught in review.
    token = ""
    if TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if token:
        cmd += ["--base-url", BASE_URL, "--token", token]
    missing_credentials = not token

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
    recon = summary.get("reconciliation")
    log_line(
        "reconciliation: "
        + ("MISSING CREDENTIALS" if missing_credentials
           else f"error={app.get('error')}" if isinstance(app, dict) and app.get("error")
           else f"ready_matches={recon.get('ready_matches')} "
                f"cycles_delta={recon.get('successful_cycles_delta')} "
                f"misses_delta={recon.get('relevant_misses_delta')}"
           if isinstance(recon, dict)
           else "no comparison recorded")
    )

    # Fail-closed reconciliation (design rev 2.1 §10) — see the function's
    # docstring for the two ways this used to fail open.
    blockers = reconciliation_blockers(
        app, missing_credentials=missing_credentials, reconciliation=recon,
        token_name=TOKEN_FILE.name)

    graded = grade_misses()
    if isinstance(graded, dict):
        log_line(
            "miss grading: green={green} yellow={yellow} red={red} "
            "pending={pending} ambiguous={ambiguous} of {total} "
            "(green<={gh}h, yellow<={yh}h)".format(
                gh=graded.get("green_hours"), yh=graded.get("yellow_hours"),
                **{k: graded.get(k) for k in
                   ("green", "yellow", "red", "pending", "ambiguous", "total")})
        )

    stop = []
    stop.extend(miss_stop_conditions(graded, raw_misses=misses))
    if integrity != "ok":
        stop.append(f"DB INTEGRITY {integrity}")
    stop.extend(blockers)
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
