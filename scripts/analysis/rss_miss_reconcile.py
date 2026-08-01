#!/usr/bin/env python3
"""Reconcile RSS shadow misses against acquired candidates. READ-ONLY.

Reproducible replacement for the prose analysis in
docs/reviews/2026-07-31-rss-miss-CORRECTED.md.

WHY THIS EXISTS. Two opposite conclusions were produced from the same evidence
on 2026-07-31: first "0 of 100 never acquired", then "99 of 100 acquired within
2.8 hours". The first was an artifact of joining two tables that store URLs in
incompatible forms - hdencode_candidates keeps a trailing slash, and
hdencode_shadow_misses does not - so exact string equality could only ever
return zero. Prose describing a result is not sufficient evidence for a result;
this script is.

THE TWO RULES THAT WOULD HAVE PREVENTED IT, both enforced here:
  1. One shared canonicaliser is applied to BOTH sides. Never compare raw
     strings produced by different code paths.
  2. Positive controls run FIRST and the script EXITS NONZERO if they fail. A
     negative join result is meaningless unless the join is first shown capable
     of finding something it should, and of rejecting something it should not.

Usage:
    python rss_miss_reconcile.py --db /path/to/crawler-snapshot.db [--json out.json]

Point it at a frozen snapshot, not the live database, so a rerun months later
reproduces the same numbers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

# Bands were predeclared before the corrected result was known, so they are not
# a post-hoc fit to the observed 2.8 h maximum.
GREEN_HOURS = 6
RED_HOURS = 24

# The feeds RSS-primary would actually run on. Catch-up//sub-feeds are useful
# for recovery but are NOT the production discovery population, so an identity
# acquired only by them does not prove normal-feed coverage.
NORMAL_FEEDS = {"movies_all", "tv_all"}


def canonical(url: str | None) -> str | None:
    """The single identity function. Both sides of every comparison use this.

    Deliberately stricter than either producer: forces https, drops `www.`,
    collapses repeated slashes, strips the trailing slash, and discards query
    and fragment. The trailing slash is the one that actually bit us.
    """
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = "/".join(seg for seg in (parts.path or "/").split("/") if seg)
    return urlunsplit(("https", host or "hdencode.org", "/" + path.rstrip("/"), "", "")).lower()


def _ts(value: str | None) -> dt.datetime | None:
    """Parse a FULL ISO timestamp and normalise to naive UTC.

    An earlier version truncated to 19 characters, silently discarding the
    timezone offset and fractional seconds. Every timestamp in this dataset
    happens to be UTC, so the answers were right — but a tool whose entire
    purpose is reproducibility must not depend on that. One non-UTC row would
    have shifted a lag by hours with nothing raised.
    """
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


def _hours(a: dt.datetime, b: dt.datetime) -> float:
    return (a - b).total_seconds() / 3600.0


def positive_controls(conn: sqlite3.Connection, acquired: dict) -> list[tuple[str, bool, bool]]:
    """Prove the join can find what it should and reject what it should not.

    Returns (name, expected, actual) triples. A negative result from an
    unexercised join proves only that the query returned nothing.
    """
    row = conn.execute(
        "SELECT canonical_url FROM hdencode_candidates WHERE canonical_url IS NOT NULL LIMIT 1"
    ).fetchone()
    known = row[0] if row else ""
    return [
        ("known-present URL joins", True, canonical(known) in acquired),
        # The exact failure mode that produced the false zero.
        ("its trailing-slash variant joins", True, canonical(known.rstrip("/")) in acquired),
        ("unrelated URL does NOT join", False,
         canonical("https://hdencode.org/definitely-not-a-real-release-xyz-000") in acquired),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to a FROZEN snapshot, not the live database")
    ap.add_argument("--json", help="write machine-readable results here")
    ap.add_argument("--allow-missing-attribution", action="store_true",
                    help="proceed without hdencode_candidate_feeds; output is "
                         "marked NON-AUTHORITATIVE")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    # Earliest acquisition per identity, plus the feed that first carried it.
    acquired: dict[str, dict] = {}
    for url, first_seen, pub_date in conn.execute(
        "SELECT canonical_url, first_seen_at, pub_date FROM hdencode_candidates"
    ):
        key = canonical(url)
        if not key:
            continue
        prior = acquired.get(key)
        if prior is None or (first_seen or "") < (prior["first_seen"] or ""):
            acquired[key] = {
                "first_seen": first_seen, "pub_date": pub_date, "feeds": set(),
                "first_feed_at": None, "first_feeds": set(), "first_normal_at": None,
            }

    # Feed attribution. Two prior versions of this block were wrong:
    #   v1 joined on a nonexistent candidate_id, swallowed the OperationalError,
    #      and reported every release "unattributed" — degrading quietly.
    #   v2 collected every feed EVER associated with a candidate, ignoring
    #      first_seen_at. That is an ever-observed SET, not a first acquirer, so
    #      it could not distinguish "a normal feed acquired it" from "catch-up
    #      acquired it and a normal feed saw it later". The published conclusion
    #      happened to be right; the method could not have shown it.
    # Now: order by membership first_seen_at and record the EARLIEST acquirer,
    # keeping ties (two feeds carrying it in the same ingest).
    attribution_available = True
    try:
        rows = list(conn.execute(
            "SELECT feed_key, canonical_url, first_seen_at "
            "FROM hdencode_candidate_feeds"
        ))
    except sqlite3.OperationalError:
        rows = []
        attribution_available = False
        # I told the reviewer this "fails loudly". It did not: it printed a
        # warning and returned 0, so a caller could act on a non-authoritative
        # result. A warning is not a failure.
        if not args.allow_missing_attribution:
            print("
ABORT: hdencode_candidate_feeds is absent, so no claim about "
                  "feed population is supported. Re-run with "
                  "--allow-missing-attribution to produce a NON-AUTHORITATIVE "
                  "result.", file=sys.stderr)
            return 3

    for feed_key, url, seen_at in rows:
        key = canonical(url)
        entry = acquired.get(key)
        if entry is None:
            continue
        when = _ts(seen_at)
        best = entry.get("first_feed_at")
        if when is None:
            continue
        if best is None or when < best:
            entry["first_feed_at"] = when
            entry["first_feeds"] = {feed_key}
        elif when == best:
            entry["first_feeds"].add(feed_key)
        if feed_key in NORMAL_FEEDS:
            prior = entry.get("first_normal_at")
            if prior is None or when < prior:
                entry["first_normal_at"] = when
        entry["feeds"].add(feed_key)

    # COLLISION AUDIT. canonical() lowercases the whole path, so two materially
    # different releases could fold onto one key and be silently treated as the
    # same identity - which would understate misses. Report it rather than
    # assume the site is case-insensitive.
    raw_by_key: dict[str, set] = {}
    for (url,) in conn.execute(
        "SELECT canonical_url FROM hdencode_candidates WHERE canonical_url IS NOT NULL"
    ):
        raw_by_key.setdefault(canonical(url), set()).add(url.rstrip("/"))
    collisions = {k: v for k, v in raw_by_key.items() if len(v) > 1}

    controls = positive_controls(conn, acquired)
    print("POSITIVE CONTROLS")
    ok = True
    for name, expected, actual in controls:
        ok &= actual == expected
        print(f"  {'PASS' if actual == expected else 'FAIL'}  {name:38} expected={expected} actual={actual}")
    if not ok:
        print("\nABORT: controls failed. Any join result below would be meaningless.", file=sys.stderr)
        return 2

    cycles = {u: t for u, t in conn.execute(
        "SELECT cycle_uuid, completed_at FROM hdencode_shadow_cycles")}
    newest = max((t for t in cycles.values() if t), default=None)
    newest_ts = _ts(newest)

    buckets: Counter = Counter()
    feeds: Counter = Counter()
    lags: list[float] = []
    pub_offsets: list[float] = []
    unresolved: list[dict] = []

    misses = list(conn.execute(
        "SELECT cycle_uuid, canonical_url FROM hdencode_shadow_misses"))
    for cycle_uuid, url in misses:
        key = canonical(url)
        entry = acquired.get(key)
        miss_at = _ts(cycles.get(cycle_uuid))

        if entry is None:
            age = _hours(newest_ts, miss_at) if (newest_ts and miss_at) else None
            state = ("UNKNOWN" if age is None
                     else "RED" if age > RED_HOURS
                     else "YELLOW" if age > GREEN_HOURS
                     else "PENDING")
            buckets[f"D never acquired ({state})"] += 1
            unresolved.append({"url": url, "miss_at": cycles.get(cycle_uuid),
                               "age_hours": round(age, 1) if age else None, "state": state})
            continue

        got_at = _ts(entry["first_seen"])
        if not (miss_at and got_at):
            buckets["? untimed"] += 1
            continue

        if got_at < miss_at:
            buckets["A acquired BEFORE the miss"] += 1
        elif got_at > miss_at:
            buckets["B acquired AFTER the miss"] += 1
            lags.append(_hours(got_at, miss_at))
        else:
            buckets["C same cycle"] += 1

        # Separates UPSTREAM publication delay from OUR polling delay. Negative
        # means the feed already carried it when the comparison ran.
        published = _ts(entry["pub_date"])
        if published:
            pub_offsets.append(_hours(published, miss_at))

        # Classify by FIRST acquirer, not by ever-observed membership.
        if not attribution_available:
            feeds["attribution unavailable"] += 1
        elif not entry["first_feeds"]:
            feeds["no membership row"] += 1
        elif entry["first_feeds"] & NORMAL_FEEDS:
            feeds["first acquired by NORMAL feed"] += 1
        elif entry.get("first_normal_at") is not None:
            feeds["catch-up first, normal feed later"] += 1
        else:
            feeds["CATCH-UP ONLY: " + ",".join(sorted(entry["first_feeds"]))] += 1

    def stats(values: list[float]) -> dict:
        if not values:
            return {}
        s = sorted(values)
        return {"n": len(s), "min": round(s[0], 2),
                "median": round(s[len(s) // 2], 2), "max": round(s[-1], 2),
                f"within_{GREEN_HOURS}h": sum(1 for v in s if v <= GREEN_HOURS),
                f"over_{RED_HOURS}h": sum(1 for v in s if v > RED_HOURS)}

    print("\nCOLLISION AUDIT (one canonical key <- multiple raw URLs)")
    if collisions:
        print(f"  {len(collisions)} collision(s) - identities may be merged:")
        for key, raws in list(collisions.items())[:5]:
            print(f"    {key[-58:]}")
            for raw in sorted(raws)[:3]:
                print(f"       <- {raw[-58:]}")
    else:
        print("  none - canonicalisation is injective over this dataset")

    if not attribution_available:
        print("\n*** FEED ATTRIBUTION UNAVAILABLE - hdencode_candidate_feeds is "
              "absent. No claim about feed population is supported. ***")

    print(f"\nCLASSIFICATION (n={len(misses)})")
    for name in sorted(buckets):
        print(f"  {name:36} {buckets[name]:4d}")

    print("\nACQUISITION LAG (hours after the miss)")
    print(" ", stats(lags) or "none")

    print("\nPUBLICATION OFFSET (negative = feed already had it when we compared)")
    print(" ", stats(pub_offsets) or "none")

    print("\nFIRST ACQUIRING FEED")
    for name, count in feeds.most_common():
        print(f"  {name:36} {count:4d}")

    if unresolved:
        print("\nUNRESOLVED")
        for item in unresolved:
            print(f"  [{item['state']}] age={item['age_hours']}h  {item['url'][-58:]}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({
                "controls": [{"name": n, "expected": e, "actual": a} for n, e, a in controls],
                "total_misses": len(misses),
                "buckets": dict(buckets),
                "acquisition_lag_hours": stats(lags),
                "publication_offset_hours": stats(pub_offsets),
                "first_acquiring_feed": dict(feeds),
                "unresolved": unresolved,
                "attribution_available": attribution_available,
                "canonical_collisions": len(collisions),
                "thresholds": {"green_hours": GREEN_HOURS, "red_hours": RED_HOURS},
            }, handle, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
