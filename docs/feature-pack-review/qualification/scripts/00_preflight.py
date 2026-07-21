#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3, subprocess
from pathlib import Path

CODE_SHA = "a6b4a7b14d6613c27f17de670677ed848fec458d"

def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError(f"{cmd}\n{p.stdout}\n{p.stderr}")
    return p

def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--project",required=True); ap.add_argument("--db",required=True)
    ap.add_argument("--config",required=True); ap.add_argument("--evidence-dir",required=True)
    a=ap.parse_args()
    project=Path(a.project).resolve(); db=Path(a.db).resolve()
    config=Path(a.config).resolve(); evidence=Path(a.evidence_dir).resolve()
    failures=[]
    if not (project/".git").exists(): failures.append("project is not a git checkout")
    if not db.is_file(): failures.append("database not found")
    if not config.is_file(): failures.append("config not found")
    if failures:
        print(json.dumps({"ok":False,"failures":failures},indent=2)); return 2
    if run(["git","status","--porcelain"],project).stdout.strip(): failures.append("dirty checkout")
    head=run(["git","rev-parse","HEAD"],project).stdout.strip()
    if run(["git","merge-base","--is-ancestor",CODE_SHA,head],project,False).returncode:
        failures.append("code-tested SHA is not an ancestor")
    changed=run(["git","diff","--name-only",f"{CODE_SHA}..{head}"],project).stdout.splitlines()
    unexpected=[p for p in changed if p and not p.startswith("docs/feature-pack-review/")]
    if unexpected: failures.append("non-document changes after code-tested SHA: "+", ".join(unexpected))
    evidence.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(evidence).free < db.stat().st_size*5+512*1024*1024:
        failures.append("insufficient free space")
    con=sqlite3.connect(f"file:{db}?mode=ro",uri=True)
    try:
        integrity=con.execute("PRAGMA integrity_check").fetchone()[0]
        version=con.execute("PRAGMA user_version").fetchone()[0]
    finally: con.close()
    if integrity!="ok": failures.append(f"integrity_check={integrity}")
    result={"ok":not failures,"failures":failures,"head":head,"code_tested_sha":CODE_SHA,
            "changed_after_code":changed,"db":str(db),"db_sha256":sha256(db),
            "db_size":db.stat().st_size,"db_user_version":version,"db_integrity":integrity,
            "config":str(config),"config_sha256":sha256(config),"evidence_dir":str(evidence)}
    (evidence/"00_preflight.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2)); return 0 if result["ok"] else 2
if __name__=="__main__": raise SystemExit(main())
