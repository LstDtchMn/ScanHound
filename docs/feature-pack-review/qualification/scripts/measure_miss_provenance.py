"""How many recorded RSS 'misses' came from cycles whose comparison was invalid?

Motivation. miss_resolution.py filters cycles for usability before trusting them
as OBSERVATIONS (normal_feeds_complete=1 and both request counts > 0), but draws
the misses themselves with an unfiltered JOIN. So a miss recorded during a cycle
the grader refuses to trust still enters the miss list and can block the gate.

Upstream cause, in backend/hdencode_shadow.py compare_shadow():

    outcome='success' if normal_feeds_complete else 'incomplete_feeds'
    if misses: outcome='relevant_miss'

misses comes from listing_only = listing_urls - rss. When the normal feeds did
not complete, rss is empty or short, listing_only inflates, and every inflated
entry in a relevant state is booked as a miss -- then the relevant_miss label
overwrites the incomplete_feeds label that says the comparison was invalid.

This script only MEASURES. It changes nothing and asserts nothing about the
right fix. Read-only.
CORRECTED 2026-08-06: this script shipped querying a nonexistent `miss_count`
column (the schema defines `relevant_miss_count`), so the final section threw
`no such column` on any database built from this branch. I hit that error while
writing the script, routed around it into a separate file, and committed the
broken query anyway; a peer review found it. Reproduction scripts must be run
against a fresh schema, not only the live snapshot that happened to work.
"""
import json
import sqlite3
from collections import Counter

con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

cyc = {}
for r in con.execute(
        "SELECT cycle_uuid, completed_at, outcome, normal_feeds_complete, "
        "rss_requests, listing_requests, details_json "
        "FROM hdencode_shadow_cycles"):
    usable = (r["outcome"] in ("success", "relevant_miss")
              and r["normal_feeds_complete"] == 1
              and (r["rss_requests"] or 0) > 0
              and (r["listing_requests"] or 0) > 0)
    cyc[r["cycle_uuid"]] = {
        "at": r["completed_at"], "outcome": r["outcome"],
        "nfc": r["normal_feeds_complete"], "rssq": r["rss_requests"] or 0,
        "lq": r["listing_requests"] or 0, "usable": usable,
    }

print(f"cycles: {len(cyc)} total, {sum(1 for c in cyc.values() if c['usable'])} usable")
print("cycle outcome x normal_feeds_complete:")
for (o, n), k in sorted(Counter((c["outcome"], c["nfc"]) for c in cyc.values()).items()):
    print(f"  {o:18s} nfc={n}  {k:4d} cycles")

misses = list(con.execute(
    "SELECT m.canonical_url u, m.title, m.status, m.cycle_uuid "
    "FROM hdencode_shadow_misses m"))
print(f"\nrecorded misses: {len(misses)}")

from_unusable, from_usable, orphan = [], [], []
for m in misses:
    c = cyc.get(m["cycle_uuid"])
    if c is None:
        orphan.append(m)
    elif c["usable"]:
        from_usable.append((m, c))
    else:
        from_unusable.append((m, c))

print(f"  from USABLE cycles   : {len(from_usable)}")
print(f"  from UNUSABLE cycles : {len(from_unusable)}")
print(f"  orphaned (no cycle)  : {len(orphan)}")

if from_unusable:
    print("\nmisses sourced from cycles the grader will not trust:")
    for m, c in sorted(from_unusable, key=lambda x: x[1]["at"]):
        print(f"  {c['at'][:19]}  outcome={c['outcome']:14s} nfc={c['nfc']} "
              f"rssq={c['rssq']:3d} lq={c['lq']:3d}")
        print(f"      {m['u'][-72:]}")
        print(f"      status={m['status']}")

# The decisive cross-check: how many DISTINCT misses did each unusable cycle
# produce? A cycle with a short rss set should inflate listing_only wholesale,
# so a single miss from a single unusable cycle would be surprising.
print("\nmisses per cycle (top 6):")
for cu, k in Counter(m["cycle_uuid"] for m in misses).most_common(6):
    c = cyc.get(cu, {})
    print(f"  {k:4d} misses  at={str(c.get('at'))[:19]} "
          f"outcome={c.get('outcome')} nfc={c.get('nfc')} usable={c.get('usable')}")

# What did that cycle's rss set actually look like? If rss was empty, every
# relevant listing row became a 'miss' -- but only ONE was recorded, which would
# mean the inflation theory is wrong. Check the stored lists directly.
print("\nstored comparison for each unusable miss-bearing cycle:")
for cu in {m["cycle_uuid"] for m, _ in from_unusable}:
    r = con.execute("SELECT details_json, rss_count, listing_count, "
                    "duplicate_count, feed_only_count, listing_only_count, "
                    "relevant_miss_count FROM hdencode_shadow_cycles "
                    "WHERE cycle_uuid=?", (cu,)).fetchone()
    if r is None:
        continue
    print(f"  rss={r['rss_count']} listing={r['listing_count']} "
          f"dup={r['duplicate_count']} feed_only={r['feed_only_count']} "
          f"listing_only={r['listing_only_count']} misses={r['relevant_miss_count']}")
    try:
        d = json.loads(r["details_json"])
        print(f"  details keys: {sorted(d)}")
        print(f"  listing_only stored: {len(d.get('listing_only') or [])} urls; "
              f"feed_only stored: {len(d.get('feed_only') or [])} urls")
    except Exception as e:
        print(f"  details unparseable: {e}")
