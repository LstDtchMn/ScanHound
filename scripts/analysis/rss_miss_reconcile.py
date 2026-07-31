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
    try:
        return dt.datetime.fromisoformat((value or "")[:19])
    except (ValueError, TypeError):
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
            acquired[key] = {"first_seen": first_seen, "pub_date": pub_date, "feeds": set()}

    # Feed attribution. hdencode_candidate_feeds keys by canonical_url, NOT by
    # a candidate id - and it stores the trailing-slash form, so it must go
    # through canonical() like everything else. An earlier version joined on a
    # nonexistent candidate_id, caught the OperationalError, and reported every
    # release as "unattributed": degrading quietly instead of lying, but still
    # wrong. Absent on older schemas; degrade rather than invent a source.
    try:
        for feed_key, url in conn.execute(
            "SELECT feed_key, canonical_url FROM hdencode_candidate_feeds"
        ):
            key = canonical(url)
            if key in acquired:
                acquired[key]["feeds"].add(feed_key)
    except sqlite3.OperationalError:
        pass

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

        feeds[",".join(sorted(entry["feeds"])) or "unattributed"] += 1

    def stats(values: list[float]) -> dict:
        if not values:
            return {}
        s = sorted(values)
        return {"n": len(s), "min": round(s[0], 2),
                "median": round(s[len(s) // 2], 2), "max": round(s[-1], 2),
                f"within_{GREEN_HOURS}h": sum(1 for v in s if v <= GREEN_HOURS),
                f"over_{RED_HOURS}h": sum(1 for v in s if v > RED_HOURS)}

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
                "thresholds": {"green_hours": GREEN_HOURS, "red_hours": RED_HOURS},
            }, handle, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
