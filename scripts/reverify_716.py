"""Re-derive the staged FEL set under the CONSOLIDATED parser.

The 716 positives were produced by the old _classify, which fired on the raw
substring "FEL" at any profile. The consolidated one requires profile 7 with
tokens subset of {FEL, MEL}. That is strictly narrower, so:

    new-FEL  is a subset of  old-FEL

No old negative can become a positive, which is why only the 716 positives need
re-testing rather than all 2,738. Any disagreement is a FALSE POSITIVE that
would have become a wrong Plex badge.

Read-only.
"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
CONSOL = os.path.join(HERE, "consol")
os.environ["PATH"] = r"X:\Docker Apps\ScanHound\scripts\host-detector" + os.pathsep + os.environ["PATH"]
sys.path.insert(0, CONSOL)
from backend.rename import dv_detect

rows = {}
for n in ("local_quickcheck_part1.jsonl", "local_quickcheck.jsonl"):
    p = os.path.join(HERE, n)
    if os.path.exists(p):
        for l in open(p, encoding="utf-8"):
            if l.strip():
                r = json.loads(l); rows[r["path"]] = r
pos = [r for r in rows.values() if r.get("fel")]
print(f"old-parser positives to re-verify: {len(pos)}", flush=True)

out = open(os.path.join(HERE, "reverify_716.jsonl"), "w", encoding="utf-8")
agree = disagree = missing = 0
t0 = time.time()
for i, r in enumerate(pos, 1):
    p = r["path"]
    if not os.path.isfile(p):
        missing += 1
        out.write(json.dumps({"path": p, "still_fel": None, "note": "file gone"}) + "\n")
        continue
    still = dv_detect.probe_fel_bounded(p)
    agree += bool(still); disagree += (not still)
    out.write(json.dumps({"path": p, "still_fel": bool(still)}) + "\n")
    out.flush()
    if not still:
        print(f"  [{i}/{len(pos)}] *** DISAGREES *** {os.path.basename(p)[:56]}", flush=True)
    elif i % 50 == 0:
        el = (time.time()-t0)/60
        print(f"  [{i}/{len(pos)}] agree={agree} disagree={disagree} "
              f"elapsed={el:.1f}m eta={el/i*(len(pos)-i):.1f}m", flush=True)
out.close()
print(f"\nDONE in {(time.time()-t0)/60:.1f} min")
print(f"  still FEL under consolidated parser : {agree}")
print(f"  DISAGREE (would have been wrong)    : {disagree}")
print(f"  file no longer present              : {missing}")
