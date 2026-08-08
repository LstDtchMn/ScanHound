"""One-shot health check of the download queue.

VERSION-CONTROLLED COPY. Added 2026-08-08.

This tool lived ONLY at /data/scanhound_check.py -- inside the container's persistent volume,
under a directory git ignores. So it survived rebuilds but was never reviewable,
never diffable, and would have been lost with the volume. It also drifted out of
agreement with the code it inspects: it reported healthy batches as having
permanently exhausted their retry budget, which is how 45 stranded downloads got
diagnosed as a source throttle instead of a spent one-shot resume.

The RUNNING copy is still /data/scanhound_check.py, because it must survive image rebuilds.
Deploy changes with:

    docker cp scripts/scanhound_check.py scanhound:/data/scanhound_check.py

Keep the two in step; if they diverge, this one is the source of truth.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")
OK, BAD, INFO = "  [OK]   ", "  [!!]  ", "  [ ]   "
problems = []

now = datetime.now(timezone.utc)
print("=" * 68)
print(f"ScanHound check   {now.isoformat(timespec='seconds')}")
print("=" * 68)

# ---- 1. is the PR #46 code actually running? --------------------------------
print("\n1. Is the new cooldown code live?")
try:
    from backend.hdencode_coordinator import get_hdencode_coordinator
    coord = get_hdencode_coordinator()
    if hasattr(coord, "observe_reveal_stall"):
        print(OK + "escalating reveal cooldown IS deployed")
        tel = coord.reveal_telemetry()
        print(INFO + f"stalls={tel['stalls']} successes={tel['successes']} "
                     f"streak={tel['stall_streak']}")
        if tel["last_cooldown_seconds"]:
            print(INFO + f"last cooldown {tel['last_cooldown_seconds'] // 60} min "
                         f"(step x{tel['last_escalation_step']})")
    else:
        print(BAD + "NOT deployed - this container predates PR #46")
        problems.append("PR #46 is not deployed yet")
    snap = coord.snapshot()
    print(INFO + f"coordinator blocked={snap.get('blocked')} "
                 f"enabled={snap.get('enabled')}")
except Exception as exc:                                   # noqa: BLE001
    print(BAD + f"could not load the coordinator: {type(exc).__name__}: {exc}")
    problems.append("coordinator did not load")

# The retry budget, read from the SAME config key production reads, and clamped the
# same way (backend.download_queue._auto_resume_max_attempts). Hardcoding a second
# copy of "3" here is how this tool came to disagree with the code in the first
# place: it reported healthy batches as permanently exhausted.
_MAX_AUTO_RESUME = 3
try:
    from backend.config import CONFIG_FILE as _CF
    with open(_CF, encoding="utf-8") as _fh:
        _MAX_AUTO_RESUME = max(1, min(10, int(json.load(_fh).get(
            "download_queue_auto_resume_max_attempts", 3))))
except Exception:                                          # noqa: BLE001
    pass

# ---- 2. did the auto-resume setting survive the restart? --------------------
print("\n2. Auto-resume setting (governs FilterBar + SwipeDeck grabs)")
KEY = "download_queue_auto_resume_after_cooldown"
try:
    from backend.config import CONFIG_FILE
    with open(CONFIG_FILE, encoding="utf-8") as fh:
        value = json.load(fh).get(KEY)
    if value is True:
        print(OK + f"{KEY} = true")
    else:
        print(BAD + f"{KEY} = {value!r} - it was REVERTED")
        print(INFO + "cause: saving any setting from the UI rewrites this file")
        print(INFO + f"backup of the good value: {CONFIG_FILE}.bak-autoresume")
        problems.append("the auto-resume setting was reverted")
except Exception as exc:                                   # noqa: BLE001
    print(BAD + f"could not read the config: {type(exc).__name__}: {exc}")

# ---- 3. the 69 stranded grabs ----------------------------------------------
print("\n3. The paused batches and the grabs inside them")
DB = "/dbvol/crawler.db"
if not os.path.exists(DB):
    print(BAD + f"{DB} not found")
    raise SystemExit(1)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

states = {r[0]: r[1] for r in con.execute(
    "SELECT state, COUNT(*) FROM download_queue_items GROUP BY 1")}
print(INFO + "queue items: " + ", ".join(
    f"{k}={v}" for k, v in sorted(states.items(), key=lambda kv: -kv[1])))

paused = list(con.execute(
    "SELECT batch_uuid, state, auto_resume_after_cooldown a, auto_resume_used u, "
    "       cooldown_until FROM download_queue_batches "
    "WHERE state = 'paused_source' ORDER BY batch_uuid"))
if not paused:
    print(OK + "NO batches are paused - everything resumed or completed")
else:
    print(INFO + f"{len(paused)} batch(es) still paused:")
    for b in paused:
        due = b["cooldown_until"]
        overdue = ""
        if due:
            try:
                overdue = (" DUE (cooldown passed)"
                           if datetime.fromisoformat(due) <= now
                           else " not due yet")
            except ValueError:
                overdue = " (unparseable cooldown)"
        print(f"        {b['batch_uuid'][:8]}  resume={b['a']} used={b['u']}"
              f"  due={str(due)[:19]}{overdue}")

        # The eligibility query the resume path itself runs, verbatim.
        hit = con.execute(
            "SELECT source FROM download_queue_items WHERE batch_uuid = ? "
            "  AND state IN ('verification_required','waiting_source') "
            "  AND cooldown_until = ? "
            "  AND queue_reason IN ('interactive_challenge','source_deferred') "
            "  AND COALESCE(last_reason_code,'') NOT IN "
            "      ('operation_timeout_unknown','interrupted_unknown_outcome') "
            "LIMIT 1", (b["batch_uuid"], b["cooldown_until"])).fetchone()
        if hit:
            print("                will be seen by auto-resume: YES")
        else:
            print("                will be seen by auto-resume: NO - it will be"
                  " SKIPPED SILENTLY")
            problems.append(f"batch {b['batch_uuid'][:8]} cannot be seen by "
                            "auto-resume (timestamp mismatch)")
        if not b["a"]:
            problems.append(f"batch {b['batch_uuid'][:8]} has auto-resume off")
        # BUDGET IS 3 AND REFUNDABLE, corrected 2026-08-08.
        #
        # This used to flag ANY nonzero auto_resume_used as "already spent its one
        # automatic retry". That was true of the deployed code at the time -- one
        # automatic resume per batch for its whole lifetime -- and it is why 45
        # downloads sat stranded for seven hours: nothing could re-drive them.
        #
        # The retry budget is now 3 by default and is REFUNDED when a resume makes
        # real source progress, so `used=1` or `used=2` is an ordinary healthy state,
        # not a dead end. Reporting it as exhausted sent me looking for a throttle
        # that was not there. Only a batch at or over the limit is actually stuck.
        used = int(b["u"] or 0)
        if used >= _MAX_AUTO_RESUME:
            problems.append(
                f"batch {b['batch_uuid'][:8]} has used all {_MAX_AUTO_RESUME} "
                "automatic retries and will not self-resume again")
        elif used:
            print(f"                automatic retries used: {used} of "
                  f"{_MAX_AUTO_RESUME} (refunded on real source progress)")

# ---- 3b. can every deferred download still recover? -------------------------
#
# REWRITTEN 2026-08-08 on peer review round 12.
#
# This used to call every deferred item whose batch was not `paused_source` an
# orphan. That was TRUE when recovery could only be reached through a paused batch --
# and the item-first fix deliberately removed that requirement, so the same check
# began reporting healthy, recovering downloads as permanently stranded. I fixed a
# false SUCCESS in round 10 and replaced it with a false FAILURE in round 11.
#
# It now asks the shared classifier, which holds the recovery policy ONCE for both
# this tool and watch_resume.py. Only ORPHANED needs a human; the waiting verdicts are
# ordinary and clear on their own.
print("\n3b. Can every deferred download still recover?")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from queue_recovery_state import (JOINED_DEFERRED_SQL, NEEDS_HUMAN,
                                      classify_all, max_auto_resume_attempts)
    rows = [dict(r) for r in con.execute(JOINED_DEFERRED_SQL)]
    verdicts = classify_all(rows, max_attempts=max_auto_resume_attempts())
    if not rows:
        print(OK + "nothing is deferred")
    else:
        for verdict in sorted(verdicts):
            items = verdicts[verdict]
            marker = BAD if verdict in NEEDS_HUMAN else INFO
            print(marker + f"{len(items)} item(s): {verdict}")
            for it in items[:4]:
                print(f"        {str(it.get('title'))[:42]:42s} "
                      f"batch={str(it.get('batch_uuid'))[:8]} "
                      f"own_cooldown={str(it.get('cooldown_until'))[:19]}")
            if len(items) > 4:
                print(f"        ... and {len(items) - 4} more")
        stuck = sum(len(verdicts.get(v, [])) for v in NEEDS_HUMAN)
        if stuck:
            problems.append(f"{stuck} deferred item(s) have no automatic recovery "
                            "path and need an explicit resume")
        else:
            print(OK + "every deferred item has a recovery path")
except Exception as exc:                                   # noqa: BLE001
    print(BAD + f"recovery classifier unavailable: {type(exc).__name__}: {exc}")
    problems.append("could not classify deferred items")

failed = list(con.execute(
    "SELECT last_reason_code, COUNT(*) n FROM download_queue_items "
    "WHERE state = 'failed' GROUP BY 1 ORDER BY 2 DESC"))
print("\n4. Anything newly failed?")
if not failed:
    print(OK + "no failed items at all")
else:
    for r in failed:
        print(INFO + f"{str(r['last_reason_code']):30s} {r['n']}")
    if any(r["last_reason_code"] == "layout_changed" for r in failed):
        problems.append("layout_changed failures are back - check whether they "
                        "are real layout changes or a new throttle shape")

print("\n" + "=" * 68)
if problems:
    print(f"{len(problems)} thing(s) need attention:")
    for p in problems:
        print(f"  - {p}")
else:
    print("Everything checks out.")
print("=" * 68)
