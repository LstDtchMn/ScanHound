"""Which eligibility filter should the miss sum use? Three candidates, measured.

Context. On 2026-07-21 a ChatGPT adversarial audit added the rule "a relevant
miss blocks readiness even when the cycle was incomplete" (commit f5e3c6e,
test_relevant_miss_blocks_even_when_cycle_is_incomplete). The intent was sound:
a degraded cycle must not be able to HIDE a real miss. What it could not
anticipate -- the window was 0 days old -- is that a degraded cycle can also
MANUFACTURE one, because compare_shadow compares the listing against the last
persisted feed snapshot when the feed did not fetch.

Candidates:
  A  status quo          every row counts
  B  fetch-only          rss_requests > 0        (narrow: the feed did fetch)
  C  full eligibility    the same filter cycles/duration/reduction already use

B preserves ChatGPT's rule for cycles that fetched but were otherwise degraded.
C treats the comparison basis as sound only when both normal feeds completed.
This measures what each admits, and grades the difference.

Read-only.
"""
import json
import sqlite3
from collections import Counter
from datetime import datetime

con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
GREEN_H, YELLOW_H = 6.0, 24.0


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


rows = list(con.execute(
    "SELECT cycle_uuid, completed_at, outcome, normal_feeds_complete,"
    " rss_requests, listing_requests, rss_count, duplicate_count,"
    " relevant_miss_count, details_json FROM hdencode_shadow_cycles"
    " ORDER BY completed_at"))

cyc = {}
for r in rows:
    d = {}
    try:
        d = json.loads(r["details_json"] or "{}")
    except Exception:
        pass
    cyc[r["cycle_uuid"]] = {
        "r": r,
        "A": True,
        "B": (r["rss_requests"] or 0) > 0,
        "C": (r["outcome"] in ("success", "relevant_miss")
              and r["normal_feeds_complete"] == 1
              and (r["rss_requests"] or 0) > 0
              and (r["listing_requests"] or 0) > 0),
        "lo": set(d.get("listing_only") or ()),
        "fo": set(d.get("feed_only") or ()),
    }

# Resolution observations always use the strict set -- that is not in question.
obs = sorted(((ts(c["r"]["completed_at"]), c) for c in cyc.values() if c["C"]),
             key=lambda x: x[0])


def classify(url, first):
    last_missing = first
    for at, c in obs:
        if at <= first:
            continue
        if url in c["lo"]:
            last_missing = at
        elif url in c["fo"]:
            return ("resolved", (at - first).total_seconds() / 3600)
    newest = obs[-1][0] if obs else first
    h = (newest - first).total_seconds() / 3600
    if last_missing > first:
        return (("red" if h > YELLOW_H else "pending"), h)
    return ("ambiguous", h)


misses = [dict(x) for x in con.execute(
    "SELECT m.canonical_url u, m.title, m.status, m.cycle_uuid,"
    " s.completed_at at FROM hdencode_shadow_misses m"
    " JOIN hdencode_shadow_cycles s ON s.cycle_uuid = m.cycle_uuid")]

print(f"{'filter':22s} {'cycles':>7s} {'misses':>7s}  grading")
print("-" * 74)
results = {}
for key, label in (("A", "A status quo"), ("B", "B rss_requests>0"),
                   ("C", "C full eligibility")):
    admitted = [m for m in misses if cyc[m["cycle_uuid"]][key]]
    ncyc = sum(1 for c in cyc.values() if c[key])
    b = Counter()
    for m in admitted:
        st, h = classify(m["u"], ts(m["at"]))
        b[("green" if h <= GREEN_H else "yellow" if h <= YELLOW_H else "red")
          if st == "resolved" else st] += 1
    results[key] = (admitted, b)
    blockers = b["red"] + b["pending"] + b["ambiguous"]
    verdict = "PASSES" if blockers == 0 else f"STOPS ({blockers})"
    print(f"{label:22s} {ncyc:>7d} {len(admitted):>7d}  "
          f"G{b['green']} Y{b['yellow']} R{b['red']} "
          f"P{b['pending']} A{b['ambiguous']}  -> {verdict}")

print("\nwhat B admits that C does not (the contested rows):")
onlyB = [m for m in misses
         if cyc[m["cycle_uuid"]]["B"] and not cyc[m["cycle_uuid"]]["C"]]
for m in onlyB:
    c = cyc[m["cycle_uuid"]]["r"]
    st, h = classify(m["u"], ts(m["at"]))
    print(f"  {c['completed_at'][:19]}  nfc={c['normal_feeds_complete']} "
          f"rss_req={c['rss_requests']} rss_count={c['rss_count']} "
          f"dup={c['duplicate_count']}")
    print(f"     {m['u'][-64:]}")
    print(f"     grades {st.upper()} at {h:.2f}h")
if not onlyB:
    print("  (none)")

print("\nwhat A admits that B does not:")
onlyA = [m for m in misses if not cyc[m["cycle_uuid"]]["B"]]
g = Counter()
for m in onlyA:
    st, h = classify(m["u"], ts(m["at"]))
    g[("green" if h <= GREEN_H else "yellow" if h <= YELLOW_H else "red")
      if st == "resolved" else st] += 1
print(f"  {len(onlyA)} misses, all from zero-fetch cycles: {dict(g)}")
