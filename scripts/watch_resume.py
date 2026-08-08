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


def _recovery_state():
    """(needs_human, still_deferred) per the SHARED policy, or (-1, -1) if unavailable.

    ROUND 13 CAUGHT THE FALSE SUCCESS AGAIN, third variant. This returned only the
    human-required count, and the caller read "nothing needs a human" as "nothing is
    deferred" -- so a row merely waiting for its own cooldown made the watcher print
    RESOLVED and exit 0, while the very recovery event it was watching for had not
    happened. Round 10 had it calling the orphan state SUCCESS; round 12 caught my fix
    inverting that to a false FAILURE; this is the third shape of the same mistake.

    So both numbers are returned and the caller must distinguish:
        needs_human > 0   -> ACTION REQUIRED
        still_deferred > 0 -> WAITING, keep polling
        both zero          -> RESOLVED
    """
    try:
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from queue_recovery_state import (JOINED_DEFERRED_SQL, classify_rows,
                                          needs_human, still_deferred)
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(JOINED_DEFERRED_SQL)]
        finally:
            con.close()
        verdicts = classify_rows(rows)
        return needs_human(verdicts), still_deferred(verdicts)
    except Exception:                                          # noqa: BLE001
        # Unknown is not zero. Reporting an unknown as all-clear is the original bug.
        return -1, -1


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
    # "NO PAUSED BATCHES" IS NOT RECOVERY. Corrected 2026-08-08 on peer review
    # round 10, which caught this exact defect after I fixed its twin in
    # scanhound_check.py and shipped this one unchanged in the same PR.
    #
    # Recovery used to be discoverable only through a paused batch, so a batch that
    # moved on while its children stayed deferred left those children unreachable.
    # That is not a hypothetical: 34 downloads sat in it for seven hours. A watcher
    # whose success condition is "no paused batches" certifies exactly that state as
    # a win -- and the old message even printed the stranded count next to the word
    # SUCCESS.
    # ASK THE SHARED CLASSIFIER, do not re-derive the rule here.
    #
    # Round 10 caught this script calling the orphan state SUCCESS. My fix then
    # called every deferred-item-without-a-paused-batch an ORPHAN -- which round 12
    # caught as the opposite error, because item-first recovery deliberately made
    # that state recoverable. Two wrong answers from the same cause: a second copy
    # of the policy. The copy is gone; queue_recovery_state owns it.
    orphaned, waiting = _recovery_state()
    if orphaned < 0:
        say("UNKNOWN: the recovery classifier could not run, so I cannot say "
            "whether anything is stranded. Treat this as needing a look, not as "
            "all-clear -- reporting an unknown as zero is the false-SUCCESS this "
            "script was fixed for.")
        sys.exit(1)
    if orphaned:
        say(f"ACTION REQUIRED: {orphaned} item(s) have no automatic recovery path. "
            "Run `python /data/scanhound_check.py` (section 3b) for the verdicts, "
            "then resume explicitly -- this will not clear on its own.")
        sys.exit(1)
    if waiting:
        # WAITING IS NOT RESOLVED. Round 13: reading "nothing needs a human" as
        # "nothing is deferred" made this exit 0 while the recovery event being
        # watched had not happened -- the third shape of this script's false success.
        say(f"WAITING: {waiting} item(s) are deferred but all have a recovery path; "
            "continuing to watch.")
    else:
        say("RESOLVED: nothing is paused and nothing is deferred.")
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
        stuck, deferred = _recovery_state()
        if stuck < 0:
            say("UNKNOWN: the recovery classifier could not run; not calling this "
                "a success.")
            sys.exit(1)
        if stuck:
            say(f"ACTION REQUIRED: {stuck} item(s) have no automatic recovery path "
                f"deferred (waiting_source={waiting}). The batch moved on without "
                "them, so no automatic path can reach them. This is a FAILURE, not "
                "a success -- the old version of this script called it SUCCESS "
                "while printing this very number.")
            sys.exit(1)
        say(f"SUCCESS: no batches are paused and nothing is deferred "
            f"(failed={failed}). The retry fired and the batches resumed.")
        sys.exit(0)

    if failed:
        say(f"ATTENTION: {failed} item(s) are now FAILED. The retry ran and hit "
            "something terminal -- check the reason codes.")
        sys.exit(0)

    if due_at and now > due_at + GRACE and not reported_overdue:
        # NOT "one-shot" any more. The budget is configurable
        # (download_queue_auto_resume_max_attempts, default 3) and is REFUNDED when a
        # resume makes real source progress, so a nonzero count is ordinary. Calling
        # it a spent one-shot is what made scanhound_check.py report 45 recoverable
        # downloads as permanently dead.
        say(f"FAILURE: the retry time passed {int((now - due_at).total_seconds() // 60)} "
            f"min ago and {len(paused)} batch(es) are STILL paused "
            f"(automatic retries used: {used}). The resume did not fire. Check the "
            "coordinator's blocked state and the per-item queue_reason -- an "
            "unknown-outcome item is held on purpose and will not auto-retry.")
        reported_overdue = True
        sys.exit(0)
