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

# The retry budget is NOT parsed here any more. It lives in
# backend/queue_recovery_policy.parse_max_attempts, which production also
# uses. Round 13 caught this file still keeping its own copy in section 3
# while section 3b imported the shared one -- so 'one shared policy' was
# false in the same file that claimed it.

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
    # RAW FACT ONLY, round 14. "everything resumed or completed" is a conclusion this
    # line cannot support: under item-first recovery no paused batch does not imply no
    # deferred child, and section 3b could contradict it two lines later.
    print(INFO + "no batch currently has state=paused_source "
                 "(deferred items are judged in 3b)")
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

        # RAW FACTS ONLY. Round 13: this ran the deleted item/batch cooldown-EQUALITY
        # query and called it "the eligibility query the resume path itself runs,
        # verbatim" -- which stopped being true when item-first recovery landed, so a
        # benign timestamp difference was still reported as "cannot be seen by
        # auto-resume". Every conclusion now comes from section 3b, which asks the
        # shared policy. This section shows what is in the row and nothing more.
        deferred_here = con.execute(
            "SELECT COUNT(*) FROM download_queue_items WHERE batch_uuid = ? "
            "AND state IN ('verification_required','waiting_source')",
            (b["batch_uuid"],)).fetchone()[0]
        print(f"                deferred items: {deferred_here}"
              f"  (verdicts in section 3b)")

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
    from queue_recovery_state import (JOINED_DEFERRED_SQL, LABELS, classify_rows,
                                      needs_human)
    from backend.queue_recovery_policy import NEEDS_HUMAN
    rows = [dict(r) for r in con.execute(JOINED_DEFERRED_SQL)]
    verdicts = classify_rows(rows)
    if not rows:
        print(OK + "nothing is deferred")
    else:
        for verdict in sorted(verdicts):
            items = verdicts[verdict]
            marker = BAD if verdict in NEEDS_HUMAN else INFO
            print(marker + f"{len(items)} item(s): "
                  f"{LABELS.get(verdict, verdict)}")
            for it in items[:4]:
                print(f"        {str(it.get('title'))[:42]:42s} "
                      f"batch={str(it.get('batch_uuid'))[:8]} "
                      f"own_cooldown={str(it.get('cooldown_until'))[:19]}")
            if len(items) > 4:
                print(f"        ... and {len(items) - 4} more")
        stuck = needs_human(verdicts)
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
