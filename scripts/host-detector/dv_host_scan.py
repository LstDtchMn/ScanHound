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


def _post_import(api_base):
    url = api_base.rstrip("/") + DV_IMPORT_PATH
    req = urllib.request.Request(url, data=b"{}",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            logger.info("dv-import -> %s", resp.read().decode("utf-8", "replace"))
    except OSError as e:
        logger.error("dv-import POST failed: %s", e)


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
    for path in _iter_files(parse_roots(cfg)):
        try:
            st = os.stat(path)
        except OSError:
            continue
        stored_m, stored_s = _get_sig(conn, path)
        if sig_is_current(stored_m, stored_s, st.st_mtime, st.st_size):
            continue
        layer = dv_detect.detect_layer(path).get("layer")
        _upsert(conn, classify_to_row(path, layer, st))
        scanned += 1
        if tagging and _tag_file(path, layer):
            st2 = os.stat(path)  # header rewrite bumped mtime/size
            _upsert(conn, classify_to_row(path, layer, st2))
    conn.close()
    logger.info("scanned %d file(s); posting dv-import", scanned)
    _post_import(args.api)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
