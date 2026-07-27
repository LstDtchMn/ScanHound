"""Per-miss state sequence using BOTH stored sets.

  in listing_only  => listing has it AND rss does NOT  -> POSITIVE proof of RSS absence
  in feed_only     => rss has it AND listing does not  -> POSITIVE proof of RSS presence
  in neither       => ambiguous: duplicate (both have it) OR both dropped it

So absence-from-feed_only alone is weak (ChatGPT is right), but presence-in-
listing_only is strong. This measures how much of each miss's life is covered
by a POSITIVE observation rather than by the ambiguous state.
"""
import json, sqlite3, datetime

con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cycles = [dict(r) for r in con.execute(
    "SELECT * FROM hdencode_shadow_cycles ORDER BY completed_at ASC")]
misses = [dict(r) for r in con.execute(
    "SELECT rowid AS rid, * FROM hdencode_shadow_misses ORDER BY rowid ASC")]
by_uuid = {c["cycle_uuid"]: c for c in cycles}

def ts(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None

def eligible(c):
    return (c and c.get("outcome") in ("success", "relevant_miss")
            and c.get("normal_feeds_complete") == 1
            and (c.get("rss_requests") or 0) > 0
            and (c.get("listing_requests") or 0) > 0)

def sets_of(c):
    try:
        d = json.loads(c.get("details_json") or "{}")
    except Exception:
        return set(), set()
    def norm(x):
        return {i if isinstance(i, str) else (i.get("url") or i.get("canonical_url") or "")
                for i in (x or [])}
    return norm(d.get("listing_only")), norm(d.get("feed_only"))

use = [c for c in cycles if eligible(c)]
seq = [(ts(c["completed_at"]), *sets_of(c)) for c in use]
seq = [s for s in seq if s[0]]
now = datetime.datetime.now(datetime.timezone.utc)
SLO = 6.0

print("eligible cycles: %d   ineligible: %d" % (len(use), len(cycles) - len(use)))
print()
print("%-3s %-30s %-13s %-9s %-8s %s" % ("#", "release", "recorded", "confirmed", "latency", "state"))
print("-" * 88)

from collections import Counter
tally = Counter()
lat = []
for n, m in enumerate(misses, 1):
    url = m["canonical_url"] or ""
    rec = by_uuid.get(m["cycle_uuid"])
    if not eligible(rec):
        tally["artifact"] += 1
        print("%-3d %-30s %-13s %-9s %-8s %s" % (
            n, (m["title"] or "?")[:30], "-", "-", "-", "artifact"))
        continue
    t0 = ts(rec["completed_at"])
    # walk forward
    arrive = None          # first positive RSS-present observation
    last_absent = t0       # last positive RSS-absent observation
    for t, lo, fo in seq:
        if t <= t0:
            continue
        if url in fo:
            arrive = t
            break
        if url in lo:
            last_absent = t
    if arrive is not None:
        h = (arrive - t0).total_seconds() / 3600.0
        lat.append(h)
        state = "recovered_on_time" if h <= SLO else "recovered_late"
        conf = "arrived"
        shown = "%.1fh" % h
    else:
        absent_h = (last_absent - t0).total_seconds() / 3600.0
        age = (now - t0).total_seconds() / 3600.0
        # still positively confirmed absent recently? -> pending/unrecovered
        # vanished from both sets without ever arriving -> evidence_gap
        vanished = (now - last_absent).total_seconds() / 3600.0
        if absent_h >= SLO:
            state = "unrecovered"
        elif vanished > 1.5 and absent_h < SLO:
            state = "evidence_gap"
        else:
            state = "pending" if age <= SLO else "unrecovered"
        conf = "absent %.1fh" % absent_h
        shown = "-"
    tally[state] += 1
    print("%-3d %-30s %-13s %-9s %-8s %s" % (
        n, (m["title"] or "?")[:30], t0.strftime("%m-%d %H:%M"), conf, shown, state))

print()
print("=== six-state tally (SLO = %.1f h) ===" % SLO)
for k in ("recovered_on_time", "recovered_late", "pending",
          "unrecovered", "evidence_gap", "artifact"):
    print("  %-18s : %d" % (k, tally.get(k, 0)))
if lat:
    lat.sort()
    print()
    print("  latency min/median/max: %.1fh / %.1fh / %.1fh" % (
        lat[0], lat[len(lat)//2], lat[-1]))
    print("  over 6h : %d      over 24h : %d" % (
        sum(1 for x in lat if x > 6.0), sum(1 for x in lat if x > 24.0)))
print()
blocking = sum(tally.get(k, 0) for k in
               ("recovered_late", "pending", "unrecovered", "evidence_gap"))
print("  BLOCKING under ChatGPT's gate (late+pending+unrecovered+gap): %d" % blocking)
print("  -> promotion allowed only if this is 0")
