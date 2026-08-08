"""Watch for an auto-resume firing.

VERSION-CONTROLLED COPY. Added 2026-08-08.

This tool lived ONLY at /data/watch_resume.py -- inside the container's persistent volume,
under a directory git ignores. So it survived rebuilds but was never reviewable,
never diffable, and would have been lost with the volume. It also drifted out of
agreement with the code it inspects: it reported healthy batches as having
permanently exhausted their retry budget, which is how 45 stranded downloads got
diagnosed as a source throttle instead of a spent one-shot resume.

The RUNNING copy is still /data/watch_resume.py, because it must survive image rebuilds.
Deploy changes with:

    docker cp scripts/watch_resume.py scanhound:/data/watch_resume.py

Keep the two in step; if they diverge, this one is the source of truth.
"""
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

DB = "/dbvol/crawler.db"
GRACE = timedelta(minutes=12)   # scheduler tick slack past the cooldown
POLL = 120


def say(msg):
    print(msg, flush=True)


def snapshot():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        paused = list(con.execute(
            "SELECT batch_uuid, auto_resume_used u, cooldown_until FROM "
            "download_queue_batches WHERE state = 'paused_source'"))
        states = {r[0]: r[1] for r in con.execute(
            "SELECT state, COUNT(*) FROM download_queue_items GROUP BY 1")}
        return paused, states
    finally:
        con.close()


paused0, states0 = snapshot()
due_at = None
for b in paused0:
    try:
        t = datetime.fromisoformat(b["cooldown_until"])
        due_at = t if due_at is None or t > due_at else due_at
    except (TypeError, ValueError):
        pass
say(f"watching {len(paused0)} paused batch(es); "
    f"waiting_source={states0.get('waiting_source', 0)}; "
    f"retry due {due_at.isoformat() if due_at else 'unknown'}")

if not paused0:
    say("RESOLVED: nothing is paused. Nothing to watch.")
    sys.exit(0)

prev = (len(paused0), states0.get("waiting_source", 0),
        states0.get("failed", 0), sum(b["u"] or 0 for b in paused0))
reported_overdue = False

while True:
    time.sleep(POLL)
    try:
        paused, states = snapshot()
    except Exception as exc:                                   # noqa: BLE001
        say(f"poll failed ({type(exc).__name__}); will retry")
        continue

    waiting = states.get("waiting_source", 0)
    failed = states.get("failed", 0)
    used = sum(b["u"] or 0 for b in paused)
    cur = (len(paused), waiting, failed, used)
    now = datetime.now(timezone.utc)

    if cur != prev:
        say(f"CHANGE: paused={len(paused)} waiting_source={waiting} "
            f"failed={failed} retries_spent={used}")
        prev = cur

    if not paused:
        say(f"SUCCESS: no batches are paused. waiting_source={waiting}, "
            f"failed={failed}. The retry fired and the batches resumed.")
        sys.exit(0)

    if failed:
        say(f"ATTENTION: {failed} item(s) are now FAILED. The retry ran and hit "
            "something terminal -- check the reason codes.")
        sys.exit(0)

    if due_at and now > due_at + GRACE and not reported_overdue:
        spent = "yes" if used else "no"
        say(f"FAILURE: the retry time passed {int((now - due_at).total_seconds() // 60)} "
            f"min ago and {len(paused)} batch(es) are STILL paused "
            f"(one-shot retry spent: {spent}). The resume did not fire. This is "
            "the silent-skip failure mode -- check the timestamp match and the "
            "coordinator's blocked state.")
        reported_overdue = True
        sys.exit(0)
