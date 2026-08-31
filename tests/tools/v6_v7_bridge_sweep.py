"""V6/V7 bridge sweep: does the API answer what the matcher answers?

TEMPORARY. Delete with the bridge, when the canonical media-type state becomes
the sole reader and writer of the verdict
(docs/design/2026-08-31-media-type-authority-model.md, Phase B).

THE MEASUREMENT. For every reachable ``background_scan_cache`` row, compare:

    API      what ``backend.api.routes.results`` serves as ``media_type``
             (post-bridge: a copy normalised through the effective cache
             reader; pre-bridge: the raw stored blob)
    MATCHER  ``backend.scanner_service.cached_media_type(row)[0]``, which is
             what ``_match_against_plex`` ends up branching on

Two bases, both from ``tests/tools/reachable_rows.py`` -- the design lane's
enumeration, not a second one:

    LISTING  the 12 listing inputs (3 categories x fresh-detail x conflict) at
             the neutral title. This is the basis the round-6 design request
             reported V7 on: "3 of 12 reachable listing rows".
    CLOSURE  ``reachable_rows()``: those 12 with all three titles, plus the
             legacy corpus shape, closed under rescan and under the out-of-band
             ``mark_scan_category_conflict``. 77 rows.

DEFECT INJECTION, so the numbers are a before AND an after from one head rather
than a comparison across commits. Neither injection restates production logic:

    --defect v6   feed ``resolve_listing_media_type`` a post dict with the
                  conflict bit cleared. That is EXACTLY the input the pre-fix
                  function saw, because suppressing the route is the only use
                  the bit has.
    --defect v7   read the raw blob instead of calling
                  ``results._normalize_cached_row`` -- literally what
                  ``_load_cached_items`` did before the bridge.
    --defect both the state at d04ab63.

MEASURED AT THIS HEAD, all four configurations::

    --defect both   listing  3 / 12      closure 37 / 77   (3 stored, 34 legacy)
    --defect v7     listing  0 / 12      closure 37 / 77   (3 stored, 34 legacy)
    --defect v6     listing  0 / 12      closure  0 / 77
    (no defect)     listing  0 / 12      closure  0 / 77

``--defect both`` is d04ab63 and reproduces the round-6 figure exactly: 3 of 12.

READ THE ``--defect v6`` ROW CAREFULLY -- it is the reviewer's argument, in
numbers. With only V7 fixed, this metric reports ZERO while the listing writer
is still manufacturing the contradiction on every conflicted row: read-side
normalisation makes the API agree with the matcher by DISCARDING the stored
verdict, so the disagreement stops being observable here. That is why V6 cannot
be deferred and why the writer has its own instrument --
``tests/test_v6_listing_writer_reads_the_conflict.py`` compares the row to
ITSELF (stored verdict vs effective read of the same row) and does see it.

Symmetrically the ``--defect v7`` row shows V6 alone is not enough: the listing
basis is clean because the writer is now correct, but the closure still has 3
stored-verdict disagreements, which are exactly the rows that acquired their
conflict OUT OF BAND via ``mark_scan_category_conflict`` after being written.
Neither fix subsumes the other.

The 34 legacy-row disagreements are not V7. A pre-#93 row stores no verdict at
all, so the raw blob had nothing to serve while the matcher reconstructs one
from the cached category/title/season. The bridge closes those too, because it
normalises every cached row rather than only conflicted ones.

Usage::

    python tests/tools/v6_v7_bridge_sweep.py                # assert 0 / 0
    python tests/tools/v6_v7_bridge_sweep.py --defect both  # the before
    python tests/tools/v6_v7_bridge_sweep.py --json

Exits nonzero when a run with NO defect injected finds any disagreement.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from unittest.mock import patch

# Derived from this file so the harness runs anywhere -- a throwaway container,
# a developer checkout, a CI runner. Run directly, sys.path[0] is tests/tools,
# not the repo root; pytest's conftest does this for the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.api.routes import results as results_route  # noqa: E402
from backend.scanner_service import cached_media_type  # noqa: E402
from tests.tools import reachable_rows as RR  # noqa: E402

NEUTRAL_TITLE = RR._TITLES[0]


def listing_basis():
    """The 12 rows the round-6 design request measured V7 on.

    ``reachable_rows._listing_rows`` with the title dimension pinned to the
    silent title -- 3 categories x fresh-detail x conflict. The title is pinned
    rather than dropped because a title carrying TV evidence decides the row by
    itself and hides the route question this basis is about.
    """
    rows = []
    for category, detail_is_tv, conflict in itertools.product(
            RR._CATEGORY_TYPE, (False, True), (False, True)):
        post = {"type": RR._CATEGORY_TYPE[category], "title": NEUTRAL_TITLE,
                "category": category, "category_conflict": conflict}
        verdict = RR.resolve_listing_media_type(post, {"is_tv": detail_is_tv})
        rows.append(RR._persist(verdict, {
            "title": NEUTRAL_TITLE, "category": category,
            "season": 3 if detail_is_tv else None,
            "category_conflict": conflict, "category_attested": True,
        }))
    return rows


# Bound ONCE, before any patching, so the injection below cannot recurse into
# itself when it is installed under the same module attribute.
_REAL_RESOLVE_LISTING = RR.resolve_listing_media_type


def _route_blind(post_info, details):
    """The pre-fix ``resolve_listing_media_type``, expressed as the production
    function fed the input it used to see."""
    return _REAL_RESOLVE_LISTING(
        dict(post_info, category_conflict=False), details)


def _api_media_type(row, *, raw):
    if raw:
        # What _load_cached_items served before the bridge: the stored blob.
        stored = row.get("media_type")
        return stored if stored in ("tv", "movie", "ambiguous") else "ambiguous"
    return results_route._normalize_cached_row(dict(row))["media_type"]


def measure(*, defect_v6=False, defect_v7=False):
    """{basis: {'total': n, 'disagreements': [...]}}"""
    if defect_v6:
        ctx = patch.object(RR, "resolve_listing_media_type", _route_blind)
    else:
        ctx = patch.object(RR, "resolve_listing_media_type",
                           RR.resolve_listing_media_type)
    with ctx:
        bases = {"listing": listing_basis(), "closure": RR._reachable_rows()}

    out = {}
    for name, rows in bases.items():
        bad = []
        for row in rows:
            api = _api_media_type(row, raw=defect_v7)
            matcher = cached_media_type(row)[0]
            if api != matcher:
                bad.append({"api": api, "matcher": matcher,
                            # A LEGACY row stores no verdict at all, so the raw
                            # blob had nothing to serve; a CURRENT row stores
                            # one that has gone stale. Both are the API and the
                            # matcher answering differently, but only the
                            # second is V6/V7 as reported.
                            "shape": ("current" if row.get("media_type")
                                      else "legacy"),
                            "category": row.get("category"),
                            "conflict": row.get("category_conflict"),
                            "stored": row.get("media_type"),
                            "provisional": row.get("media_type_provisional"),
                            "season": row.get("season"),
                            "is_tv": row.get("is_tv"),
                            "title": row.get("title")})
        out[name] = {"total": len(rows), "disagreements": bad}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--defect", choices=("none", "v6", "v7", "both"),
                    default="none")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = measure(defect_v6=args.defect in ("v6", "both"),
                     defect_v7=args.defect in ("v7", "both"))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("defect injected: %s" % args.defect)
        for name in ("listing", "closure"):
            r = result[name]
            cur = sum(1 for d in r["disagreements"] if d["shape"] == "current")
            leg = len(r["disagreements"]) - cur
            print("  %-8s %d / %d API-vs-matcher disagreements "
                  "(%d on rows that store a verdict, %d on legacy rows)"
                  % (name, len(r["disagreements"]), r["total"], cur, leg))
            for d in r["disagreements"][:6]:
                print("      api=%-9s matcher=%-9s cat=%-6s conflict=%-5s "
                      "stored=%-9s season=%s"
                      % (d["api"], d["matcher"], d["category"], d["conflict"],
                         d["stored"], d["season"]))
            if len(r["disagreements"]) > 6:
                print("      ... %d more" % (len(r["disagreements"]) - 6))

    if args.defect == "none":
        bad = sum(len(r["disagreements"]) for r in result.values())
        if bad:
            print("FAIL: %d disagreements with no defect injected" % bad)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
