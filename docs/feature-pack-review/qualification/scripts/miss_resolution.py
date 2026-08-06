"""Retrospective miss-resolution grading for the RSS shadow window.

Jesse's tiered criterion (2026-07-24): a miss the feed catches up on within
<=6h is GREEN, 6-24h YELLOW (acceptable, non-failing), >24h or never RED.

`hdencode_shadow_misses` records each miss once with no timestamps, so it
cannot answer this alone. Each cycle's details_json stores the full
`feed_only` and `listing_only` URL lists, which supports retrospective
grading -- but ONLY with the three-state model below.

WHAT DISAPPEARANCE ACTUALLY MEANS (peer review, 2026-07-26).
An earlier version of this script treated "the URL left listing_only" as
resolution and claimed it never graded a miss greener than the evidence
supports. That was WRONG. Leaving listing_only has three possible causes:

  1. the feed acquired it -> it moved into the intersection   (resolved)
  2. the listing dropped it after the feed acquired it        (resolved)
  3. the listing dropped it while the feed STILL lacks it     (NOT resolved)

Case 3 is a false resolution, and the intersection is stored only as a COUNT,
so absence from both difference sets cannot distinguish 1/2 from 3. The three
states this data can actually prove are:

  still in listing_only          -> definitely still missing
  later appears in feed_only     -> DEFINITELY resolved by that cycle
  absent from both               -> AMBIGUOUS, cannot close the miss

AMBIGUOUS is reported as its own bucket and must block gate closure, exactly
like PENDING. It is not evidence of failure either -- it is absence of proof.

CYCLE QUALITY. A partial listing fetch, a parser error, or a cycle killed
mid-run can manufacture a false disappearance. Only cycles that completed with
both fetches succeeding are used as absence/resolution observations. (Three
container recreates on 2026-07-26 killed scans mid-cycle, so this is not
hypothetical.)
"""
import json
import sqlite3
import sys
from datetime import datetime

GREEN_H, YELLOW_H = 6.0, 24.0


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

all_cycles, usable = [], []
for r in con.execute(
        "SELECT cycle_uuid, completed_at, outcome, normal_feeds_complete, "
        "rss_requests, listing_requests, details_json "
        "FROM hdencode_shadow_cycles WHERE details_json IS NOT NULL "
        "ORDER BY completed_at"):
    try:
        d = json.loads(r["details_json"])
    except Exception:
        continue
    c = {
        "at": ts(r["completed_at"]),
        "listing_only": set(d.get("listing_only") or ()),
        "feed_only": set(d.get("feed_only") or ()),
        # Mirrors the readiness eligibility rule: a cycle only counts as an
        # observation if both sides actually completed.
        "usable": (r["outcome"] in ("success", "relevant_miss")
                   and r["normal_feeds_complete"] == 1
                   and (r["rss_requests"] or 0) > 0
                   and (r["listing_requests"] or 0) > 0),
    }
    all_cycles.append(c)
    if c["usable"]:
        usable.append(c)

# MISS SOURCING. This JOIN was originally unfiltered, so a miss recorded during
# a cycle this script itself refuses to trust as an OBSERVATION still entered the
# graded population. A first fix filtered on rss_requests>0; a 2026-08-06 peer
# review refuted that proxy, because poll_cycle counts `requested` across
# normal + catch-up feeds and marks a failed attempt as requested. It therefore
# admitted catch-up-only, failed-fetch and half-stale comparisons.
#
# WHAT THIS WINDOW CAN AND CANNOT SUPPORT. Attribution -- the correct rule, now
# live in compare_shadow -- needs to know which normal feed succeeded. Nothing
# recorded that: hdencode_shadow_cycles carried only a cycle-level boolean until
# 2026-08-06. So the 2026-07-22..08-05 window cannot be graded under attribution
# at any level of effort. The evidence to do so was never written.
#
# It is therefore graded CONSERVATIVELY: a miss counts only when both normal
# feeds completed in its cycle. That is stricter for ADMISSION than attribution
# -- a mixed cycle (movies_all changed, tv_all failed) contributes nothing here,
# whereas attribution would admit its valid movie half -- so the figure is a
# LOWER bound on blocking misses.
#
# WHAT THAT DOES AND DOES NOT SUPPORT. It guarantees the grader never FALSELY
# ACCUSES the feed of a miss. It does NOT establish overall health: zero blockers
# in the smaller admitted set says nothing about the larger attribution-valid
# set, because an omitted mixed-cycle row could itself be permanently missing. An
# earlier version of this comment claimed it "can never overstate health", which
# is backwards. The supportable claim is only about the ADMITTED records.
misses = [dict(r) for r in con.execute(
    "SELECT m.canonical_url u, m.title, m.status, s.completed_at at "
    "FROM hdencode_shadow_misses m "
    "JOIN hdencode_shadow_cycles s ON s.cycle_uuid = m.cycle_uuid "
    "WHERE s.normal_feeds_complete = 1")]
