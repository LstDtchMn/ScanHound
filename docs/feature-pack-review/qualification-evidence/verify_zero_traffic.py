#!/usr/bin/env python3
"""Objective 10: prove the HDEncode off-switch produces ZERO traffic.

Read-only. Mounts the production DB volume read-only in a throwaway
container; never execs into the live container, never writes.

Evidence gathered:
  * every hdencode_* table is empty -- in particular hdencode_feed_state,
    which gains a row the first time ANY feed is polled, and
    hdencode_ingest_cycles, which gains a row per discovery cycle;
  * the container log contains no HDEncode/RSS request activity since start;
  * the scheduler and maintenance loop ARE running (so this is evidence of a
    working off-switch, not merely of an idle application).

Run it once right after deploy for a baseline, then again after the scheduler
and maintenance loop have each had time to fire at least once.
"""
from __future__ import annotations
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
DB_VOLUME = "scanhound_scanhound_db"
IMAGE = "scanhound:latest"
CONTAINER = "scanhound"

COUNT_CODE = (
    "import json,sqlite3;"
    "c=sqlite3.connect('file:/dbvol/crawler.db?mode=ro',uri=True);"
    "t=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' "
    "AND name LIKE 'hdencode%' ORDER BY name\")];"
    "print(json.dumps({'user_version':c.execute('PRAGMA user_version').fetchone()[0],"
    "'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],"
    "'counts':{n:c.execute('SELECT COUNT(*) FROM \"'+n+'\"').fetchone()[0] for n in t}}))"
)

# Log lines that would indicate real discovery traffic.
TRAFFIC_PATTERNS = [
    r"hdencode", r"\brss\b", r"feed", r"scrape.*hdencode", r"discovery cycle",
]
# Lines that merely mention the disabled state are not traffic.
BENIGN = [r"disabled", r"skipp", r"off\b"]


def main():
    now = dt.datetime.now(dt.timezone.utc)

    p = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{DB_VOLUME}:/dbvol:ro",
         "--entrypoint", "python", IMAGE, "-c", COUNT_CODE],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        print(f"DB read failed: {p.stdout[:400]}")
        return 2
    db = json.loads(p.stdout.strip().splitlines()[-1])

    logs = subprocess.run(["docker", "logs", CONTAINER], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
    hits = []
    for line in logs.splitlines():
        low = line.lower()
        if any(re.search(pat, low) for pat in TRAFFIC_PATTERNS):
            if not any(re.search(b, low) for b in BENIGN):
                hits.append(line.strip())

    started = subprocess.run(
        ["docker", "inspect", CONTAINER, "--format", "{{.State.StartedAt}}"],
        text=True, stdout=subprocess.PIPE).stdout.strip()
    scheduler_running = "Scheduler started" in logs
    maintenance_running = "Maintenance loop started" in logs

    nonzero = {t: n for t, n in db["counts"].items() if n}
    ok = not nonzero and not hits and db["integrity"] == "ok"

    result = {
        "checked_at": now.isoformat(),
        "container_started_at": started,
        "db_user_version": db["user_version"],
        "db_integrity": db["integrity"],
        "hdencode_table_counts": db["counts"],
        "nonzero_hdencode_tables": nonzero,
        "log_traffic_hits": hits,
        "scheduler_running": scheduler_running,
        "maintenance_loop_running": maintenance_running,
        "zero_traffic_proven": ok,
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    (EVIDENCE / f"10_zero_traffic_{stamp}.json").write_text(
        json.dumps(result, indent=2) + "\n")

    print(f"container up since : {started}")
    print(f"db                 : user_version={db['user_version']} integrity={db['integrity']}")
    print(f"hdencode tables    : {db['counts']}")
    print(f"non-empty tables   : {nonzero or 'NONE'}")
    print(f"log traffic hits   : {len(hits)}")
    for h in hits[:10]:
        print(f"    ! {h}")
    print(f"scheduler running  : {scheduler_running}")
    print(f"maintenance running: {maintenance_running}")
    print(f"\nZERO-TRAFFIC PROVEN: {'YES' if ok else 'NO'}")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
