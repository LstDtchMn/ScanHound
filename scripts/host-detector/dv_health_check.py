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
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
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
    """Best-effort. A logging failure must never break the check."""
    try:
        with LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(msg.rstrip() + "\n")
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _gotify_token():
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
            timeout=120)
        ok = p.returncode == 0 and "200" in (p.stdout or "")
        log("notify: delivered=%s rc=%s out=%s"
            % (ok, p.returncode, (p.stdout or "").strip()[:120]))
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


def main() -> int:
    tool_problems = check_tools()
    db_problems, stats = check_db()
    problems = tool_problems + db_problems

    log("check: tools_ok=%s denied=%s classified=%s/%s"
        % (not tool_problems, stats.get("denied", "?"),
           stats.get("classified", "?"), stats.get("total", "?")))

    if not problems:
        # Recovered: drop the marker so the NEXT failure alerts again.
        try:
            if MARKER.exists():
                MARKER.unlink()
                log("check: healthy again; dedup marker cleared")
        except OSError:
            pass
        return 0

    body = "\n".join("- " + p for p in problems)
    if stats:
        body += ("\n\nDetector store: %s of %s classified, %s denied."
                 % (stats.get("classified"), stats.get("total"),
                    stats.get("denied")))

    if MARKER.exists():
        log("check: PROBLEMS present but already alerted (marker exists):\n" + body)
        return 0

    if notify("ScanHound: DV detection is broken", body, 8):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            MARKER.write_text(body, encoding="utf-8")
        except OSError as e:
            log("check: alert delivered but marker not written: %s" % e)
    else:
        # Deliberately NOT writing the marker: an undelivered alert must be
        # retried next run, never suppressed.
        log("check: alert NOT delivered; will retry next run")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log("check: unexpected failure: %r" % e)
        sys.exit(0)
