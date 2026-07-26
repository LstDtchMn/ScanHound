"""Retrospective miss-resolution grading for the RSS shadow window.

Jesse's tiered criterion (2026-07-24): a miss that the feed catches up on
within <=6h is GREEN, 6-24h is YELLOW (acceptable, non-failing), >24h or
never is RED.

The hdencode_shadow_misses table records each miss ONCE with no timestamps,
so it cannot answer this alone. But every cycle's details_json stores the
full feed_only and listing_only URL lists, which lets resolution be derived
retrospectively:

  * a URL is MISSING at cycle T if it appears in listing_only at T
    (listing has it, feed does not);
  * it is RESOLVED by cycle T' > T when it stops appearing in listing_only:
    either the feed now has it (it moved to the both/duplicate set, which
    stores only a count, or to feed_only after leaving the listing window),
    or the listing dropped it. The first case is overwhelmingly more likely
    while the item is recent, so this yields an UPPER BOUND on latency;
  * if it appears in listing_only again later, it was still missing then --
    the resolution bound restarts from the last sighting.

Latency is therefore bracketed: > (last missing sighting - first sighting)
and <= (first absent cycle - first sighting). Both bounds are reported and
the TIER is assigned from the upper bound (conservative: never grades a miss
greener than the evidence supports).
"""
import json
import sqlite3
from datetime import datetime, timezone


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

cycles = []
for r in con.execute(
        "SELECT cycle_uuid, completed_at, details_json FROM hdencode_shadow_cycles "
        "WHERE details_json IS NOT NULL ORDER BY completed_at"):
    try:
        d = json.loads(r["details_json"])
    except Exception:
        continue
    cycles.append({
        "uuid": r["cycle_uuid"],
        "at": ts(r["completed_at"]),
        "listing_only": set(d.get("listing_only") or ()),
        "feed_only": set(d.get("feed_only") or ()),
    })

misses = [dict(r) for r in con.execute(
    "SELECT m.canonical_url u, m.title, m.status, s.completed_at at "
    "FROM hdencode_shadow_misses m "
    "JOIN hdencode_shadow_cycles s ON s.cycle_uuid = m.cycle_uuid")]

print(f"cycles with details: {len(cycles)}   recorded misses: {len(misses)}")
if cycles:
    gaps = [(cycles[i + 1]["at"] - cycles[i]["at"]).total_seconds() / 60
            for i in range(len(cycles) - 1)]
    gaps.sort()
    print(f"cycle cadence: median {gaps[len(gaps) // 2]:.0f} min, "
          f"max {gaps[-1]:.0f} min\n")

def grade(url, first_seen):
    """Return (last_missing_at, resolved_bound_at, tier)."""
    last_missing = first_seen
    resolved_at = None
    for c in cycles:
        if c["at"] <= first_seen:
            continue
        if url in c["listing_only"]:
            last_missing = c["at"]          # still missing at this cycle
            resolved_at = None              # any earlier "absent" was a gap
        elif resolved_at is None:
            resolved_at = c["at"]           # first absence after a sighting
    return last_missing, resolved_at


tiers = {"green": [], "yellow": [], "red": [], "pending": []}
rows = []
for m in sorted(misses, key=lambda x: x["at"]):
    first = ts(m["at"])
    last_missing, resolved = grade(m["u"], first)
    if resolved is None:
        upper_h = None
        # Distinguish "observed unresolved over a long span" from "too recent
        # to grade": a miss from the latest cycle has zero subsequent cycles,
        # so calling it RED would fail the window on absence of data. It is
        # PENDING until later cycles exist; if the unresolved span itself
        # exceeds 24h, it is a genuine RED regardless.
        newest = cycles[-1]["at"] if cycles else first
        span_h = (newest - first).total_seconds() / 3600
        tier = "red" if span_h > 24 else "pending"
    else:
        upper_h = (resolved - first).total_seconds() / 3600
        lower_h = max(0.0, (last_missing - first).total_seconds() / 3600)
        tier = "green" if upper_h <= 6 else ("yellow" if upper_h <= 24 else "red")
    tiers[tier].append(m["u"])
    rows.append((m["at"][:16], m["status"], upper_h, tier, m["title"][:34],
                 m["u"].rsplit("/", 1)[-1][:40]))

print(f"{'missed at':16} {'status':14} {'<=h':>6}  {'tier':6}  title")
for at, status, upper, tier, title, slug in rows:
    h = f"{upper:5.1f}" if upper is not None else " neverr"[:6]
    print(f"{at:16} {status:14} {h:>6}  {tier.upper():6}  {title}")

print()
print("=== VERDICT against the tiered criterion ===")
print(f"  GREEN  (<=6h)        : {len(tiers['green'])}")
print(f"  YELLOW (6-24h)       : {len(tiers['yellow'])}")
print(f"  RED    (>24h/never)  : {len(tiers['red'])}")
print(f"  PENDING (too recent) : {len(tiers['pending'])}")
if tiers["red"]:
    print("\n  RED urls:")
    for u in tiers["red"]:
        print("   ", u)
print("\nNOTE: tier uses the UPPER bound of the resolution bracket, so a miss")
print("is never graded greener than the evidence supports. 'never' means the")
print("URL was still in listing_only at its last sighting with no later")
print("absence observed.")
