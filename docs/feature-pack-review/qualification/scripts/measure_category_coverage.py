"""How many cached listing rows carry an explicit crawl category?

WHY THIS EXISTS. Attribution treats `category` as the authoritative signal and
falls back to "unknown" when nothing affirmative is present. That is only safe if
categories are actually populated in practice: if most rows lacked one, "unknown"
would become the common case and requiring BOTH feeds would suppress far more
than intended -- the mirror image of the defect it fixed.

I reported this distribution in the Round 3 review request as prose. The reviewer
correctly treated the figures as author-attested rather than repository-verifiable,
because nothing on the branch could produce them. This script is the correction:
the claim is now executable.

Reads `background_scan_cache`, which stores the scanner's own payload per cached
listing row. Emits JSON to stdout. Read-only; no URLs or titles are emitted.

    python measure_category_coverage.py [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

# Assigned at source construction in backend/scanner_service.py.
TV_CATEGORIES = ("tv",)
MOVIE_CATEGORIES = ("4k", "remux")
NO_EVIDENCE_CATEGORIES = ("search",)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/dbvol/crawler.db")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    total = con.execute("SELECT COUNT(*) FROM background_scan_cache").fetchone()[0]
    categories = Counter()
    season_set = 0
    episodes_set = 0
    unparseable = 0
    tv_category_without_season = 0
    season_without_tv_category = 0

    for (payload,) in con.execute("SELECT data FROM background_scan_cache"):
        try:
            row = json.loads(payload or "{}")
        except Exception:
            unparseable += 1
            continue
        category = str(row.get("category") or "").strip().lower()
        categories[category or "(absent)"] += 1
        has_season = row.get("season") is not None
        if has_season:
            season_set += 1
        if row.get("episodes"):
            episodes_set += 1
        # Cross-check the two independent TV signals against each other. Large
        # disagreement would undermine treating either as authoritative.
        if category in TV_CATEGORIES and not has_season:
            tv_category_without_season += 1
        if has_season and category not in TV_CATEGORIES:
            season_without_tv_category += 1

    def classify(category):
        if category in TV_CATEGORIES:
            return "tv_evidence"
        if category in MOVIE_CATEGORIES:
            return "movie_evidence"
        if category in NO_EVIDENCE_CATEGORIES:
            return "no_evidence_by_design"
        return "no_evidence_unrecognised"

    buckets = Counter()
    for category, count in categories.items():
        key = "(absent)" if category == "(absent)" else category
        buckets["absent" if key == "(absent)" else classify(key)] += count

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": args.db,
        "table": "background_scan_cache",
        "rows": total,
        "unparseable_payloads": unparseable,
        "by_category": dict(sorted(categories.items(),
                                   key=lambda kv: -kv[1])),
        "by_attribution_effect": dict(buckets),
        "structured_signals": {
            "season_set": season_set,
            "episodes_set": episodes_set,
        },
        "cross_checks": {
            "tv_category_without_season": tv_category_without_season,
            "season_without_tv_category": season_without_tv_category,
            "note": ("The two TV signals are independent. Close agreement "
                     "supports treating either as authoritative; wide "
                     "disagreement would not."),
        },
        "interpretation": {
            "unknown_is_rare_if": "absent + no_evidence_unrecognised is small",
            "caveat": ("This measures rows the scanner has CACHED, not the miss "
                       "corpus. Historical miss rows predate the category field "
                       "entirely, which is why the retrospective window is "
                       "bounded conservatively instead of attributed."),
        },
    }
    json.dump(out, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
