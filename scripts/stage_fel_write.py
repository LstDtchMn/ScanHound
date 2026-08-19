"""Gates 1, 2 and 4 of ChatGPT's execution gate. STAGES ONLY — writes nothing.

Gate 1  every staged row is exactly dv_layer='fel' and came from a positive
        bounded probe; zero mel/p5/p8/none/unknown/NULL; count reconciles.
Gate 2  every staged path, rewritten to the form Plex stores, is matched
        against the live Plex part.file inventory using ScanHound's OWN
        normalize_path. Unexplained unmatched rows are a STOP condition.
Gate 4  snapshot the current managed DV labels of every affected title, so the
        operation can be reversed exactly rather than approximately.

Outputs staged_fel.jsonl, label_snapshot.json and a summary. Touches neither
dv_host.db nor Plex.
"""
import json
import os
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "dvfix"))
from backend.rename.dv_paths import normalize_path  # noqa: E402

PLEX = (r"C:\Users\NLSur\AppData\Local\Plex Media Server\Plug-in Support"
        r"\Databases\com.plexapp.plugins.library.db-2026-08-08")

# Drive letter -> the path form Plex actually stores. Verified 2026-08-10 by
# VOLUME GUID, not by the similar names: E: is labelled "4K HDR Columbo" while
# its junction is "4K Columbo", and G: has no junction at all.
REWRITE = {
    "a:\\": "C:\\4K Drives\\4K Gambino\\",
    "e:\\": "C:\\4K Drives\\4K Columbo\\",
    "i:\\": "C:\\4K Drives\\4k HDR Arnold\\",
    "j:\\": "C:\\4K Drives\\4K Jefferson & Truman BU\\",
    "q:\\": "C:\\4K Drives\\4K Quantum\\",
    "r:\\": "C:\\4K Drives\\4K Rickover\\",
    "u:\\": "C:\\4K Drives\\4K Ulysses & Yuri Gagarin BU\\",
    "g:\\": "G:\\",          # no junction; Plex uses the drive letter directly
}


def to_plex_form(path):
    low = path[:3].lower()
    if low not in REWRITE:
        return None
    return REWRITE[low] + path[3:]


# ── load probe results ─────────────────────────────────────────────────
rows = []
for name in ("local_quickcheck_part1.jsonl", "local_quickcheck.jsonl"):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            rows += [json.loads(l) for l in fh if l.strip()]
by_path = {r["path"]: r for r in rows}          # dedupe across the two files
rows = list(by_path.values())
print(f"probe results loaded : {len(rows)}")

positives = [r for r in rows if r.get("fel") is True]
print(f"  bounded-FEL positives : {len(positives)}")
print(f"  not proven FEL        : {len(rows) - len(positives)}  (staged: 0)")

# ── GATE 1 ─────────────────────────────────────────────────────────────
staged = []
for r in positives:
    plex_path = to_plex_form(r["path"])
    if plex_path is None:
        print(f"  !! no rewrite rule for {r['path'][:60]}")
        continue
    staged.append({"source_path": r["path"], "path": plex_path,
                   "dv_layer": "fel", "gb": r.get("gb"),
                   "evidence": "bounded", "probe_seconds": r.get("seconds")})

layers = Counter(s["dv_layer"] for s in staged)
print("\nGATE 1 — FEL-only staging")
print(f"  staged rows            : {len(staged)}")
print(f"  layer histogram        : {dict(layers)}")
g1 = (set(layers) == {"fel"} and len(staged) == len(positives)
      and all(s["evidence"] == "bounded" for s in staged))
print(f"  every row fel+bounded, count reconciles : {g1}")
if not g1:
    print("  GATE 1 FAILED — stop")
    sys.exit(1)

# ── GATE 2 ─────────────────────────────────────────────────────────────
c = sqlite3.connect("file:" + urllib.parse.quote(PLEX.replace("\\", "/")) + "?mode=ro",
                    uri=True)
plex_norm = {}
for mid, f in c.execute("""SELECT mi.id, mp.file FROM metadata_items mi
        JOIN media_items md ON md.metadata_item_id = mi.id
        JOIN media_parts mp ON mp.media_item_id = md.id
        WHERE mp.file IS NOT NULL"""):
    plex_norm.setdefault(normalize_path(f), mid)

# Positive control: a path we KNOW Plex holds must be found, or a zero
# "unmatched" result would be meaningless.
control = normalize_path(r"C:\4K Drives\4K Gambino\4K HDR\10 Days of a Bad Man (2023).mkv")
print("\nGATE 2 — live Plex path preflight")
print(f"  Plex part paths indexed : {len(plex_norm)}")
print(f"  positive control found  : {control in plex_norm}")
if control not in plex_norm:
    print("  CONTROL FAILED — an unmatched count here would prove nothing. Stop.")
    sys.exit(1)

matched, unmatched = [], []
for s in staged:
    n = normalize_path(s["path"])
    if n in plex_norm:
        s["plex_id"] = plex_norm[n]
        matched.append(s)
    else:
        unmatched.append(s)
