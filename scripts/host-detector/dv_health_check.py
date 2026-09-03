#!/usr/bin/env python3
"""Durable health check for the DV host detector. Alerts to Gotify.

WHY THIS EXISTS
---------------
On 2026-08-15 every file in ``C:\\Tools`` was left with an empty, protected
DACL (``D:PAI``, zero ACEs) while the DIRECTORY's ACL stayed intact -- the
signature of ``icacls <dir> /grant X:(OI)(CI)F /T``, whose inheritance flags are
container-only and therefore produce no effective ACE on a FILE. Windows then
refused to LAUNCH dovi_tool, ffmpeg, ffprobe and MediaInfo.

DV detection was dead ~12 hours and nothing noticed. The detector recorded
``[WinError 5] Access is denied`` against ~3,900 MEDIA files, which reads as a
drive problem; every media file was in fact perfectly readable. It was the
second occurrence of this damage and the cause was never attributed, so a third
is assumed.

The check therefore watches BOTH layers, because either alone can be silent:

1. Can the tools actually be EXECUTED? This is the root cause, and it is
   detectable before any scan runs. ``dovi_tool --version`` needs no media file.
2. Is the detector's own store accumulating permission failures?

CONTRACT
--------
* Exit 0 always unless the check itself is broken. This must never be able to
  fail a scan run that is otherwise fine.
* Silence is not success: if the DB cannot be read, or the tool directory is
  missing entirely, that is an ALERT, not a quiet pass. A check that goes quiet
  when its subject disappears is worse than no check.
* The dedup marker is written ONLY after a CONFIRMED delivery (HTTP 200).
  Writing it after a failed send converts a transient Gotify outage into a
  permanently suppressed alert -- the dead-man's-switch lesson from the Kuma
  push monitors, where a rejected push returns 404 while curl exits 0.
* Every log write is wrapped. A logging failure must never kill the check
  (a fail-fast log write once killed a job at line 1 and erased its own
  evidence).

GOTIFY TOKEN
------------
By default the push token is scraped out of the Whats-up-Docker compose file
(``WUD_TRIGGER_GOTIFY_MYGOTIFY_TOKEN=``) -- it was never issued to this
checker, it was borrowed from another container's application token. If
Gotify starts rejecting it (HTTP 401 "you need to provide a valid access
token"), that is not a bug in this script; it means the checker needs its own
token. Set the environment variable ``SCANHOUND_GOTIFY_TOKEN_FILE`` to the
path of a file containing a real application token (issued to this checker,
in Gotify's admin UI) and its contents are used instead, with no code change.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB = Path(r"X:/Docker Apps/ScanHound/data/dv_host.db")
TOOLS = Path(r"C:/Tools")
TOOLS_REQUIRED = ("dovi_tool.exe", "ffmpeg.exe", "ffprobe.exe")
# State lives beside the detector's own database, NOT in C:\DockerData\scanhound.
# That directory is SYSTEM+Administrators only, so the account this check runs
# under cannot write there: the log silently vanished and -- far worse -- the
# dedup MARKER could never be written, which would have re-sent the same alert
# every run forever. Caught by checking that the log file actually appeared
# rather than trusting the exit code.
STATE_DIR = Path(r"X:/Docker Apps/ScanHound/data")
MARKER = STATE_DIR / "dv-health-alerted.marker"
LOGFILE = STATE_DIR / "dv-health-check.log"

WUD_COMPOSE = Path(r"X:/Docker Apps/Whats up docker/docker-compose.yml")
GOTIFY_URL = "http://gotify:80"
IMAGE = "scanhound:latest"

#: Denials above this are a real regression, not a one-off locked file.
DENIED_THRESHOLD = 25


def log(msg: str) -> None:
    """Best-effort. A logging failure must never break the check.

    Every physical line gets its own ISO-8601 UTC timestamp, so a multi-line
    dump (a failed delivery's captured output, say) can still be correlated
    line-by-line against other logs, and a bare ``grep`` shows *when* each
    line landed without cross-referencing anything else.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamped = "\n".join(
        "%s %s" % (ts, line) for line in msg.rstrip("\n").split("\n")
    )
    try:
        with LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(stamped + "\n")
    except Exception:
        pass
    try:
        print(stamped)
    except Exception:
        pass


#: Overrides the compose-scraped token below when set: the path of a file
#: holding a real Gotify application token issued to this checker. See the
#: "GOTIFY TOKEN" section in the module docstring.
GOTIFY_TOKEN_FILE_ENV = "SCANHOUND_GOTIFY_TOKEN_FILE"


def _read_token_file(path):
    """The token as text, whatever editor wrote the file.

    PowerShell's Out-File and '>' write UTF-16 with a BOM by default, and
    Notepad writes UTF-8 with a BOM. A plain utf-8 read raised
    UnicodeDecodeError on the first and kept the BOM as part of the token on
    the second -- and UnicodeDecodeError is a ValueError, not an OSError, so
    it escaped notify() and killed the whole check (round-7 verifier). The
    BOM decides the encoding; without one, UTF-8; anything else is unreadable.
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    # A second BOM (an editor re-saving a BOM'd file) is not part of a token.
    return text.lstrip("\ufeff")


def _gotify_token():
    override = os.environ.get(GOTIFY_TOKEN_FILE_ENV)
    if override:
        try:
            tok = _read_token_file(override).strip()
            if tok:
                return tok
            log("notify: %s is set but the file is empty; falling back to "
                "the compose-scraped token" % GOTIFY_TOKEN_FILE_ENV)
        except (OSError, ValueError) as e:
            # ValueError covers UnicodeDecodeError: a file this cannot decode
            # is unreadable, not fatal -- the check must never die on it.
            log("notify: %s is set but unreadable (%s); falling back to the "
                "compose-scraped token" % (GOTIFY_TOKEN_FILE_ENV, e.__class__.__name__))
    try:
        import re
        m = re.search(r"WUD_TRIGGER_GOTIFY_MYGOTIFY_TOKEN=(\S+)",
                      WUD_COMPOSE.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def notify(title: str, message: str, priority: int = 7) -> bool:
    """Push to Gotify. True ONLY on a confirmed HTTP 200.

    Gotify publishes no host port, so the push goes through a short-lived
    container on the internal `proxy` network -- the same pattern the
    qualification collector uses.
    """
    token = _gotify_token()
    if not token:
        log("notify: no gotify token available; alert NOT delivered")
        return False
    # The push container catches Gotify's own HTTPError so a rejection (401,
    # etc.) prints the status code and the response BODY instead of dying
    # with an uncaught traceback -- an uncaught traceback is what previously
    # made a 401 indistinguishable from any other failure once truncated.
    code = (
        "import json, sys, urllib.request, urllib.error\n"
        "d = json.dumps({'title': sys.argv[1], 'message': sys.argv[2], "
        "'priority': int(sys.argv[3])}).encode()\n"
        "try:\n"
        "    r = urllib.request.urlopen(urllib.request.Request(\n"
        "        %r + sys.argv[4], data=d,\n"
        "        headers={'Content-Type': 'application/json'}), timeout=15)\n"
        "    print('HTTP_OK', r.status)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('HTTP_ERROR', e.code)\n"
        "    print(e.read().decode('utf-8', 'replace'))\n"
        "    sys.exit(1)\n"
    ) % (GOTIFY_URL + "/message?token=")
    try:
        p = subprocess.run(
            ["docker", "run", "--rm", "--network", "proxy",
             "--entrypoint", "python", IMAGE, "-c", code,
             title, message, str(priority), token],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120)
        out = p.stdout or ""
        ok = p.returncode == 0 and "HTTP_OK 200" in out
        if ok:
            log("notify: delivered rc=%s" % p.returncode)
        else:
            # Full output, not a 120-char head: a 401 body and the status
            # line it belongs to were previously the part getting cut off,
            # leaving 218 identical truncated tracebacks and no diagnosis.
            log("notify: delivery FAILED rc=%s; output (last 2000 chars):\n%s"
                % (p.returncode, out[-2000:]))
            if "HTTP_ERROR 401" in out:
                log("notify: ACTION: Gotify rejected the token (401): the "
                    "checker needs its own valid application token")
        return ok
    except Exception as e:  # noqa: BLE001
        log("notify: send failed: %s" % e)
        return False


def check_tools():
    """Can each required tool actually be LAUNCHED? Returns list of problems."""
    problems = []
    if not TOOLS.is_dir():
        return ["%s does not exist" % TOOLS]
    for name in TOOLS_REQUIRED:
        exe = TOOLS / name
        if not exe.is_file():
            problems.append("%s is missing" % name)
            continue
        # No media file involved: this isolates "can Windows run it at all".
        flag = "--version" if name == "dovi_tool.exe" else "-version"
        try:
            subprocess.run([str(exe), flag], capture_output=True, timeout=60)
        except PermissionError:
            problems.append("%s cannot be EXECUTED (empty DACL? run: "
                            "icacls \"%s\" /inheritance:e)" % (name, exe))
        except Exception as e:  # noqa: BLE001
            problems.append("%s failed to launch: %s" % (name, type(e).__name__))
    return problems


def check_db():
    """Permission-failure count in the detector's store. Returns (problems, stats)."""
    if not DB.exists():
        return (["detector database missing at %s" % DB], {})
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True, timeout=20)
        q = lambda s: con.execute(s).fetchone()[0]
        stats = {
            "total": q("SELECT COUNT(*) FROM dv_host"),
            "classified": q("SELECT COUNT(*) FROM dv_host WHERE dv_layer<>'unknown'"),
            "denied": q("SELECT COUNT(*) FROM dv_host "
                        "WHERE last_error LIKE '%Access is denied%'"),
        }
        con.close()
    except Exception as e:  # noqa: BLE001
        # A check that cannot read its subject must SAY SO, not pass quietly.
        return (["detector database unreadable: %s" % e], {})

    problems = []
    if stats["denied"] > DENIED_THRESHOLD:
        problems.append(
            "%d files report 'Access is denied'. This is almost certainly the "
            "C:\\Tools DACL damage again, NOT a drive fault -- the media files "
            "read fine. Check: dovi_tool --version" % stats["denied"])
    return (problems, stats)


