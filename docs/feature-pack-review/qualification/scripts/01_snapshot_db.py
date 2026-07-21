#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, sqlite3
from pathlib import Path

def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def inspect(path):
    con=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
    try:
        integrity=con.execute("PRAGMA integrity_check").fetchone()[0]
        version=con.execute("PRAGMA user_version").fetchone()[0]
        tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts={}
        for table in tables:
            q='"'+table.replace('"','""')+'"'
            counts[table]=con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
        schema=[r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
    finally: con.close()
    return {"sha256":sha256(path),"size":Path(path).stat().st_size,"integrity_check":integrity,
            "user_version":version,"row_counts":counts,
            "schema_sha256":hashlib.sha256("\n".join(schema).encode()).hexdigest()}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--source",required=True); ap.add_argument("--evidence-dir",required=True)
    a=ap.parse_args(); src=Path(a.source).resolve(); ev=Path(a.evidence_dir).resolve(); ev.mkdir(parents=True,exist_ok=True)
    stamp=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst=ev/f"production-{stamp}.sqlite3"
    s=sqlite3.connect(f"file:{src}?mode=ro",uri=True); d=sqlite3.connect(dst)
    try: s.backup(d); d.commit()
    finally: d.close(); s.close()
    result={"created_at":stamp,"source":str(src),"snapshot":str(dst),"source_observation":inspect(src),"snapshot_observation":inspect(dst)}
    if result["snapshot_observation"]["integrity_check"]!="ok": raise SystemExit("snapshot integrity failure")
    (ev/"01_snapshot.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
