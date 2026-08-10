"""ScanHound DV host detector (HOST artifact — NOT in the Docker image).

Runs on TurtleLandSRVR (.170) where dovi_tool.exe reaches both local drives and
the .180 SMB media. Reads data/dv_host.json (NOT config.py), keeps its OWN
standalone dv_host.db (raw sqlite3 — it must NEVER open the container's crawler
database or construct its ORM layer, which runs DDL), reuses
dv_detect.detect_layer, optionally tags MKVs with mkvpropedit, then POSTs
/rename/dv-import so the container ingests it.

Usage (Task Scheduler action, with dovi_tool.exe's dir on PATH; run from the
repo root so the --config default resolves — --db and --api already default
to the shared data/dv_host.db and http://localhost:9721):
    python scripts\\host-detector\\dv_host_scan.py
"""
import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Make backend.rename.dv_detect importable when run from repo root.
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))
from backend.rename import dv_detect  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dv_host_scan")

DV_MTIME_TOL = 2.0  # >= FAT/exFAT 2s granularity — below this = endless rescans

# ── retry backoff for failed detections ────────────────────────────────
#
# WHY THIS EXISTS. A failed detection stores a NULL signature so the file is
# retried, which is correct on its own. Combined with a per-file time cap it
# was not: two titles that wedge dovi_tool were retried at the FRONT of every
# run (they sit in an otherwise fully-scanned root that os.walk reaches first),
# burning 1800 s each before the run ever reached the 236 files that had never
# been scanned at all. Measured 2026-08-09: three such runs in one day, one
# hour of every six-hour window, and because the run was then hard-killed at
# its Task Scheduler limit it never reached the dv-import POST either -- so
# the container's dv_scan gained nothing for two weeks while the host database
# kept growing.
#
# Escalating backoff means a permanently-failing file costs one attempt per
# week instead of one per run, without ever declaring it unscannable: it stays
# eligible forever, just not constantly.
DV_RETRY_BACKOFF_HOURS = (6, 24, 72, 168)


def retry_delay_hours(attempts, schedule=DV_RETRY_BACKOFF_HOURS):
    """Hours to wait before retrying a file that has failed *attempts* times."""
    if attempts <= 0:
        return 0
    return schedule[min(attempts, len(schedule)) - 1]


def is_retry_due(next_retry_at, now):
    """Whether a failed row is eligible again. A NULL due-time is always due.

    NULL is 'due' rather than 'never' on purpose: rows written before this
    column existed carry NULL, and the safe reading of a missing schedule is
    "we have no reason to hold this back", not "hold it back forever".
    """
    if next_retry_at is None:
        return True
    try:
        return float(next_retry_at) <= float(now)
    except (TypeError, ValueError):
        return True

# The API router mounts at bare /rename (no /api prefix) — see
# APIRouter(prefix="/rename", ...) in backend/api/routes/rename.py, included
# with no additional prefix in backend/api/main.py.
DV_IMPORT_PATH = "/rename/dv-import"

# The container's import endpoint (backend/api/routes/rename.py's
# _DEFAULT_DV_HOST_DB) reads /data/dv_host.db, bind-mounted from
# <repo-root>/data on the host (./data:/data in docker-compose.yml). Resolve
# that same file by walking up from this script's location
# (scripts/host-detector/dv_host_scan.py -> parents[2] == repo root) so the
# handoff works without an explicit --db.
DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "dv_host.db")

_TAG_NAMES = {
    "fel": "Dolby Vision Profile 7 FEL",
    "mel": "Dolby Vision Profile 7 MEL",
    "profile8": "Dolby Vision Profile 8",
    "profile5": "Dolby Vision Profile 5",
}