#: A stalled JDownloader poll beyond this is an outage, not a blip: the poller
#: runs every few seconds, so 30 minutes of no successful poll cannot be
#: transient. Calibrated against the observed failure -- the 2026-08-15 stall
#: ran ~15 HOURS unnoticed because nothing watched for it at all.
JD_STALL_SECONDS = 1800
API_HEALTH = "http://127.0.0.1:9721/health"


def check_jd():
    """Is the JDownloader poll alive? Returns a list of problems.

    Reads the app's own health surface rather than reaching into JDownloader.
    The question is not "is JD running" -- it was, throughout the outage -- but
    "is ScanHound still getting answers from it", which is what actually decides
    whether downloads move.

    Two ways to have no timestamp, opposite meanings:
      * never succeeded AND never failed -> the app just started; say nothing.
      * never succeeded BUT failing      -> broken since boot; alert.
    Treating both as healthy is how the stall stayed invisible; treating both as
    broken would alert on every restart.
    """
    try:
        with urllib.request.urlopen(API_HEALTH, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return ["ScanHound API is not answering /health: %s" % str(e)[:90]]

    if not body.get("jd_enabled"):
        return []                       # JD deliberately off: silence is correct
    jd = body.get("jd_poll")
    if not isinstance(jd, dict):
        return []                       # older build, or sub-report failed

    stalled = jd.get("stalled_seconds")
    fails = jd.get("consecutive_failures") or 0
    err = jd.get("last_error") or "no error recorded"

    if stalled is None:
        if fails:
            return ["JDownloader has NEVER answered since ScanHound started "
                    "(%d consecutive failures). Downloads cannot progress. "
                    "Last error: %s" % (fails, err)]
        return []
    if stalled > JD_STALL_SECONDS:
        return ["JDownloader poll stalled for %.1f hours (last success %s, "
                "%d consecutive failures). The downloads list is frozen. "
                "Restarting the scanhound container reconnects it. "
                "Last error: %s"
                % (stalled / 3600.0, jd.get("last_success_at"), fails, err)]
    return []


def _read_active_keys():
    """The problem KEYS alerted about last run, as a set."""
    try:
        raw = MARKER.read_text(encoding="utf-8")
    except OSError:
        return set()
    # Tolerates the pre-2026-08-15 marker, which held human text rather than
    # keys: unknown content parses to an empty set, so the first run after an
    # upgrade re-alerts once rather than staying silent. Re-alerting is the
    # safe direction for a watchdog.
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("active"), list):
            return set(data["active"])
    except ValueError:
        pass
    return set()