print(f"  staged rows MATCHED     : {len(matched)}")
print(f"  staged rows UNMATCHED   : {len(unmatched)}")

# The gate is ZERO UNEXPLAINED MISMATCHES, not a coverage percentage -- 99.3%
# is not what makes this pass. Reviewer's caveat on the previous revision was
# that the script announced a stop condition and then did not stop: the
# explained/unexplained distinction lived in human review rather than in code,
# so a NEW mismatch (say, a systematic rewrite failure like the 2026-07-11
# Y:-drive incident) would have printed among the examples and been waved past.
#
# Each entry must carry a reason. Adding one is a deliberate act; forgetting to
# is now a hard stop.
EXPLAINED_NO_PLEX_TARGET = {
    "Hamilton.2020.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.TrueHD.Atmos.7.1-CiNEPHiLES.mkv":
        "raw release name; Plex never matched it to a library item",
    "Day.of.the.Dead.1985.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.FLAC.1.0-CiNEPHiLES.mkv":
        "raw release name; Plex never matched it to a library item",
    "Bowfinger.1999.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.DTS-HD.MA.5.1-CiNEPHiLES.mkv":
        "raw release name; Plex never matched it to a library item",
    "Notting Hill (1999).mkv": "file present on disk, absent from the Plex library",
    "The Return of the Pink Panther (1975).mkv":
        "file present on disk, absent from the Plex library",
}

unexplained = [s for s in unmatched
               if os.path.basename(s["path"]) not in EXPLAINED_NO_PLEX_TARGET]
for s in unmatched:
    base = os.path.basename(s["path"])
    why = EXPLAINED_NO_PLEX_TARGET.get(base, "*** UNEXPLAINED ***")
    print(f"    {base[:58]:<60} {why}")
if unexplained:
    print(f"\n  GATE 2 FAILED: {len(unexplained)} unmatched row(s) with no recorded")
    print("  explanation. Each must be investigated and added to")
    print("  EXPLAINED_NO_PLEX_TARGET with a reason, or the rewrite table is wrong.")
    sys.exit(1)
print("  zero UNEXPLAINED mismatches -> gate 2 invariant holds")

# ── GATE 4 ─────────────────────────────────────────────────────────────
ids = [s["plex_id"] for s in matched]
snapshot = {}
if ids:
    ph = ",".join("?" * len(ids))
    tagrows = c.execute(f"""SELECT mi.id, mi.title, t.tag FROM metadata_items mi
        LEFT JOIN taggings tg ON tg.metadata_item_id = mi.id
        LEFT JOIN tags t ON t.id = tg.tag_id
              AND t.tag IN ('DV FEL','DV MEL','DV P8','DV P5')
        WHERE mi.id IN ({ph})""", ids).fetchall()
    for mid, title, tag in tagrows:
        e = snapshot.setdefault(str(mid), {"title": title, "labels": []})
        if tag:
            e["labels"].append(tag)

pre = Counter()
for v in snapshot.values():
    pre["+".join(sorted(v["labels"])) or "(none)"] += 1
print("\nGATE 4 — pre-write label snapshot")
print(f"  titles snapshotted      : {len(snapshot)}")
print("  their CURRENT managed DV labels:")
for k, v in pre.most_common(8):
    print(f"    {v:>5}  {k}")
would_replace = sum(n for k, n in pre.items() if k not in ("(none)", "DV FEL"))
print(f"\n  titles where DV FEL would REPLACE a different managed label : {would_replace}")
print("  (authoritative replacement happens even in additive-only mode)")

# ── outputs ────────────────────────────────────────────────────────────
# Split per the reviewer's option 1: rows intended to produce a Plex label are
# kept separate from rows with no Plex target, so
#     rows intended for Plex effect == matched rollback snapshot population
# and the five residuals stay individually enumerated instead of dissolving
# into an undifferentiated "unmatched" count.
json.dump(matched, open(os.path.join(HERE, "staged_fel_apply.jsonl"), "w",
                        encoding="utf-8"), indent=1)
json.dump(unmatched, open(os.path.join(HERE, "staged_fel_no_plex_target.jsonl"), "w",
                          encoding="utf-8"), indent=1)
json.dump(snapshot, open(os.path.join(HERE, "label_snapshot.json"), "w",
                         encoding="utf-8"), indent=1)

print("\n--- accounting ---")
print(f"  rows intended for Plex effect : {len(matched)}")
print(f"  rollback snapshot population  : {len(snapshot)}")
print(f"  reconciles                    : {len(matched) == len(snapshot)}")
print(f"  explained no-Plex-target rows : {len(unmatched)} (enumerated below)")
for s in unmatched:
    print(f"      {os.path.basename(s['path'])}")
print(f"\nwrote staged_fel_apply.jsonl ({len(matched)}), "
      f"staged_fel_no_plex_target.jsonl ({len(unmatched)}), "
      f"label_snapshot.json ({len(snapshot)}).")
print("NOTHING written to dv_host.db or Plex.")
