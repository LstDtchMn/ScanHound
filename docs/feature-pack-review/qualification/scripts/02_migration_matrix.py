#!/usr/bin/env python3
"""Run production-schema qualification against disposable database copies only."""
from __future__ import annotations
import argparse, hashlib, json, random, shutil, sqlite3, subprocess, time, uuid
from pathlib import Path

def run(cmd, check=False):
    p=subprocess.run(cmd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and p.returncode: raise RuntimeError(f"{cmd}\n{p.stdout}\n{p.stderr}")
    return p

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
        for t in tables:
            q='"'+t.replace('"','""')+'"'
            counts[t]=con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
    finally: con.close()
    return {"sha256":sha256(path),"integrity_check":integrity,"user_version":version,"row_counts":counts}

def init_image(image, directory, dbname):
    code=("from backend.database import DatabaseManager;"
          f"db=DatabaseManager('/qualification/{dbname}');"
          "c=db.get_connection();print(c.execute('PRAGMA user_version').fetchone()[0]);"
          "print(c.execute('PRAGMA integrity_check').fetchone()[0]);db.close()")
    return run(["docker","run","--rm","-v",f"{directory}:/qualification","--entrypoint","python",image,"-c",code])

def kill_attempt(image, directory, dbname):
    name="scanhound-migration-"+uuid.uuid4().hex[:10]
    code=("from backend.database import DatabaseManager;"
          f"DatabaseManager('/qualification/{dbname}');print('COMPLETED')")
    p=subprocess.Popen(["docker","run","--name",name,"-v",f"{directory}:/qualification",
                        "--entrypoint","python",image,"-c",code],
                       text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    delay=random.uniform(.002,.150); time.sleep(delay); alive=p.poll() is None
    if alive: run(["docker","kill","--signal","KILL",name])
    out,err=p.communicate(timeout=30); run(["docker","rm","-f",name])
    return {"delay":delay,"was_alive":alive,"returncode":p.returncode,"stdout":out,"stderr":err}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--snapshot",required=True); ap.add_argument("--evidence-dir",required=True)
    ap.add_argument("--new-image",required=True); ap.add_argument("--old-image",required=True)
    ap.add_argument("--interrupt-attempts",type=int,default=30)
    a=ap.parse_args()
    snapshot=Path(a.snapshot).resolve(); ev=Path(a.evidence_dir).resolve()
    work=ev/"migration-matrix"
    if work.exists(): raise SystemExit(f"refusing existing work directory {work}")
    work.mkdir(parents=True)
    base=inspect(snapshot); failures=[]; cases={}
    cur=work/"current"; cur.mkdir(); curdb=cur/snapshot.name; shutil.copy2(snapshot,curdb)
    p1=init_image(a.new_image,cur,curdb.name); o1=inspect(curdb)
    p2=init_image(a.new_image,cur,curdb.name); o2=inspect(curdb)
    cases["current_upgrade_restart"]={"first":vars(p1),"second":vars(p2),"after_first":o1,"after_second":o2}
    if p1.returncode or p2.returncode: failures.append("new-image migration/restart command failed")
    if o1["integrity_check"]!="ok" or o2["integrity_check"]!="ok": failures.append("new-image integrity failure")
    for t,c in base["row_counts"].items():
        if o2["row_counts"].get(t)!=c: failures.append(f"pre-existing row count changed: {t} {c}->{o2['row_counts'].get(t)}")
    olddir=work/"old-reopen"; olddir.mkdir(); olddb=olddir/snapshot.name; shutil.copy2(curdb,olddb)
    po=init_image(a.old_image,olddir,olddb.name); oo=inspect(olddb)
    cases["old_image_reopen"]={"process":vars(po),"observation":oo}
    if po.returncode or oo["integrity_check"]!="ok": failures.append("old image did not safely reopen migrated copy")
    events=[]; actually_interrupted=False
    for i in range(max(1,a.interrupt_attempts)):
        idir=work/f"interrupt-{i:02d}"; idir.mkdir(); idb=idir/snapshot.name; shutil.copy2(snapshot,idb)
        event=kill_attempt(a.new_image,idir,idb.name)
        reopen=init_image(a.new_image,idir,idb.name); obs=inspect(idb)
        event.update({"reopen":vars(reopen),"observation":obs}); events.append(event)
        if event["was_alive"]:
            actually_interrupted=True
            if reopen.returncode or obs["integrity_check"]!="ok": failures.append("interrupted copy did not recover")
            break
    cases["interrupted_migration"]=events
    if not actually_interrupted: failures.append("could not interrupt while migration process was alive; evidence inconclusive")
    roll=work/"rollback"; roll.mkdir(); rdb=roll/snapshot.name; shutil.copy2(snapshot,rdb); ro=inspect(rdb)
    cases["rollback_restore"]=ro
    if ro["sha256"]!=base["sha256"] or ro["integrity_check"]!="ok": failures.append("rollback restore mismatch")
    result={"ok":not failures,"failures":failures,"baseline":base,"new_image":a.new_image,"old_image":a.old_image,"cases":cases}
    (ev/"02_migration_matrix.json").write_text(json.dumps(result,indent=2,default=str)+"\n")
    print(json.dumps(result,indent=2,default=str)); return 0 if result["ok"] else 2
if __name__=="__main__": raise SystemExit(main())
