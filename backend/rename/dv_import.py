"""Ingest the host detector's dv_host.db into crawler.db's dv_scan table.

The container is the SOLE owner of crawler.db. This reads the host store
read-only (raw sqlite3 — it must NOT construct a second DatabaseManager on the
host DB, which would run DDL) and upserts every row as source='scan', which the
upsert's ON CONFLICT supersedes any existing 'seed' row for the same path.
"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


def import_dv_host_db(db, host_db_path):
    """Upsert every dv_host.db row into *db*.dv_scan as source='scan'.

    Returns ``{"imported": <new paths>, "updated": <existing paths>}``.
    A missing/unreadable host DB is a no-op returning zeros.
    """
    if not host_db_path or not os.path.exists(host_db_path):
        logger.warning("dv-import: host db not found: %s", host_db_path)
        return {"imported": 0, "updated": 0}

    imported = 0
    updated = 0
    try:
        conn = sqlite3.connect(f"file:{host_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT path, dv_layer, sig_mtime, sig_size, title FROM dv_host").fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.error("dv-import: reading host db failed: %s", e)
        return {"imported": 0, "updated": 0}

    for r in rows:
        path = r["path"]
        if not path:
            continue
        existed = db.get_dv_scan(path) is not None
        ok = db.upsert_dv_scan(
            path, r["dv_layer"], title=r["title"],
            sig_mtime=r["sig_mtime"], sig_size=r["sig_size"], source="scan")
        if not ok:
            continue
        if existed:
            updated += 1
        else:
            imported += 1
    return {"imported": imported, "updated": updated}
