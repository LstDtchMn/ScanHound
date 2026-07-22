#!/usr/bin/env python3
"""Runbook step 2 (adapted): sqlite .backup snapshot of /dbvol/crawler.db.

Runs inside a FRESH throwaway container with the scanhound_scanhound_db volume
mounted read-only at /dbvol and the evidence dir at /out. Equivalent to the
bundle's 01_snapshot_db.py (backup + inspect both sides) but with the volume
paths fixed and no argparse, so it can run with a bare ``python /out/snapshot_dbvol.py``.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

SRC = "/dbvol/crawler.db"
OUT = Path("/out")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        version = con.execute("PRAGMA user_version").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        for t in tables:
            q = '"' + t.replace('"', '""') + '"'
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
        schema = [r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
    finally:
        con.close()
    return {
        "sha256": sha256(path), "size": Path(path).stat().st_size,
        "integrity_check": integrity, "user_version": version, "row_counts": counts,
        "schema_sha256": hashlib.sha256("\n".join(schema).encode()).hexdigest(),
    }


def main():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = OUT / f"production-{stamp}.sqlite3"
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.commit()
    finally:
        d.close()
        s.close()
    result = {
        "created_at": stamp, "source": SRC, "snapshot": dst.name,
        "source_observation": inspect(SRC),
        "snapshot_observation": inspect(dst),
    }
    if result["snapshot_observation"]["integrity_check"] != "ok":
        raise SystemExit("snapshot integrity failure")
    (OUT / "01_snapshot.json").write_text(json.dumps(result, indent=2) + "\n")
    # Print WITHOUT row-by-row data; row_counts + hashes only (no content).
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
