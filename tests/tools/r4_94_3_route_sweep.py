"""R4-94-3 blast-radius / ordering sweep over the REAL /scan/rescan-item route.

The R4-94-3 claims are about ORDER, and about how much else moves. Both are
measurements, so they are made here rather than argued in a commit message.

For every cached-row shape in the product below, the route is driven TWICE, in
two orders:

    PRE   the row already records ``category_conflict`` when the first rescan
          runs -- the shape every conflict test on this branch used;
    POST  the row records no conflict; the PRODUCTION writer
          ``mark_scan_category_conflict`` records it BETWEEN the two rescans,
          which is what happens to a release the crawl SKIPS as already cached.

The final row is the same either way, so the second rescan's answer must be the
same either way. At 1965399 it was not, for 30 of 384 shapes: that is C1.

Four properties are asserted, and the script exits nonzero if any fails:

    1. ORDER INDEPENDENCE   pre and post must agree on the final answer.
    2. ATTESTATION KEPT     an attested row must still be attested afterwards
                            (C4).
    3. INVARIANT            is_tv is (media_type == 'tv') on every item the
                            route returns (the L3 invariant, re-checked here
                            over 1536 steps rather than nine rows).
    4. NO VERDICT MOVES ON UNCONFLICTED ROWS
                            every step whose verdict differs from the recorded
                            baseline must be a row recording a conflict. This
                            is the blast-radius bound: run with --baseline to
                            write a baseline from another head, then compare.

Usage:

    python tests/tools/r4_94_3_route_sweep.py                 # assert 1-3
    python tests/tools/r4_94_3_route_sweep.py --write out.json
    python tests/tools/r4_94_3_route_sweep.py --baseline old.json   # + assert 4

Run inside the test container (it starts the real app and writes a scratch
background_scan_cache row). ``SCANHOUND_ALLOW_OPEN=1`` is required outside
pytest, which sets it via an autouse fixture.
"""
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

from fastapi.testclient import TestClient  # noqa: E402

import backend.api.dependencies as deps  # noqa: E402
from backend.api.main import create_app  # noqa: E402
from backend.database import DatabaseManager  # noqa: E402

URL = "https://hdencode.org/r4-94-3-route-sweep/"
# Deliberately silent: a title carrying a season token is TITLE-authority TV
# evidence and would decide most of the product by itself.
TITLE = "Quiet Neutral Title"

CATEGORIES = ["", "4k", "remux", "tv"]
IS_TV = [False, True]
SEASON = [None, 3]
STORED = [None, ("tv", True), ("tv", False), ("movie", True),
          ("movie", False), ("ambiguous", True)]
FRESH = [False, True]
ATTESTED = [False, True]


def _seed(category, is_tv, season, stored, attested, conflict):
    row = {"url": URL, "title": TITLE, "year": 2026, "status": "missing",
           "category": category, "category_conflict": conflict,
           "category_attested": attested, "is_tv": is_tv, "season": season}
    if stored is not None:
        row["media_type"], row["media_type_provisional"] = stored
    dm = DatabaseManager()
    dm.upsert_background_cache([{
        "url": URL, "title": TITLE, "year": 2026, "status": "missing",
        "source_category": "HDEncode", "data": json.dumps(row)}])
    dm.close()


def _mark_conflict():
    dm = DatabaseManager()
    dm.mark_scan_category_conflict([URL])
    dm.close()


def _details(fresh):
    return {"display_title": TITLE, "year": 2026, "rating": "-", "url": URL,
            "imdb_id": "tt0000001", "size": "23.9 GB", "res": "2160p",
            "hdr": "SDR", "dovi": False, "is_tv": fresh,
            "season": 2 if fresh else None, "episode_number": None,
            "episodes": None, "posted_date": None}


