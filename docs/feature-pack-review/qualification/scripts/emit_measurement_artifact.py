"""Emit the RSS window measurement as a machine-readable artifact.

The 2026-08-06 peer review asked for this directly (Finding 5 and the closure
list): commit executable queries and machine-readable output including
denominators, so the numeric claims are reproducible from the branch rather than
attested in prose. It also asked that reproduction scripts be run against a fresh
schema, not only the production snapshot.

Writes JSON to stdout. Every count carries its denominator and the exact
predicate that produced it, so a reviewer can see what was and was not included
without reading the surrounding narrative.

Read-only. No production data beyond aggregate counts and the URLs of records
that BLOCK -- so a redacted run can be shared without publishing the corpus.

    python emit_measurement_artifact.py [--db PATH] [--include-urls]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime

GREEN_H, YELLOW_H = 6.0, 24.0
VALID = ("changed", "not_modified")


def ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/dbvol/crawler.db")
    ap.add_argument("--include-urls", action="store_true",
                    help="include the URLs of blocking records (RED/PENDING/"
                         "AMBIGUOUS). Off by default so the artifact can be "
                         "shared without publishing the corpus.")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    cols = {r[1] for r in con.execute("PRAGMA table_info(hdencode_shadow_cycles)")}
    has_provenance = "normal_feed_outcomes" in cols

    rows = list(con.execute(
        "SELECT cycle_uuid, completed_at, outcome, normal_feeds_complete,"
        " rss_requests, listing_requests, relevant_miss_count, details_json"
        + (", normal_feed_outcomes" if has_provenance else "")
        + " FROM hdencode_shadow_cycles ORDER BY completed_at"))

    def eligible(r):
        return (r["outcome"] in ("success", "relevant_miss")
                and r["normal_feeds_complete"] == 1
                and (r["rss_requests"] or 0) > 0
                and (r["listing_requests"] or 0) > 0)

    elig = [r for r in rows if eligible(r)]

    # Observation set for resolution: strict, and not in question.
    cyc = {}
    for r in rows:
        try:
            d = json.loads(r["details_json"] or "{}")
        except Exception:
            d = {}
        cyc[r["cycle_uuid"]] = {
            "at": r["completed_at"],
            "lo": set(d.get("listing_only") or ()),
            "fo": set(d.get("feed_only") or ()),
            "eligible": eligible(r),
            "complete": r["normal_feeds_complete"] == 1,
            "provenance": (r["normal_feed_outcomes"] if has_provenance else None),
        }
    obs = sorted(((ts(c["at"]), c) for c in cyc.values() if c["eligible"]),
                 key=lambda x: x[0])

    def classify(url, first):
        last_missing = first
        for at, c in obs:
            if at <= first:
                continue
            if url in c["lo"]:
                last_missing = at
            elif url in c["fo"]:
                return "resolved", (at - first).total_seconds() / 3600
        newest = obs[-1][0] if obs else first
        h = (newest - first).total_seconds() / 3600
        if last_missing > first:
            return ("red" if h > YELLOW_H else "pending"), h
        return "ambiguous", h

    all_misses = [dict(r) for r in con.execute(
        "SELECT m.canonical_url u, m.status, m.cycle_uuid,"
        + (" m.media_type," if "media_type" in
           {c[1] for c in con.execute("PRAGMA table_info(hdencode_shadow_misses)")}
           else " NULL AS media_type,")
        + " s.completed_at at, s.normal_feeds_complete complete"
        " FROM hdencode_shadow_misses m"
        " JOIN hdencode_shadow_cycles s ON s.cycle_uuid = m.cycle_uuid")]

    def grade(population):
        buckets, hours, blocking = Counter(), [], []
        for m in population:
            state, h = classify(m["u"], ts(m["at"]))
            tier = (("green" if h <= GREEN_H else
                     "yellow" if h <= YELLOW_H else "red")
                    if state == "resolved" else state)
            buckets[tier] += 1
            if state == "resolved":
                hours.append(h)
            else:
                blocking.append({"tier": tier, "hours": round(h, 2),
                                 **({"url": m["u"]} if args.include_urls else {})})
        hs = sorted(hours)
        latency = None
        if hs:
            latency = {
                "n": len(hs),
                "median_hours": round(hs[len(hs) // 2], 3),
                "min_hours": round(hs[0], 3),
                "max_hours": round(hs[-1], 3),
                "within_1h": sum(1 for h in hs if h <= 1),
                "within_2h": sum(1 for h in hs if h <= 2),
                "within_6h": sum(1 for h in hs if h <= 6),
            }
        return {
            "denominator": len(population),
            "tiers": {k: buckets[k] for k in
                      ("green", "yellow", "red", "pending", "ambiguous")},
            "blocking_total": buckets["red"] + buckets["pending"] + buckets["ambiguous"],
            "catch_up_latency": latency,
            "blocking_records": blocking,
        }

    conservative = [m for m in all_misses if m["complete"] == 1]

    er = sum(r["rss_requests"] or 0 for r in elig)
    el = sum(r["listing_requests"] or 0 for r in elig)

    artifact = {
        "generated_from": args.db,
        "schema": {
            "has_per_feed_provenance": has_provenance,
            "note": ("Attribution requires per-feed provenance. Where this is "
                     "false, every row predates it and the window can only be "
                     "bounded conservatively -- attribution is not recoverable "
                     "at any level of effort, because the evidence was never "
                     "written."),
        },
        "window": {
            "cycles_total": len(rows),
            "cycles_eligible": len(elig),
            "eligibility_predicate": ("outcome IN ('success','relevant_miss') AND "
                                      "normal_feeds_complete=1 AND rss_requests>0 "
                                      "AND listing_requests>0"),
            "first_eligible": elig[0]["completed_at"] if elig else None,
            "last_eligible": elig[-1]["completed_at"] if elig else None,
            "observed_days": (round((ts(elig[-1]["completed_at"])
                                     - ts(elig[0]["completed_at"])).total_seconds()
                                    / 86400, 3) if len(elig) > 1 else 0),
            "required_cycles": 20,
            "required_days": 7,
        },
        "request_reduction": {
            "feed_requests": er,
            "listing_requests": el,
            "reduction_pct": round(100.0 * (el - er) / el, 2) if el else None,
            "requests_avoided": el - er,
            "population": "eligible cycles only",
        },
        "miss_populations": {
            "all_recorded": {
                "denominator": len(all_misses),
                "predicate": "every row in hdencode_shadow_misses",
                "note": ("Includes rows whose comparison cannot support a miss "
                         "claim. Reported for completeness, not as evidence."),
            },
            "conservative_bound": {
                "predicate": "source cycle normal_feeds_complete = 1",
                "note": ("Strictly stricter than attribution: a mixed cycle "
                         "(movies_all changed, tv_all failed) contributes "
                         "nothing here, whereas attribution would admit its "
                         "valid movie half. A lower bound on blocking misses; "
                         "cannot overstate health."),
                **grade(conservative),
            },
        },
        "status_mix": dict(Counter(m["status"] for m in all_misses)),
        "grading_rule": {"green_hours": GREEN_H, "yellow_hours": YELLOW_H,
                         "source": "Jesse's tiered criterion, 2026-07-24"},
        "resolution_evidence": {
            "rule": ("a miss is resolved only when its canonical URL later "
                     "appears in feed_only during an ELIGIBLE cycle"),
            "limitation": ("proves the feed had acquired the URL by that later "
                           "cycle; it does NOT prove the original comparison "
                           "was valid. Peer review, 2026-08-06, Finding 3."),
            "observation_cycles": len(obs),
        },
    }
    json.dump(artifact, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
