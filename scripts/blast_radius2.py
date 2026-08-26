"""Blast radius of the MANUAL /dv-sync-labels button, modelled the way the
consumer actually works.

Two corrections to my first attempt:
  * it compared RAW casefolded strings, ignoring normalize_path's Y: <-> UNC
    mapping -- the same vacuous comparison this project has been bitten by
    before. Use the real function.
  * it keyed on individual FILES. reconcile_movie operates on a TITLE and
    pick_layer takes ALL of its parts, returning the first positive rank found.
    A title is therefore protected if ANY of its parts has an authoritative
    row -- "300 (2007)" has two parts, one on a backup drive and one on the
    network share.

Read-only, static Plex backup.
"""
import os
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "dvfix"))
from backend.rename.dv_paths import normalize_path  # noqa: E402
from backend.rename.dv_labeler import MANAGED, is_authoritative  # noqa: E402

PLEX = (r"C:\Users\NLSur\AppData\Local\Plex Media Server\Plug-in Support"
        r"\Databases\com.plexapp.plugins.library.db-2026-08-08")

# --- scan rows, WITH their layer (an 'unknown' row is not a match) ----------
scan = {}
with open(os.path.join(HERE, "scan_rows.tsv"), encoding="utf-8") as fh:
    for line in fh:
        if not line.strip():
            continue
        path, layer = line.rstrip("\n").split("\t")
        scan[normalize_path(path)] = layer

print(f"POSITIVE CONTROL")
print(f"  scan rows loaded            : {len(scan)}  (expect 466)")
a = normalize_path(r"Y:/Movie 1 (14TB)/4K DV\300 (2007).mkv")
b = normalize_path("//TURTLELANDSRV2/4K HDR Geronimo/Movie 1 (14TB)/4K DV/300 (2007).mkv")
print(f"  Y:-form == UNC-form         : {a == b}")
print(f"  a known scan path is present: {a in scan}")
assert len(scan) > 400 and a == b and a in scan, "CONTROL FAILED - stop"
print("  controls pass\n")

# --- Plex titles carrying a managed DV label, with ALL their parts ----------
c = sqlite3.connect("file:" + urllib.parse.quote(PLEX.replace("\\", "/")) + "?mode=ro",
                    uri=True)
# Derived from MANAGED, never restated. A hardcoded four-label list named
# the pre-rename badges and knew nothing of DV8/DV5/DV7/DV/HDR10, so it
# UNDER-stated the blast radius of the button it exists to measure -- the
# dangerous direction to be wrong in.
_managed = sorted(MANAGED)
_ph_managed = ",".join("?" * len(_managed))
tags = [r[0] for r in c.execute(
    f"SELECT id FROM tags WHERE tag IN ({_ph_managed})", _managed)]
ph = ",".join("?" * len(tags))
labelled_ids = {r[0]: r[1] for r in c.execute(
    f"""SELECT DISTINCT mi.id, mi.title FROM taggings tg
        JOIN metadata_items mi ON mi.id = tg.metadata_item_id
        WHERE tg.tag_id IN ({ph})""", tags)}

parts = defaultdict(list)
ph2 = ",".join("?" * len(labelled_ids))
for mid, f in c.execute(
        f"""SELECT mi.id, mp.file FROM metadata_items mi
            JOIN media_items md ON md.metadata_item_id = mi.id
            JOIN media_parts mp ON mp.media_item_id = md.id
            WHERE mi.id IN ({ph2}) AND mp.file IS NOT NULL""",
        list(labelled_ids)):
    parts[mid].append(normalize_path(f))

print(f"Plex titles carrying a managed DV label : {len(labelled_ids)}")
print(f"  multi-part among them                 : "
      f"{sum(1 for v in parts.values() if len(v) > 1)}")

safe, at_risk, no_parts = [], [], []
for mid, title in labelled_ids.items():
    ps = parts.get(mid, [])
    if not ps:
        no_parts.append(title)
        continue
    # pick_layer returns the first POSITIVE rank across all parts; an
    # 'unknown' row is not a positive finding.
    hit = [scan[p] for p in ps if p in scan]
    if any(is_authoritative(l) and l != "none" for l in hit):
        safe.append(title)
    elif any(l == "none" for l in hit):
        safe.append(title)          # authoritative none: matched, label removed legitimately
    else:
        at_risk.append(title)

print(f"\n  protected by an authoritative scan row : {len(safe)}")
print(f"  NO matching scan row -> label REMOVED  : {len(at_risk)}")
print(f"  (no part rows in Plex at all)          : {len(no_parts)}")
print("\n^ the middle number is how many DV badges the MANUAL sync button would")
print("  strip today, before any of tonight's work is written.")
if at_risk:
    print("\n  examples:")
    for t in sorted(at_risk)[:10]:
        print(f"    {t[:60]}")