def sweep():
    """{sequence key -> [step, step]}; a step is
    [media_type, provisional, is_tv, category_conflict, category_attested]."""
    out = {}
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as client:
        for cat, itv, sea, sto, fre, att in itertools.product(
                CATEGORIES, IS_TV, SEASON, STORED, FRESH, ATTESTED):
            for order in ("pre", "post"):
                _seed(cat, itv, sea, sto, att, order == "pre")
                steps = []
                with patch.object(deps.registry.scanner.scrapers,
                                  "scrape_details", return_value=_details(fre)):
                    for i in range(2):
                        if order == "post" and i == 1:
                            _mark_conflict()
                        resp = client.post("/scan/rescan-item",
                                           json={"url": URL})
                        if resp.status_code != 200:
                            steps.append(["HTTP", resp.status_code, None,
                                          None, None])
                            break
                        it = resp.json()["item"]
                        steps.append([it["media_type"],
                                      it["media_type_provisional"],
                                      it["is_tv"], it["category_conflict"],
                                      it.get("category_attested")])
                out[json.dumps([cat, itv, sea, sto, fre, att, order])] = steps
    return out


def check(data, baseline=None):
    failures = []

    arms = {}
    for key, steps in data.items():
        parsed = json.loads(key)
        arms.setdefault(json.dumps(parsed[:-1]), {})[parsed[-1]] = steps
    order_dependent = [(shape, a["pre"][-1], a["post"][-1])
                       for shape, a in sorted(arms.items())
                       if a["pre"][-1] != a["post"][-1]]
    print("shapes: %d   sequences: %d   route steps: %d"
          % (len(arms), len(data), sum(len(v) for v in data.values())))
    print("1. ORDER-DEPENDENT shapes: %d" % len(order_dependent))
    for shape, pre, post in order_dependent[:10]:
        print("     %s\n       pre=%s post=%s" % (shape, pre, post))
    if order_dependent:
        failures.append("order-dependent shapes: %d" % len(order_dependent))

    lost = [k for k, v in data.items()
            if json.loads(k)[5] is True and v and v[-1][4] is not True]
    print("2. ATTESTATION LOST by a rescan: %d" % len(lost))
    for k in lost[:5]:
        print("     %s -> %s" % (k, data[k]))
    if lost:
        failures.append("attestation lost: %d" % len(lost))

    bad = [(k, s) for k, v in data.items() for s in v
           if s[0] != "HTTP" and s[2] is not (s[0] == "tv")]
    print("3. INVARIANT VIOLATIONS (is_tv != media_type=='tv'): %d" % len(bad))
    for k, s in bad[:5]:
        print("     %s -> %s" % (k, s))
    if bad:
        failures.append("invariant violations: %d" % len(bad))

    if baseline is not None:
        if set(baseline) != set(data):
            failures.append("baseline covers a different product")
        else:
            moved, unconflicted = 0, []
            kinds = {}
            for k in data:
                for a, b in zip(baseline[k], data[k]):
                    if a[:3] == b[:3]:
                        continue
                    moved += 1
                    kinds[(tuple(a[:3]), tuple(b[:3]))] = 1 + kinds.get(
                        (tuple(a[:3]), tuple(b[:3])), 0)
                    if not a[3]:
                        unconflicted.append((k, a, b))
            print("4. steps whose verdict moved vs baseline: %d" % moved)
            for (a, b), n in sorted(kinds.items(), key=lambda x: -x[1]):
                print("     n=%-4d %s -> %s" % (n, a, b))
            print("   moved steps on a row recording NO conflict: %d"
                  % len(unconflicted))
            for row in unconflicted[:5]:
                print("     COUNTEREXAMPLE %s" % (row,))
            if unconflicted:
                failures.append(
                    "verdict moved on %d unconflicted step(s)"
                    % len(unconflicted))
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write")
    ap.add_argument("--baseline")
    args = ap.parse_args()

    data = sweep()
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=0, sort_keys=True)
        print("wrote", args.write)
    baseline = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as fh:
            baseline = json.load(fh)
    failures = check(data, baseline)
    if failures:
        print("\nFAILED:", "; ".join(failures))
        return 1
    print("\nALL PROPERTIES HOLD")
    return 0


sys.exit(main())
