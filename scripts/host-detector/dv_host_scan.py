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


def classify_to_row(path, layer, st):
    """Build a dv_host.db row. 'unknown' stores NULL mtime so the next run retries."""
    unknown = layer in ("unknown", None)
    return {
        "path": path,
        "dv_layer": layer,
        "sig_mtime": None if unknown else float(st.st_mtime),
        "sig_size": None if unknown else int(st.st_size),
    }


def tag_name_for(layer):
    """MKV track-name string for a layer, or None when no tag applies."""
    return _TAG_NAMES.get(layer)


# ── db (own standalone sqlite — not the container's ORM layer) ──────────
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
    conn.commit()
    return conn


def _get_sig(conn, path):
    row = conn.execute(
        "SELECT sig_mtime, sig_size FROM dv_host WHERE path = ?", (path,)).fetchone()
    return (row["sig_mtime"], row["sig_size"]) if row else (None, None)


def _upsert(conn, row):
    conn.execute('''
        INSERT INTO dv_host (path, dv_layer, sig_mtime, sig_size, scanned_at)
        VALUES (:path, :dv_layer, :sig_mtime, :sig_size, CURRENT_TIMESTAMP)
        ON CONFLICT(path) DO UPDATE SET
            dv_layer = excluded.dv_layer,
            sig_mtime = excluded.sig_mtime,
            sig_size = excluded.sig_size,
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


def _post_import(api_base, label="dv-import"):
    """POST the import trigger. Returns True on success, False on failure.

    Returning a status matters: this used to swallow the outcome, so a scan
    could finish, report success, and leave the container with none of its
    results -- the failure mode being fixed here, one layer up.

    A missed import is self-healing and never loses data: the endpoint re-reads
    the WHOLE host store and upserts every row, so the next successful call
    carries anything an earlier one missed.
    """
    url = api_base.rstrip("/") + DV_IMPORT_PATH
    req = urllib.request.Request(url, data=b"{}",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            logger.info("%s -> %s", label,
                        resp.read().decode("utf-8", "replace"))
        return True
    except OSError as e:
        logger.error("%s POST failed: %s", label, e)
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
    # Hand results off DURING the walk, not only at the end. 0 disables.
    ap.add_argument("--import-every", type=int, default=10, metavar="N",
                    help="POST dv-import after every N newly scanned files "
                         "(0 = only at the end)")
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
    scanned = 0
    imported_at = 0   # value of `scanned` when the last interim import fired
    for path in _iter_files(parse_roots(cfg)):
        try:
            st = os.stat(path)
        except OSError:
            continue
        stored_m, stored_s = _get_sig(conn, path)
        if sig_is_current(stored_m, stored_s, st.st_mtime, st.st_size):
            continue
        # Announce the file BEFORE reading it, not after. detect_layer streams
        # the whole title over SMB -- 9 minutes for a 57 GB file at the measured
        # ~100 MB/s, and up to the 30-minute _EXTRACT_TIMEOUT. A line emitted
        # only on completion would leave exactly the silence this is meant to
        # remove: on 2026-08-09 a five-hour run showed nothing, and the rate got
        # inferred from process snapshots instead -- 12x wrong, and the review
        # built on it had to be retracted.
        gb = st.st_size / 1e9
        logger.info("[%d] scanning %s (%.1f GB)",
                    scanned + 1, os.path.basename(path), gb)
        t0 = time.monotonic()
        result = dv_detect.detect_layer(path)
        dt = time.monotonic() - t0
        layer = result.get("layer")
        error = result.get("error")
        # A RATE IS ONLY PRINTED WHEN THE DETECTION ACTUALLY COMPLETED.
        #
        # detect_layer() returns layer="unknown" with an error string rather than
        # raising -- timeout, read failure, demux error, info failure. On a
        # timeout dovi_tool may have read any fraction of the file, so
        # size/elapsed is not a measurement of anything; it is a guess wearing a
        # unit. It would also be fabricated on precisely the two titles whose
        # guessed throughput caused the retraction this logging exists to
        # prevent. Caught in peer review (ChatGPT, 2026-08-09).
        #
        # "effective scan rate" is deliberate too: elapsed time covers dovi_tool's
        # own work, which is what matters operationally, but it is not a
        # byte-counter reading off the network stack.
        if error:
            logger.info("[%d] -> %s in %.0fs (%s; rate unavailable)",
                        scanned + 1, layer, dt, error)
        else:
            logger.info("[%d] -> %s in %.0fs (%.0f MB/s effective scan rate)",
                        scanned + 1, layer, dt,
                        (st.st_size / 1e6 / dt) if dt > 0 else 0.0)
        _upsert(conn, classify_to_row(path, layer, st))
        scanned += 1
        if tagging and _tag_file(path, layer):
            st2 = os.stat(path)  # header rewrite bumped mtime/size
            _upsert(conn, classify_to_row(path, layer, st2))

        # HAND OFF DURING THE WALK.
        #
        # _post_import() used to be the last statement of main(), reached only
        # after the entire root walk finished -- and the walk does not finish.
        # ~230 files at ~6/hour is ~38 hours against the scheduled task's PT6H
        # limit, so every run was killed mid-loop and the import never ran at
        # all. Measured 2026-08-10: host dv_host.db 622 rows, container dv_scan
        # 466, MAX(last_seen_at) frozen at 2026-07-26 -- Plex DV labels 14 days
        # stale while detection was working perfectly the whole time.
        #
        # Gated on NEW files rather than a timer on purpose. Each import
        # re-upserts every row, and upsert_dv_scan refreshes last_seen_at, which
        # is what the label sync watches -- so an import with nothing new behind
        # it would trigger a full every-movie-library Plex pass for no reason
        # (app_service.py's own comment calls that "pure waste"). Tying the
        # cadence to real detections keeps every sync earned.
        if args.import_every > 0 and (scanned - imported_at) >= args.import_every:
            _post_import(args.api, label="interim dv-import")
            # Advance regardless of outcome: the import is cumulative, so a
            # failure loses nothing and retrying every single file would only
            # hammer a container that is down.
            imported_at = scanned

    conn.close()
    logger.info("scanned %d file(s); posting dv-import", scanned)
    # ALWAYS run the final import, even when this run scanned nothing. Rows
    # committed by earlier killed runs are still sitting in the host store
    # unexported -- that backlog is exactly how the 622/466 gap accumulated, and
    # gating this on scanned>0 would strand it forever.
    ok = _post_import(args.api, label="final dv-import")
    if not ok:
        logger.error("final dv-import failed -- the container did not receive "
                     "this scan's results; Plex labels will not update")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
