"""Ingest the host detector's Dolby Vision rows into crawler.db's dv_scan table.

TWO ways in, ONE upsert. The container is the SOLE owner of crawler.db.

  * import_dv_rows(db, rows)   — the durable path (round-4 review). The detector
    POSTs its rows in the request body; the container never reads the host file,
    so the Windows bind-mount / WAL-mmap failure class does not exist here.
  * import_dv_host_db(db, path) — the legacy path. Reads dv_host.db read-only
    (raw sqlite3 — it must NOT construct a second DatabaseManager on the host DB,
    which would run DDL). RETAINED for compatibility, but a read/open failure now
    RAISES rather than returning zeros behind an HTTP 200: an empty database that
    read cleanly and a database that could not be read are different states and
    must be distinguishable at the API boundary (round-4 finding 2).
"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


class DvHostReadError(Exception):
    """The host dv_host.db could not be opened/read. NOT an empty import."""


def import_dv_rows(db, rows):
    """Upsert an iterable of DV rows into *db*.dv_scan as source='scan'.

    Each row is a mapping with at least ``path`` and ``dv_layer``; ``title``,
    ``sig_mtime`` and ``sig_size`` are optional. Returns explicit counts so the
    caller can validate the outcome mechanically:

        {"source_rows": N, "processed": P, "imported": I, "updated": U,
         "failed": F}

    ``processed`` counts rows that reached an upsert (a row with no ``path`` is
    skipped and counts as neither processed nor failed). ``failed`` counts rows
    whose upsert returned falsey — a partial failure the caller MUST surface.
    """
    rows = list(rows or [])
    imported = updated = processed = failed = 0
    for r in rows:
        path = r.get("path")
        if not path:
            continue
        existed = db.get_dv_scan(path) is not None
        ok = db.upsert_dv_scan(
            path, r.get("dv_layer"), title=r.get("title"),
            sig_mtime=r.get("sig_mtime"), sig_size=r.get("sig_size"),
            source="scan")
        if not ok:
            failed += 1
            continue
        processed += 1
        if existed:
            updated += 1
        else:
            imported += 1
    return {
        "source_rows": len(rows),
        "processed": processed,
        "imported": imported,
        "updated": updated,
        "failed": failed,
    }


def import_dv_host_db(db, host_db_path):
    """Read dv_host.db and upsert its rows into *db*.dv_scan (legacy path).

    RAISES DvHostReadError if the host DB is missing or cannot be read, so the
    route can return a non-2xx status instead of a silent zero-row success.
    """
    if not host_db_path or not os.path.exists(host_db_path):
        raise DvHostReadError(f"host db not found: {host_db_path}")
    try:
        conn = sqlite3.connect(f"file:{host_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r) for r in conn.execute(
                "SELECT path, dv_layer, sig_mtime, sig_size, title FROM dv_host")
        ]
        conn.close()
    except sqlite3.Error as e:
        # This is exactly the case that hid the bind-mount/WAL failure behind a
        # 200. It now propagates.
        raise DvHostReadError(f"reading host db failed: {e}") from e
    return import_dv_rows(db, rows)