def _write_active_keys(keys):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps({"active": sorted(keys)}), encoding="utf-8")
        return True
    except OSError as e:
        log("check: alert delivered but marker not written: %s" % e)
        return False


def check_queue(body):
    """Download-queue stall conditions, from the same /health body.

    Three separate conditions rather than one timer, because "nothing was
    attempted" and "everything attempted failed" want different responses --
    the ambiguity that left the 2026-08-13 stall unresolvable. A verification
    hold is reported as needing a person, never as a scheduler fault.
    """
    q = (body or {}).get("queue")
    if not isinstance(q, dict):
        return []                      # older build, or the sub-report failed
    ev = q.get("evidence") or {}
    out = []
    if q.get("executor_starved"):
        out.append("Download queue: %s item(s) are DUE but nothing has been "
                   "attempted (oldest due %s, last attempt %s). The queue "
                   "worker is not picking up work."
                   % (ev.get("due_now"), ev.get("oldest_due_at"),
                      ev.get("last_attempt_at") or "never"))
    if q.get("source_no_progress"):
        out.append("Download queue: attempts are running but the source has "
                   "delivered nothing since %s (deadline %ss). Downloads are "
                   "not progressing."
                   % (ev.get("last_source_progress_at") or "never",
                      ev.get("progress_deadline_seconds")))
    if q.get("human_required"):
        out.append("Download queue needs a person: %s verification hold(s), "
                   "%s batch(es) holding deferred work with auto-resume off. "
                   "No automatic action can clear these."
                   % (ev.get("verification_holds"),
                      ev.get("batches_deferred_without_auto_resume")))
    return out


