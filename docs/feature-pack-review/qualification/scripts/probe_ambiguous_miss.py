"""Resolve the single AMBIGUOUS RSS miss, or prove it cannot be resolved.

The shadow-cycle data alone cannot close it: the intersection is stored as a
count, so "absent from both difference sets" is genuinely unprovable there (see
miss_resolution.py). This looks OUTSIDE the shadow tables for independent
evidence that the RSS path did or did not have the release -- the main crawler
records, which are populated by the normal feed path.

Read-only. Run against a :ro mount of the live volume.
"""
import json
import sqlite3
import sys

SLUG = sys.argv[1] if len(sys.argv) > 1 else "pallichattambi"

con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(f"searching {len(tables)} tables for '{SLUG}'")

hits = []
for t in tables:
    try:
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
    except sqlite3.Error:
        continue
    for c in cols:
        try:
            n = con.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE CAST("{c}" AS TEXT) LIKE ?',
                (f"%{SLUG}%",)).fetchone()[0]
        except sqlite3.Error:
            continue
        if n:
            hits.append((t, c, n))

if not hits:
    print("NO occurrence anywhere in the database.")
else:
    seen = {}
    for t, c, n in hits:
        seen.setdefault(t, []).append(f"{c}({n})")
    for t, cs in seen.items():
        print(f"  HIT {t}: {', '.join(cs)}")

# The shadow cycles are the only place the miss itself is recorded. Show every
# cycle that mentioned it, and which difference set it was in, so the
# disappearance can be dated even if it cannot be closed.
print("\nshadow-cycle timeline for this slug:")
rows = 0
for r in con.execute(
        "SELECT cycle_uuid, completed_at, outcome, normal_feeds_complete, "
        "details_json FROM hdencode_shadow_cycles "
        "WHERE details_json IS NOT NULL AND details_json LIKE ? "
        "ORDER BY completed_at", (f"%{SLUG}%",)):
    try:
        d = json.loads(r["details_json"])
    except Exception:
        continue
    fo = [u for u in (d.get("feed_only") or []) if SLUG in u]
    lo = [u for u in (d.get("listing_only") or []) if SLUG in u]
    where = []
    if fo:
        where.append("feed_only")
    if lo:
        where.append("listing_only")
    if not where:
        continue
    rows += 1
    print(f"  {r['completed_at']}  {'+'.join(where):22s} "
          f"outcome={r['outcome']} feeds_complete={r['normal_feeds_complete']} "
          f"intersection={d.get('intersection_count')}")
if not rows:
    print("  (none)")

# Was it ever in listing_only at a moment when the SAME cycle's intersection
# count was high enough that acquisition is plausible? Not proof -- state it as
# what it is.
print("\nfirst and last cycle overall (for window bounds):")
for label, order in (("first", "ASC"), ("last", "DESC")):
    r = con.execute("SELECT completed_at FROM hdencode_shadow_cycles "
                    f"ORDER BY completed_at {order} LIMIT 1").fetchone()
    print(f"  {label}: {r['completed_at']}")
