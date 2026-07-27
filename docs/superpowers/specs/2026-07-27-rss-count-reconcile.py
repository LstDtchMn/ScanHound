"""Reconcile 28 (sum of cycle relevant_miss_count) vs 29 (rows in the miss table),
and re-classify under the STRUCTURAL eligibility rule rather than rss_requests=0.
"""
import json, sqlite3, datetime

con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

cyc = [dict(r) for r in con.execute(
    "SELECT * FROM hdencode_shadow_cycles ORDER BY completed_at ASC")]
mis = [dict(r) for r in con.execute("SELECT * FROM hdencode_shadow_misses")]

s = con.execute("SELECT SUM(relevant_miss_count) AS s FROM hdencode_shadow_cycles").fetchone()["s"]
print("readiness summary  SUM(relevant_miss_count) :", s)
print("miss-table rows                             :", len(mis))
print("distinct canonical_url in miss table        :", len({m['canonical_url'] for m in mis}))
print("distinct (cycle_uuid, url) pairs            :", len({(m['cycle_uuid'], m['canonical_url']) for m in mis}))
print()

by_uuid = {c["cycle_uuid"]: c for c in cyc}
from collections import Counter
per_cycle_rows = Counter(m["cycle_uuid"] for m in mis)
print("cycles whose relevant_miss_count != miss rows:")
mismatch = 0
for c in cyc:
    n_rows = per_cycle_rows.get(c["cycle_uuid"], 0)
    n_col = c["relevant_miss_count"] or 0
    if n_rows != n_col:
        mismatch += 1
        print("   %s  col=%s rows=%s  outcome=%s feeds_ok=%s rss_req=%s list_req=%s" % (
            (c["completed_at"] or "?")[:19], n_col, n_rows,
            c["outcome"], c["normal_feeds_complete"], c["rss_requests"], c["listing_requests"]))
print("   (mismatching cycles: %d)" % mismatch)

def eligible(c):
    return (c and c.get("outcome") in ("success", "relevant_miss")
            and c.get("normal_feeds_complete") == 1
            and (c.get("rss_requests") or 0) > 0
            and (c.get("listing_requests") or 0) > 0)

print()
print("=== artifact rule comparison ===")
naive = sum(1 for m in mis if (by_uuid.get(m["cycle_uuid"]) or {}).get("rss_requests", 0) == 0)
struct = sum(1 for m in mis if not eligible(by_uuid.get(m["cycle_uuid"])))
print("  excluded by rss_requests==0 only  :", naive)
print("  excluded by STRUCTURAL eligibility:", struct)
print("  difference (missed by naive rule) :", struct - naive)
for m in mis:
    c = by_uuid.get(m["cycle_uuid"])
    if c and not eligible(c) and (c.get("rss_requests") or 0) != 0:
        print("     extra: %s  outcome=%s feeds_ok=%s rss=%s list=%s" % (
            (m["title"] or "?")[:34], c["outcome"], c["normal_feeds_complete"],
            c["rss_requests"], c["listing_requests"]))

print()
print("=== can 'duplicate' hide a later acquisition? ===")
dup_total = sum((c["duplicate_count"] or 0) for c in cyc if eligible(c))
print("  total duplicate_count over eligible cycles:", dup_total)
has_urls = 0
for c in cyc[:400]:
    try:
        d = json.loads(c.get("details_json") or "{}")
    except Exception:
        continue
    if any(k for k in d.keys() if 'dup' in k.lower() or k in ('rss_urls', 'all_rss')):
        has_urls += 1
print("  cycles storing a duplicate/rss URL set     :", has_urls, " <- 0 means absence-from-feed_only is UNPROVABLE")
ks = set()
for c in cyc:
    try:
        ks |= set(json.loads(c.get("details_json") or "{}").keys())
    except Exception:
        pass
print("  keys present in details_json               :", sorted(ks))
