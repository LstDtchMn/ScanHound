#!/usr/bin/env python3
"""Per-cycle RSS coverage margin, burst volume and outage headroom. READ-ONLY.

Reproducible replacement for the ad-hoc A2 numbers in the Track A package, which
were computed in a throwaway shell command and were therefore prose-only.

THE FLAW THIS FIXES. The published margin combined the CURRENT feed depth from
`hdencode_feed_state` (one row per feed, no history) with the WORST HISTORICAL
poll gap from `hdencode_ingest_cycles`. Those two quantities are not
time-aligned, so "an outage under 9.8 h loses nothing" was never supported: it
assumed today's depth held throughout the window.

HOW DEPTH IS RECOVERED. `hdencode_ingest_cycles` does not store each body's
newest/oldest entry, and the 46 evidence JSONs carry only feed health. But
`hdencode_candidate_feeds` retains complete membership intervals
(first_seen_at .. last_seen_at, populated on all rows), so the body of feed F at
time T can be reconstructed as the members whose interval spans T. Their
pub_date spread is that body's observed depth.

STATED ASSUMPTIONS - this is a reconstruction, not a recording:
  A1. Membership intervals are contiguous: an item present at first_seen and at
      last_seen was present throughout. If an item left a feed and returned, this
      OVERSTATES depth for the gap.
  A2. pub_date is the upstream publication time and is not rewritten.
  A3. Only `changed` ingests represent a fetched body; 304/failed rows carry no
      entries and are excluded from depth but DO count toward the poll gap,
      because a poll that returned nothing new still consumed wall-clock.
  A4. Depth is bounded below by what we ingested; if a body contained items
      ScanHound discarded before persistence, true depth is larger.

Usage:
    python rss_coverage_margin.py --db /path/to/crawler-snapshot.db [--json out.json]
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sqlite3
import sys
from collections import defaultdict

NORMAL_FEEDS = ("movies_all", "tv_all")


def _ts(value):
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="FROZEN snapshot, not the live DB")
    ap.add_argument("--json", help="write machine-readable results here")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    # ── membership intervals + publication times ────────────────────────────
    pub = {}
    for url, pub_date in conn.execute(
        "SELECT canonical_url, pub_date FROM hdencode_candidates"
    ):
        when = _ts(pub_date)
        if when:
            pub[url] = when

    members = defaultdict(list)          # feed -> [(first, last, pub)]
    for feed_key, url, first_seen, last_seen in conn.execute(
        "SELECT feed_key, canonical_url, first_seen_at, last_seen_at "
        "FROM hdencode_candidate_feeds"
    ):
        a, b, p = _ts(first_seen), _ts(last_seen), pub.get(url)
        if a and b and p:
            members[feed_key].append((a, b, p))

    # ── the saturation denominator, stated exactly ─────────────────────────
    saturation = {}
    for feed in NORMAL_FEEDS:
        total = conn.execute(
            "SELECT COUNT(*) FROM hdencode_ingest_cycles WHERE feed_key=?", (feed,)
        ).fetchone()[0]
        ok = conn.execute(
            "SELECT COUNT(*) FROM hdencode_ingest_cycles WHERE feed_key=? AND changed=1",
            (feed,),
        ).fetchone()[0]
        full = conn.execute(
            "SELECT COUNT(*) FROM hdencode_ingest_cycles "
            "WHERE feed_key=? AND changed=1 AND candidate_count=50", (feed,)
        ).fetchone()[0]
        saturation[feed] = {"ingest_rows": total, "successful": ok, "at_50": full}

    report = {"assumptions": ["contiguous membership", "stable pub_date",
                             "changed-only bodies", "depth is a lower bound"],
              "saturation": saturation, "feeds": {}}

    print("SATURATION (exact denominator)")
    for feed, s in saturation.items():
        print(f"  {feed:12} {s['at_50']}/{s['successful']} successful polls returned 50 "
              f"({s['ingest_rows']} ingest rows incl. failures)")

    # ── per-cycle depth, gap and margin ────────────────────────────────────
    for feed in NORMAL_FEEDS:
        cycles = [(_ts(t), n) for t, n in conn.execute(
            "SELECT completed_at, candidate_count FROM hdencode_ingest_cycles "
            "WHERE feed_key=? AND completed_at IS NOT NULL ORDER BY completed_at",
            (feed,))]
        cycles = [(t, n) for t, n in cycles if t]
        rows = members[feed]

        depths, margins, gaps, per_cycle = [], [], [], []
        prev = None
        for when, count in cycles:
            gap = (when - prev).total_seconds() / 3600.0 if prev else None
            prev = when
            live = [p for a, b, p in rows if a <= when <= b]
            depth = ((max(live) - min(live)).total_seconds() / 3600.0
                     if len(live) >= 2 else None)
            margin = (depth - gap) if (depth is not None and gap is not None) else None
            if depth is not None:
                depths.append(depth)
            if gap is not None:
                gaps.append(gap)
            if margin is not None:
                margins.append(margin)
                per_cycle.append({"at": when.isoformat(), "entries": count,
                                  "depth_h": round(depth, 2), "gap_h": round(gap, 2),
                                  "margin_h": round(margin, 2)})

        nonpositive = [m for m in margins if m <= 0]
        worst_gap = max(gaps) if gaps else None
        # Margin AT the longest gap, not depth-now minus gap-then.
        at_worst = next((c["margin_h"] for c in per_cycle
                         if abs(c["gap_h"] - (worst_gap or 0)) < 1e-9), None)

        feed_report = {
            "cycles_with_margin": len(margins),
            "depth_h": {"min": _pct(depths, 0), "p5": _pct(depths, .05),
                        "p50": _pct(depths, .5), "p95": _pct(depths, .95),
                        "max": _pct(depths, 1)},
            "gap_h": {"p50": _pct(gaps, .5), "p95": _pct(gaps, .95),
                      "max": round(worst_gap, 2) if worst_gap else None},
            "margin_h": {"min": _pct(margins, 0), "p5": _pct(margins, .05),
                         "p50": _pct(margins, .5), "p95": _pct(margins, .95)},
            "nonpositive_margin_cycles": len(nonpositive),
            "margin_at_longest_gap_h": at_worst,
            "outage_headroom_h": _pct(depths, 0),
        }
        report["feeds"][feed] = feed_report

        print(f"\n{feed}")
        print(f"  cycles measured        : {len(margins)}")
        d = feed_report["depth_h"]
        print(f"  depth  min/p5/p50/max  : {d['min']} / {d['p5']} / {d['p50']} / {d['max']} h")
        g = feed_report["gap_h"]
        print(f"  gap    p50/p95/max     : {g['p50']} / {g['p95']} / {g['max']} h")
        m = feed_report["margin_h"]
        print(f"  MARGIN min/p5/p50      : {m['min']} / {m['p5']} / {m['p50']} h")
        print(f"  cycles with margin <= 0: {len(nonpositive)}")
        print(f"  margin at longest gap  : {at_worst} h")
        print(f"  OUTAGE HEADROOM        : {feed_report['outage_headroom_h']} h "
              f"(the MINIMUM observed depth, not the current one)")

    # ── busiest publication burst, which sizes the sweep ───────────────────
    print("\nPUBLICATION BURST (sizes the hybrid sweep)")
    all_pub = sorted(pub.values())
    worst6, worst6_at = 0, None
    for i, start in enumerate(all_pub):
        j = bisect.bisect_right(all_pub, start + dt.timedelta(hours=6))
        if j - i > worst6:
            worst6, worst6_at = j - i, start
    report["burst"] = {"max_items_per_6h": worst6,
                       "window_start": worst6_at.isoformat() if worst6_at else None,
                       "total_publications": len(all_pub)}
    print(f"  busiest rolling 6 h    : {worst6} releases "
          f"(from {worst6_at.isoformat()[:16] if worst6_at else '?'})")
    print(f"  total publications     : {len(all_pub)}")
    print(f"  -> a page-1-only sweep must hold >= {worst6} items to be safe at 6 h")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