# ── pure helpers (unit-tested) ──────────────────────────────────────────
def load_host_config(path):
    """Read data/dv_host.json. Missing/invalid -> {} (caller no-ops)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def parse_roots(cfg):
    """Split dv_library_roots on ';' and newlines; trim; drop empties."""
    raw = cfg.get("dv_library_roots") or ""
    parts = re.split(r"[;\n]", raw)
    return [p.strip() for p in parts if p.strip()]


def should_run(cfg):
    """True only when detection is enabled AND at least one root is configured."""
    return bool(cfg.get("dv_detection")) and bool(parse_roots(cfg))


def sig_is_current(stored_mtime, stored_size, st_mtime, st_size,
                   tol=DV_MTIME_TOL):
    """Whether a stored signature still matches the file (skip re-scan).

    A NULL stored component never matches. Size must match exactly; mtime within
    *tol* (>=2.0s to absorb FAT/exFAT granularity)."""
    if stored_mtime is None or stored_size is None:
        return False
    try:
        return (abs(float(stored_mtime) - float(st_mtime)) <= tol
                and int(stored_size) == int(st_size))
    except (TypeError, ValueError):
        return False


def classify_to_row(path, layer, st, *, attempts=0, error=None, now=None):
    """Build a dv_host.db row. 'unknown' stores NULL mtime so the next run retries.

    A failed row additionally carries how many times it has failed and when it
    next becomes eligible, so "retry it" does not have to mean "retry it on
    every single run".
    """
    unknown = layer in ("unknown", None)
    if now is None:
        now = time.time()
    row = {
        "path": path,
        "dv_layer": layer,
        "sig_mtime": None if unknown else float(st.st_mtime),
        "sig_size": None if unknown else int(st.st_size),
        "attempts": 0,
        "last_error": None,
        "next_retry_at": None,
    }
    if unknown:
        row["attempts"] = int(attempts) + 1
        row["last_error"] = error
        row["next_retry_at"] = float(now) + retry_delay_hours(row["attempts"]) * 3600.0
    return row


def partition_work(candidates, now):
    """Order the run's work: never-scanned, then changed, then due retries.

    *candidates* are ``(path, st, row)`` with ``row`` None when the file has no
    database entry at all. Returns a single ordered list.

    WHY ORDER MATTERS, AND WHY 'NEWEST FIRST' IS THE LOAD-BEARING PART.
    os.walk yields directory order, which is roughly alphabetical and entirely
    unrelated to what deserves attention. A title acquired an hour ago sat
    behind hundreds of backlog entries purely because its name starts with a
    late letter. Sorting the unscanned bucket by mtime descending is what makes
    a fresh acquisition the FIRST thing a run looks at -- ordering the buckets
    alone would not have done it, because a new acquisition and a two-month-old
    backlog entry are both simply 'never scanned'.
    """
    never, changed, retries = [], [], []
    for path, st, row in candidates:
        if row is None:
            never.append((path, st, row))
            continue
        if row["dv_layer"] in ("unknown", None):
            if is_retry_due(row["next_retry_at"], now):
                retries.append((path, st, row))
            continue
        if not sig_is_current(row["sig_mtime"], row["sig_size"],
                              st.st_mtime, st.st_size):
            changed.append((path, st, row))
    never.sort(key=lambda c: c[1].st_mtime, reverse=True)
    changed.sort(key=lambda c: c[1].st_mtime, reverse=True)
    # Longest-waiting failure first, so backoff cannot starve one file forever.
    retries.sort(key=lambda c: (c[2]["next_retry_at"] or 0.0))
    return never + changed + retries


def tag_name_for(layer):
    """MKV track-name string for a layer, or None when no tag applies."""
    return _TAG_NAMES.get(layer)


# ── db (own standalone sqlite — not the container's ORM layer) ──────────
#: Columns added after the table shipped. ALTER TABLE ADD COLUMN is the only
#: migration this file may perform — it must never run the container's ORM DDL.
_ADDED_COLUMNS = (
    ("attempts", "INTEGER DEFAULT 0"),
    ("last_error", "TEXT"),
    ("next_retry_at", "REAL"),
)


def _open_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS dv_host (
            path TEXT PRIMARY KEY,
            dv_layer TEXT,
            sig_mtime REAL,
            sig_size INTEGER,
            title TEXT,
            scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    have = {r["name"] for r in conn.execute("PRAGMA table_info(dv_host)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE dv_host ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


def _load_rows(conn):
    """Every row, keyed by path — one query instead of one per walked file."""
    return {r["path"]: dict(r) for r in conn.execute(
        "SELECT path, dv_layer, sig_mtime, sig_size, attempts, next_retry_at "
        "FROM dv_host")}


def _upsert(conn, row):
    conn.execute('''
        INSERT INTO dv_host (path, dv_layer, sig_mtime, sig_size,
                             attempts, last_error, next_retry_at, scanned_at)
        VALUES (:path, :dv_layer, :sig_mtime, :sig_size,
                :attempts, :last_error, :next_retry_at, CURRENT_TIMESTAMP)
        ON CONFLICT(path) DO UPDATE SET
            dv_layer = excluded.dv_layer,
            sig_mtime = excluded.sig_mtime,
            sig_size = excluded.sig_size,
            attempts = excluded.attempts,
            last_error = excluded.last_error,
            next_retry_at = excluded.next_retry_at,
            scanned_at = CURRENT_TIMESTAMP
    ''', row)
    conn.commit()


def _tag_file(path, layer):
    """mkvpropedit track-name tag for MKV. Returns True on a successful write."""
    name = tag_name_for(layer)
    if not name or not path.lower().endswith(".mkv"):
        return False
    exe = shutil.which("mkvpropedit")
    if not exe:
        logger.warning("mkvpropedit not on PATH — skipping tag for %s", path)
        return False
    try:
        subprocess.run(
            [exe, path, "--edit", "track:v1", "--set", f"name={name}"],
            check=True, capture_output=True, timeout=300)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("mkvpropedit failed on %s: %s", path, e)
        return False


def _post_import(api_base):
    url = api_base.rstrip("/") + DV_IMPORT_PATH
    req = urllib.request.Request(url, data=b"{}",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            logger.info("dv-import -> %s", resp.read().decode("utf-8", "replace"))
        return True
    except OSError as e:
        logger.error("dv-import POST failed: %s", e)
        return False


def _iter_files(roots):
    exts = dv_detect._SUPPORTED_EXTS
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in exts:
                    yield os.path.join(dirpath, fn)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="data/dv_host.json")
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--api", default="http://localhost:9721")
    # 5h30m under a PT6H Task Scheduler limit. A HARD KILL at the limit loses
    # the file in flight AND skips the dv-import POST at the end of main(),
    # which is why the container's dv_scan gained nothing while the host
    # database grew: every run died before reaching the handoff. Stopping
    # ourselves BETWEEN files, with time to spare, converts that into a normal
    # exit 0 that always imports what it found.
    ap.add_argument("--max-runtime-minutes", type=float, default=330.0,
                    help="stop between files once this much wall clock is used "
                         "(0 disables the budget)")
    ap.add_argument("--mode", choices=("backfill", "steady"), default="backfill",
                    help="backfill: everything, ordered. steady: only "
                         "never-scanned and changed files, no retry sweep.")
    ap.add_argument("--import-every", type=int, default=25,
                    help="POST dv-import after this many files so a long run "
                         "publishes progress instead of only at the end")
    args = ap.parse_args(argv)

    cfg = load_host_config(args.config)
    if not should_run(cfg):
        logger.info("dv_detection off or no roots — nothing to do")
        return 0
    if not dv_detect.available():
        logger.error("dovi_tool not on PATH — aborting (nothing written)")
        return 1

    tagging = bool(cfg.get("dv_file_tagging"))
    conn = _open_db(args.db)
    started = time.monotonic()
    budget = float(args.max_runtime_minutes) * 60.0

    rows = _load_rows(conn)
    candidates = []
    for path in _iter_files(parse_roots(cfg)):
        try:
            st = os.stat(path)
        except OSError:
            continue
        candidates.append((path, st, rows.get(path)))

    now = time.time()
    work = partition_work(candidates, now)
    if args.mode == "steady":
        work = [c for c in work if c[2] is None
                or c[2]["dv_layer"] not in ("unknown", None)]
    logger.info("walked %d file(s); %d need work (mode=%s, budget=%.0f min)",
                len(candidates), len(work), args.mode, budget / 60.0)

    scanned = 0
    stopped_early = False
    for path, st, row in work:
        if budget > 0 and (time.monotonic() - started) >= budget:
            logger.info("time budget reached — stopping cleanly with %d file(s) "
                        "left for the next run", len(work) - scanned)
            stopped_early = True
            break
        # Log BEFORE the work, not after. The detector was silent for hours at a
        # time, which is what made a wedged run indistinguishable from a busy
        # one and led to throughput being inferred from process snapshots
        # instead of read from the database.
        logger.info("[%d/%d] scanning %.1f GB  %s",
                    scanned + 1, len(work), st.st_size / 1e9, path)
        t0 = time.monotonic()
        result = dv_detect.detect_layer(path)
        layer = result.get("layer")
        secs = max(time.monotonic() - t0, 1e-6)
        error = result.get("error")
        evidence = result.get("evidence") or error or "?"
        # NO RATE ON A FAILED DETECTION.
        #
        # detect_layer() returns unknown WITH an error rather than raising --
        # stall, timeout, read failure, parse failure. On a stall dovi_tool may
        # have read any fraction of the file, so size/elapsed is not a
        # measurement; it is a guess wearing a unit. It would be fabricated on
        # exactly the titles that already wedge: an 80 GB file stalling for
        # 1800 s printed "44 MB/s  FAILED". That is the error which caused the
        # throughput retraction, automated. (Round-1 blocker on the
        # live-progress branch; consolidation blocker 1.)
        if error:
            logger.info("[%d/%d] -> %s (%s) in %.0fs  rate unavailable%s",
                        scanned + 1, len(work), layer, evidence, secs,
                        "" if layer != "unknown" else "  FAILED")
        else:
            logger.info("[%d/%d] -> %s (%s) in %.0fs  %.0f MB/s effective scan rate",
                        scanned + 1, len(work), layer, evidence, secs,
                        (st.st_size / 1e6) / secs)
        attempts = (row or {}).get("attempts") or 0
        _upsert(conn, classify_to_row(path, layer, st, attempts=attempts,
                                      error=result.get("error"), now=time.time()))
        scanned += 1
        if tagging and _tag_file(path, layer):
            st2 = os.stat(path)  # header rewrite bumped mtime/size
            _upsert(conn, classify_to_row(path, layer, st2, attempts=attempts,
                                          error=result.get("error"),
                                          now=time.time()))
        if args.import_every > 0 and scanned % args.import_every == 0:
            _post_import(args.api)

    conn.close()
    logger.info("scanned %d file(s)%s; posting dv-import", scanned,
                " (stopped on budget)" if stopped_early else "")
    # The final import is the run's last chance to complete the handoff, so it
    # belongs in the exit status. Interim failures stay non-fatal: the host DB is
    # the durable producer and the import is cumulative, so the next successful
    # call carries whatever an earlier one missed. (Consolidation blocker 2.)
    ok = _post_import(args.api)
    if not ok:
        logger.error("final dv-import failed; host rows are durable but "
                     "container/Plex are stale")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
