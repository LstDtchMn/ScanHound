#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, hashlib, json
from pathlib import Path
def sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""): h.update(c)
    return h.hexdigest()
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--evidence-dir",required=True); a=ap.parse_args()
    ev=Path(a.evidence_dir).resolve(); skip={"FINAL_EVIDENCE_INDEX.json","FINAL_EVIDENCE_INDEX.md","SHA256SUMS"}
    entries=[{"path":str(p.relative_to(ev)),"size":p.stat().st_size,"sha256":sha(p)}
             for p in sorted(ev.rglob("*")) if p.is_file() and p.name not in skip]
    index={"generated_at":dt.datetime.now(dt.timezone.utc).isoformat(),
           "code_tested_sha":"a6b4a7b14d6613c27f17de670677ed848fec458d","entries":entries}
    (ev/"FINAL_EVIDENCE_INDEX.json").write_text(json.dumps(index,indent=2)+"\n")
    lines=["# ScanHound final production qualification evidence","",f"Generated: {index['generated_at']}","",
           f"Code-tested SHA: `{index['code_tested_sha']}`","","| File | Bytes | SHA-256 |","|---|---:|---|"]
    lines += [f"| `{e['path']}` | {e['size']} | `{e['sha256']}` |" for e in entries]
    (ev/"FINAL_EVIDENCE_INDEX.md").write_text("\n".join(lines)+"\n")
    (ev/"SHA256SUMS").write_text("\n".join(f"{e['sha256']}  {e['path']}" for e in entries)+"\n")
    print(json.dumps(index,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