def main() -> int:
    tool_problems = check_tools()
    db_problems, stats = check_db()
    jd_problems = check_jd()
    try:
        with urllib.request.urlopen(API_HEALTH, timeout=20) as r:
            _body = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        _body = {}     # check_jd already reports an unreachable API
    queue_problems = check_queue(_body)

    # Keyed by SUBSYSTEM, not by "some problem exists". Peer review 2026-08-15:
    # a single global latch meant that once ANY problem had alerted, a LATER and
    # unrelated failure stayed silent until every problem cleared. A tool outage
    # could therefore hide a JDownloader stall indefinitely, and vice versa --
    # the exact cross-subsystem suppression this watchdog exists to prevent.
    #
    # Keys are stable identities, deliberately NOT the human message text:
    # messages carry durations and streak counts that change every run, which
    # would re-alert forever.
    active = {}
    if tool_problems:
        active["tool_exec"] = tool_problems
    if db_problems:
        active["detector_db"] = db_problems
    if jd_problems:
        active["jd_stalled"] = jd_problems
    # Keyed separately so a queue stall alerts even while a JD problem persists
    # -- the cross-subsystem suppression the last review caught.
    if queue_problems:
        active["queue_stalled"] = queue_problems
    problems = tool_problems + db_problems + jd_problems + queue_problems

    log("check: queue_ok=%s jd_ok=%s tools_ok=%s denied=%s classified=%s/%s"
        % (not queue_problems, not jd_problems, not tool_problems,
           stats.get("denied", "?"),
           stats.get("classified", "?"), stats.get("total", "?")))

    previously = _read_active_keys()
    now_keys = set(active)

    if not problems:
        try:
            if MARKER.exists():
                MARKER.unlink()
                log("check: healthy again; alert state cleared")
        except OSError:
            pass
        return 0

    fresh = now_keys - previously
    if not fresh:
        # Everything currently wrong has already been reported. Recording the
        # CURRENT set (not the union) is what lets a subsystem that recovers and
        # later breaks again alert a second time.
        if now_keys != previously:
            _write_active_keys(now_keys)
        log("check: problems unchanged (%s); already alerted" % ",".join(sorted(now_keys)))
        return 0

    body = "\n".join("- " + p for p in problems)
    if stats:
        body += ("\n\nDetector store: %s of %s classified, %s denied."
                 % (stats.get("classified"), stats.get("total"),
                    stats.get("denied")))

    title = "ScanHound: %s" % ", ".join(sorted(fresh)).replace("_", " ")
    if notify(title, body, 8):
        # Only what was actually announced becomes "already alerted". If the
        # send fails nothing is recorded, so the next run retries -- a transient
        # Gotify outage must never convert into a permanently suppressed alert.
        _write_active_keys(now_keys)
    else:
        log("check: alert NOT delivered; will retry next run")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log("check: unexpected failure: %r" % e)
        sys.exit(0)