excluded = con.execute(
    "SELECT COUNT(*) FROM hdencode_shadow_misses m "
    "JOIN hdencode_shadow_cycles s ON s.cycle_uuid = m.cycle_uuid "
    "WHERE s.normal_feeds_complete != 1").fetchone()[0]

print(f"cycles: {len(all_cycles)} total, {len(usable)} usable as observations "
      f"({len(all_cycles) - len(usable)} rejected: incomplete/partial/killed)")
print(f"recorded misses: {len(misses)} graded, "
      f"{excluded} excluded as recorded during a cycle whose normal feeds "
      f"did not both complete (conservative bound; see MISS SOURCING)")
if len(usable) > 1:
    gaps = sorted((usable[i + 1]["at"] - usable[i]["at"]).total_seconds() / 60
                  for i in range(len(usable) - 1))
    print(f"usable-cycle cadence: median {gaps[len(gaps) // 2]:.0f} min, "
          f"max {gaps[-1]:.0f} min\n")


def classify(url, first_seen):
    """Return (state, hours, detail) using only provable transitions."""
    last_missing = first_seen
    for c in usable:
        if c["at"] <= first_seen:
            continue
        if url in c["listing_only"]:
            last_missing = c["at"]          # still provably missing
        elif url in c["feed_only"]:
            # DEFINITIVE: the feed has it and the listing no longer does.
            return ("resolved", (c["at"] - first_seen).total_seconds() / 3600, "")
        # absent from both -> ambiguous; keep looking for a feed_only sighting

    newest = usable[-1]["at"] if usable else first_seen
    unresolved_h = (newest - first_seen).total_seconds() / 3600
    if last_missing > first_seen:
        # Observed still-missing at a later cycle, then never provably resolved.
        if unresolved_h > YELLOW_H:
            return ("red", unresolved_h, "observed missing, never provably resolved")
        return ("pending", unresolved_h, "still missing, window too short")
    if unresolved_h > YELLOW_H:
        return ("ambiguous", unresolved_h,
                "left listing_only but never seen in feed_only (>24h)")
    return ("ambiguous", unresolved_h,
            "left listing_only but never seen in feed_only (recent)")


buckets = {"green": [], "yellow": [], "red": [], "pending": [], "ambiguous": []}
rows = []
for m in sorted(misses, key=lambda x: x["at"]):
    first = ts(m["at"])
    state, hours, detail = classify(m["u"], first)
    if state == "resolved":
        tier = ("green" if hours <= GREEN_H
                else "yellow" if hours <= YELLOW_H else "red")
    else:
        tier = state
    buckets[tier].append((m["u"], detail))
    rows.append((m["at"][:16], f"{hours:5.1f}", tier, m["title"][:30], detail))

print(f"{'missed at':16} {'hours':>6}  {'tier':10} title")
for at, h, tier, title, detail in rows:
    print(f"{at:16} {h:>6}  {tier.upper():10} {title}")

print("\n=== VERDICT (only provable transitions) ===")
for k in ("green", "yellow", "red", "pending", "ambiguous"):
    print(f"  {k.upper():10}: {len(buckets[k])}")

blocking = len(buckets["pending"]) + len(buckets["ambiguous"])
print(f"\n  gate-blocking (pending + ambiguous): {blocking}")
print(f"  gate-failing  (red)                : {len(buckets['red'])}")
if buckets["ambiguous"]:
    print("\n  AMBIGUOUS -- left listing_only but never observed in feed_only.")
    print("  The intersection stores only a count, so these cannot be closed")
    print("  from current data. Persisting both_urls or a full feed_urls set")
    print("  per cycle would make them provable going forward.")
    for u, d in buckets["ambiguous"][:8]:
        print(f"    {u[-60:]}")
print("\nGATE: closure requires 0 RED, 0 PENDING and 0 AMBIGUOUS.")
print("Continue the observation tail past the nominal window end until every")
print("miss is conclusively classified.")

# --json emits the verdict as one machine-readable line, so the collector can
# act on the GRADED classification instead of a raw miss count. Added
# 2026-08-05 alongside the graded stop condition: the collector stopped on
# `if misses:`, which treats "the feed caught up an hour later" as permanent
# coverage loss. Jesse's tiered rule (2026-07-24) says <=6h is GREEN, and on
# 2026-08-05 the live data was 149 GREEN / 0 YELLOW / 0 RED / 1 AMBIGUOUS --
# so the raw count stopped the window 150 times for 1 unprovable miss.
# Printed LAST and prefixed, so the human report above stays readable.
if "--json" in sys.argv:
    print("JSON_VERDICT " + json.dumps({
        "green": len(buckets["green"]),
        "yellow": len(buckets["yellow"]),
        "red": len(buckets["red"]),
        "pending": len(buckets["pending"]),
        "ambiguous": len(buckets["ambiguous"]),
        "total": sum(len(v) for v in buckets.values()),
        "green_hours": GREEN_H,
        "yellow_hours": YELLOW_H,
        "ambiguous_urls": [u for u, _ in buckets["ambiguous"]],
        "red_urls": [u for u, _ in buckets["red"]],
    }))
