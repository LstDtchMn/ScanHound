#!/usr/bin/env python3
"""Non-destructive filesystem capability sentinel."""
from __future__ import annotations
import argparse, ctypes, datetime as dt, errno, hashlib, json, os, shutil, stat, sys, uuid
from pathlib import Path

RENAME_NOREPLACE=1; AT_FDCWD=-100

def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def identity(path):
    st=path.stat(); r={"path":str(path),"device":st.st_dev,"mode":oct(stat.S_IMODE(st.st_mode)),"platform":sys.platform}
    try:
        v=os.statvfs(path); r.update({"block_size":v.f_bsize,"blocks":v.f_blocks,"free_blocks":v.f_bfree,"name_max":v.f_namemax})
    except (AttributeError,OSError): pass
    return r

def noreplace(src,dst):
    if os.name=="nt":
        try: os.rename(src,dst); return {"supported":True,"unexpected_replace":True}
        except FileExistsError: return {"supported":True,"destination_preserved":True}
        except OSError as e: return {"supported":False,"errno":e.errno,"error":str(e)}
    libc=ctypes.CDLL(None,use_errno=True); fn=getattr(libc,"renameat2",None)
    if fn is None: return {"supported":False,"error":"renameat2 unavailable"}
    fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]; fn.restype=ctypes.c_int
    rc=fn(AT_FDCWD,os.fsencode(src),AT_FDCWD,os.fsencode(dst),RENAME_NOREPLACE)
    if rc==0: return {"supported":True,"unexpected_replace":True}
    err=ctypes.get_errno()
    return {"supported":err not in (errno.ENOSYS,errno.EINVAL,errno.ENOTSUP),
            "destination_preserved":err in (errno.EEXIST,errno.ENOTEMPTY),"errno":err,"error":os.strerror(err)}

def fsync_file(path):
    try:
        with open(path,"r+b") as f: f.flush(); os.fsync(f.fileno())
        return {"supported":True}
    except OSError as e: return {"supported":False,"errno":e.errno,"error":str(e)}

def fsync_dir(path):
    try:
        fd=os.open(path,getattr(os,"O_DIRECTORY",0)|os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return {"supported":True}
    except OSError as e: return {"supported":False,"errno":e.errno,"error":str(e)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--parent",required=True); ap.add_argument("--secondary-parent")
    ap.add_argument("--evidence-dir",required=True); ap.add_argument("--execute",action="store_true")
    a=ap.parse_args(); parent=Path(a.parent).resolve(); ev=Path(a.evidence_dir).resolve()
    if "scanhound-sentinel" not in parent.name.lower(): raise SystemExit("parent basename must contain scanhound-sentinel")
    parent.mkdir(parents=True,exist_ok=True)
    if any(parent.iterdir()): raise SystemExit("refusing non-empty sentinel parent")
    if not a.execute:
        print(json.dumps({"dry_run":True,"parent":str(parent),"identity":identity(parent)},indent=2)); return 0
    run=parent/("run-"+uuid.uuid4().hex); run.mkdir(); failures=[]
    result={"started_at":dt.datetime.now(dt.timezone.utc).isoformat(),"parent":str(parent),"identity":identity(parent),"tests":{},"failures":failures}
    try:
        src=run/"src.bin"; dst=run/"dst.bin"; src.write_bytes(b"SRC"+os.urandom(32)); dst.write_bytes(b"DST"+os.urandom(32))
        sh,dh=digest(src),digest(dst); nr=noreplace(src,dst)
        nr.update({"source_exists":src.exists(),"source_unchanged":src.exists() and digest(src)==sh,
                   "destination_exists":dst.exists(),"destination_unchanged":dst.exists() and digest(dst)==dh})
        result["tests"]["no_replace"]=nr
        if nr.get("unexpected_replace") or not nr["destination_unchanged"]: failures.append("no-replace failed")
        orig=run/"hardlink-original"; link=run/"hardlink-link"; orig.write_bytes(os.urandom(64))
        try:
            os.link(orig,link); hard={"supported":True,"same_inode":orig.stat().st_ino==link.stat().st_ino,
                                     "same_device":orig.stat().st_dev==link.stat().st_dev,"content_equal":digest(orig)==digest(link)}
        except OSError as e: hard={"supported":False,"errno":e.errno,"error":str(e)}
        result["tests"]["hardlink"]=hard
        sf=run/"fsync.bin"; sf.write_bytes(os.urandom(64))
        result["tests"]["file_fsync"]=fsync_file(sf); result["tests"]["directory_fsync"]=fsync_dir(run)
        manifest=run/"manifest.json"; manifest.write_text('{"generation":0}\n')
        tmp=run/".manifest.tmp"; tmp.write_text('{"generation":1}\n')
        ts=fsync_file(tmp); os.replace(tmp,manifest); ds=fsync_dir(run)
        result["tests"]["manifest_atomic_replace"]={"temp_fsync":ts,"directory_fsync":ds,"content":manifest.read_text()}
        if manifest.read_text()!='{"generation":1}\n': failures.append("atomic replace mismatch")
        if a.secondary_parent:
            second=Path(a.secondary_parent).resolve()
            if "scanhound-sentinel" not in second.name.lower(): failures.append("secondary parent name invalid")
            else:
                second.mkdir(parents=True,exist_ok=True)
                if any(second.iterdir()): failures.append("secondary parent non-empty")
                else:
                    rd=second/("run-"+uuid.uuid4().hex); rd.mkdir(); cs=run/"cross.bin"; cd=rd/"cross.bin"; cs.write_bytes(os.urandom(64))
                    try:
                        os.rename(cs,cd); ex={"exdev_boundary":False,"rename_succeeded":True,"src_device":run.stat().st_dev,"dst_device":rd.stat().st_dev}
                    except OSError as e: ex={"exdev_boundary":e.errno==errno.EXDEV,"errno":e.errno,"error":str(e),"source_intact":cs.exists()}
                    result["tests"]["exdev"]=ex; shutil.rmtree(rd)
        result["ok"]=not failures
    finally:
        result["created_files"]=sorted(str(p.relative_to(run)) for p in run.rglob("*") if p.is_file())
        shutil.rmtree(run)
        result["cleanup"]={"run_removed":not run.exists(),"parent_empty":not any(parent.iterdir())}
        if not all(result["cleanup"].values()): failures.append("cleanup failed"); result["ok"]=False
        result["finished_at"]=dt.datetime.now(dt.timezone.utc).isoformat()
    ev.mkdir(parents=True,exist_ok=True)
    out=ev/("03_sentinel_"+hashlib.sha256(str(parent).encode()).hexdigest()[:12]+".json")
    out.write_text(json.dumps(result,indent=2)+"\n"); print(json.dumps(result,indent=2))
    return 0 if result["ok"] else 2
if __name__=="__main__": raise SystemExit(main())
