"""Emit the RSS window measurement as a bounded, machine-readable artifact.

The 2026-08-06 peer review (Finding 5 and the closure list) asked for evidence
that is reproducible from the branch rather than attested in prose. Round 2's
first attempt fell short in a way worth recording: it selected ALL cycles and ALL
miss rows in whatever database it was handed, so it reported 311 cycles running
to 2026-08-06T12:38Z while the surrounding text claimed a 2026-07-22..08-05
window, and a rerun would silently change the cohort.

TWO BOUNDS, KEPT SEPARATE. A valid retrospective analysis needs both, and the
first version conflated them:

  --admission-end      the last moment a miss may ENTER the cohort.
  --observation-end    the last moment a cycle may be used to RESOLVE an
                       admitted miss. This may legitimately extend past
                       admission-end, so a miss recorded on the final admitted
                       day still gets its catch-up window.

Mixing them lets an Aug 6 miss be admitted while Aug 6 cycles also resolve
earlier misses -- a moving denominator.

SOURCE BINDING. VACUUM INTO produces a consistent snapshot BEFORE any measurement
query, and that snapshot -- not the live file -- is what gets hashed and queried.
The earlier version hashed crawler.db after querying it, which bound nothing: the
app runs WAL mode, so returned rows could live in crawler.db-wal, and the reads
and the digest could observe different moments.

REPLAY. --replay-out writes a redacted dataset carrying the hashed per-cycle
listing_only and feed_only sets, so a reviewer can recompute the resolution
calculation ("did this URL later appear in feed_only?") from repository contents
alone. The per-record manifest is output; this is INPUT. Hash equality is
sufficient for the algorithm, so no plaintext URL is needed.

PRIVACY. URLs are emitted as full SHA-256 digests by default. The same release
hashes identically across runs, so records stay comparable and replayable without
publishing the corpus. --include-urls opts into plaintext.

    python emit_measurement_artifact.py --db PATH \\
        [--admission-start ISO] [--admission-end ISO] [--observation-end ISO] \\
        [--replay-out PATH] [--snapshot PATH] [--no-snapshot] [--include-urls]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

GREEN_H, YELLOW_H = 6.0, 24.0
VALID_FEED_OUTCOMES = ("changed", "not_modified")


def ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def as_utc(value):
    parsed = ts(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        return f"unavailable: {exc.__class__.__name__}"
    return digest.hexdigest()


def url_id(url, *, plaintext):
    """Full SHA-256, not a truncated prefix.

    The review asked for full-length identifiers in an audit artifact: 16 hex
    characters is fine for eyeballing, but a digest that anchors evidence should
    not be shortened.
    """
    if plaintext:
        return str(url)
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/dbvol/crawler.db")
    ap.add_argument("--snapshot", default=None,
                    help="path for the VACUUM INTO snapshot (default: "
                         "<db>.measure-snapshot)")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="query the live database directly. The digest then "
                         "does NOT bind the queried bytes. Exploration only.")
    ap.add_argument("--replay-out", default=None,
                    help="write a redacted replay dataset here: hashed "
                         "per-cycle listing_only/feed_only sets and hashed miss "
                         "identities, sufficient to recompute the resolution "
                         "calculation without the production corpus")
    ap.add_argument("--admission-start", default=None,
                    help="earliest completed_at a miss may be admitted from. "
                         "DEFAULT: unbounded -- every cycle is admitted. (The "
                         "help previously claimed 'the first eligible cycle', "
                         "which was wrong.)")
    ap.add_argument("--admission-end", default=None,
                    help="latest completed_at a miss may be admitted from "
                         "(default: unbounded -- and then the cohort is NOT "
                         "fixed, which the artifact states plainly)")
    ap.add_argument("--observation-end", default=None,
                    help="latest cycle usable to RESOLVE an admitted miss "
                         "(default: unbounded)")
    ap.add_argument("--include-urls", action="store_true",
                    help="emit plaintext URLs instead of truncated hashes")
    args = ap.parse_args()

    # SNAPSHOT FIRST, THEN MEASURE. The previous version opened the live
    # database read-only, ran many independent queries, and hashed crawler.db at
    # the end. A 2026-08-06 review showed that binds nothing:
    #
    #   * the application runs SQLite in WAL mode, so rows returned by these
    #     queries may live in crawler.db-wal, which was never hashed;
    #   * hashing after the reads means the reads and the digest can observe
    #     different moments of a live file.
    #
    # VACUUM INTO produces a single consistent file containing the WAL contents,
    # and every query below runs against that. The digest then genuinely
    # identifies the bytes that produced the counts.
    snapshot_path = args.snapshot or (args.db + ".measure-snapshot")
    snapshot_note = None
    if args.no_snapshot:
        snapshot_note = ("--no-snapshot: queried the live database directly. "
                         "The digest does NOT bind the queried bytes; WAL "
                         "contents are excluded and the file may change between "
                         "reads. Use for exploration only.")
        query_path = args.db
    else:
        try:
            if os.path.exists(snapshot_path):
                os.remove(snapshot_path)
            src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
            try:
                src.execute("VACUUM INTO ?", (snapshot_path,))
            finally:
                src.close()
            query_path = snapshot_path
            snapshot_note = ("VACUUM INTO snapshot taken before any measurement "
                             "query; WAL contents are folded in and the file is "
                             "closed before hashing.")
        except sqlite3.Error as exc:
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 2

    source_digest = sha256_file(query_path)
    con = sqlite3.connect(f"file:{query_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    cycle_cols = {r[1] for r in con.execute(
        "PRAGMA table_info(hdencode_shadow_cycles)")}
    miss_cols = {r[1] for r in con.execute(
        "PRAGMA table_info(hdencode_shadow_misses)")}
    has_provenance = "normal_feed_outcomes" in cycle_cols

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

    adm_start = as_utc(args.admission_start) if args.admission_start else None
    adm_end = as_utc(args.admission_end) if args.admission_end else None
    obs_end = as_utc(args.observation_end) if args.observation_end else None
    # An observation window that ends before admission would deny a
    # late-admitted miss any chance to resolve, which silently manufactures
    # PENDING and AMBIGUOUS verdicts.
    if adm_end and obs_end and obs_end < adm_end:
        print("observation-end precedes admission-end: admitted misses would "
              "have no window in which to resolve", file=sys.stderr)
        return 2

    def in_admission(r):
        at = as_utc(r["completed_at"])
        if adm_start and at < adm_start:
            return False
        if adm_end and at > adm_end:
            return False
        return True

    def in_observation(r):
        at = as_utc(r["completed_at"])
        if adm_start and at < adm_start:
            return False
        if obs_end and at > obs_end:
            return False
        return True

    cohort = [r for r in rows if in_admission(r)]
    cohort_eligible = [r for r in cohort if eligible(r)]

    # The resolution observation set is bounded by observation-end, NOT by
    # admission-end -- that separation is the point.
    cyc = {}
    for r in rows:
        try:
            details = json.loads(r["details_json"] or "{}")
        except Exception:
            details = {}
        cyc[r["cycle_uuid"]] = {
            "uuid": r["cycle_uuid"],
            "at": r["completed_at"],
            "listing_only": set(details.get("listing_only") or ()),
            "feed_only": set(details.get("feed_only") or ()),
            "eligible": eligible(r),
            "complete": r["normal_feeds_complete"] == 1,
            "observable": in_observation(r),
            "provenance": (r["normal_feed_outcomes"] if has_provenance else None),
        }
    observations = sorted(
        ((as_utc(c["at"]), c) for c in cyc.values()
         if c["eligible"] and c["observable"]),
        key=lambda pair: pair[0])

    def classify(url, first_seen):
        """Return (state, hours, resolving_cycle_at, resolving_cycle_uuid)."""
        last_missing = first_seen
        for at, c in observations:
            if at <= first_seen:
                continue
            if url in c["listing_only"]:
                last_missing = at
            elif url in c["feed_only"]:
                # The resolving cycle's UUID travels with its timestamp: a
                # timestamp alone cannot be joined back to the cycle that
                # supplied the evidence.
                return ("resolved", (at - first_seen).total_seconds() / 3600,
                        c["at"], c["uuid"])
        newest = observations[-1][0] if observations else first_seen
        unresolved_h = (newest - first_seen).total_seconds() / 3600
        if last_missing > first_seen:
            state = "red" if unresolved_h > YELLOW_H else "pending"
            return state, unresolved_h, None, None
        return "ambiguous", unresolved_h, None, None

    select_media = " m.media_type," if "media_type" in miss_cols else " NULL AS media_type,"
    select_basis = (" m.attribution_basis," if "attribution_basis" in miss_cols
                    else " NULL AS attribution_basis,")
    all_misses = [dict(r) for r in con.execute(
        "SELECT m.canonical_url u, m.status," + select_media + select_basis
        + " m.cycle_uuid, s.completed_at at, s.normal_feeds_complete complete"
        + (", s.normal_feed_outcomes provenance" if has_provenance
           else ", NULL AS provenance")
        + " FROM hdencode_shadow_misses m"
          " JOIN hdencode_shadow_cycles s ON s.cycle_uuid = m.cycle_uuid")]
    admitted_cycle_ids = {r["cycle_uuid"] for r in cohort}
    cohort_misses = [m for m in all_misses if m["cycle_uuid"] in admitted_cycle_ids]

    def grade(population, label, predicate, note):
        buckets = Counter()
        hours = []
        manifest = []
        blocking = []
        for m in sorted(population, key=lambda x: str(x["at"])):
            state, h, resolving_at, resolving_uuid = classify(
                m["u"], as_utc(m["at"]))
            tier = (("green" if h <= GREEN_H else
                     "yellow" if h <= YELLOW_H else "red")
                    if state == "resolved" else state)
            buckets[tier] += 1
            record = {
                "url_id": url_id(m["u"], plaintext=args.include_urls),
                "tier": tier,
                "state": state,
                "source_cycle": m["cycle_uuid"],
                "first_seen": m["at"],
                "resolving_cycle_at": resolving_at,
                "resolving_cycle_uuid": resolving_uuid,
                "latency_hours": round(h, 3),
                "status": m["status"],
                "media_type": m["media_type"],
                "attribution_basis": m["attribution_basis"],
                "provenance_present": m["provenance"] is not None,
            }
            manifest.append(record)
            if state == "resolved":
                hours.append(h)
            # A >24h RESOLUTION is RED and therefore blocking. The first version
            # appended only unresolved states, so a slow-resolved red would have
            # been counted in the tiers and omitted from this list -- harmless
            # while the count is zero, incomplete the moment it is not.
            if tier in ("red", "pending", "ambiguous"):
                blocking.append(record)
        latency = None
        if hours:
            hs = sorted(hours)
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
            "label": label,
            "denominator": len(population),
            "predicate": predicate,
            "note": note,
            "tiers": {k: buckets[k] for k in
                      ("green", "yellow", "red", "pending", "ambiguous")},
            "blocking_total": (buckets["red"] + buckets["pending"]
                               + buckets["ambiguous"]),
            "catch_up_latency": latency,
            "blocking_records": blocking,
            "record_manifest": manifest,
        }

    conservative = [m for m in cohort_misses if m["complete"] == 1]
    legacy_rows = [m for m in cohort_misses if m["provenance"] is None]
    provenance_rows = [m for m in cohort_misses if m["provenance"] is not None]

    feed_requests = sum(r["rss_requests"] or 0 for r in cohort_eligible)
    listing_requests = sum(r["listing_requests"] or 0 for r in cohort_eligible)

    cohort_digest = hashlib.sha256(
        "\n".join(sorted(admitted_cycle_ids)).encode("utf-8")).hexdigest()

    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "live_path": args.db,
            "queried_path": query_path,
            "snapshotted": not args.no_snapshot,
            "sha256": source_digest,
            "sha256_covers": ("the exact file every query below ran against"
                              if not args.no_snapshot else
                              "crawler.db only -- NOT the WAL, and NOT pinned "
                              "to the moment of reading"),
            "note": snapshot_note,
        },
        "bounds": {
            "admission_start": args.admission_start,
            "admission_end": args.admission_end,
            "observation_end": args.observation_end,
            "cohort_is_fixed": bool(args.admission_end),
            "note": ("admission bounds which misses ENTER the cohort; "
                     "observation bounds which cycles may RESOLVE them, and may "
                     "extend later so a late-admitted miss still gets its "
                     "catch-up window. Without --admission-end the cohort is "
                     "NOT fixed and a rerun may report different counts."),
        },
        "cohort": {
            "cycles_admitted": len(cohort),
            "cycles_admitted_eligible": len(cohort_eligible),
            "cycle_uuid_digest_sha256": cohort_digest,
            "first_admitted_eligible": (cohort_eligible[0]["completed_at"]
                                        if cohort_eligible else None),
            "last_admitted_eligible": (cohort_eligible[-1]["completed_at"]
                                       if cohort_eligible else None),
            "observation_cycles_used": len(observations),
            "observed_days": (round(
                (as_utc(cohort_eligible[-1]["completed_at"])
                 - as_utc(cohort_eligible[0]["completed_at"])).total_seconds()
                / 86400, 3) if len(cohort_eligible) > 1 else 0),
            "required_cycles": 20,
            "required_days": 7,
            "eligibility_predicate": (
                "outcome IN ('success','relevant_miss') AND "
                "normal_feeds_complete=1 AND rss_requests>0 AND "
                "listing_requests>0"),
        },
        "schema": {
            "has_per_feed_provenance": has_provenance,
            "cohort_legacy_rows": len(legacy_rows),
            "cohort_provenance_aware_rows": len(provenance_rows),
            "note": ("Attribution requires per-feed provenance. Legacy rows "
                     "predate it and cannot be graded under attribution at any "
                     "level of effort, because the evidence was never written. "
                     "They are bounded conservatively instead."),
        },
        "request_reduction": {
            "feed_requests": feed_requests,
            "listing_requests": listing_requests,
            "reduction_pct": (round(100.0 * (listing_requests - feed_requests)
                                    / listing_requests, 2)
                              if listing_requests else None),
            "requests_avoided": listing_requests - feed_requests,
            "population": "admitted eligible cycles only",
            "note": ("A projection, not a realised saving. In shadow mode both "
                     "the feeds and the full listing run, so total requests are "
                     "currently HIGHER. This is what would be saved once the "
                     "feed becomes the primary discovery path."),
        },
        "miss_populations": {
            "all_recorded_in_cohort": {
                "denominator": len(cohort_misses),
                "predicate": "every miss row whose source cycle is in the cohort",
                "note": ("Includes rows whose comparison cannot support a miss "
                         "claim. Reported for completeness, not as evidence."),
            },
            "conservative_bound": grade(
                conservative,
                "conservative_bound",
                "source cycle normal_feeds_complete = 1, within admission bounds",
                ("Stricter for ADMISSION than attribution: a mixed cycle "
                 "(movies_all changed, tv_all failed) contributes nothing here, "
                 "whereas attribution would admit its valid movie half. So this "
                 "is a lower bound on attributable blocking misses. It "
                 "guarantees no false accusation of a miss. It does NOT "
                 "establish overall health -- zero blockers in the smaller "
                 "admitted set says nothing about the larger attribution-valid "
                 "set, because an omitted mixed-cycle row could itself be "
                 "permanently missing. Supports only the admitted-record "
                 "resolution claim."),
            ),
        },
        "status_mix": dict(Counter(m["status"] for m in cohort_misses)),
        "grading_rule": {
            "green_hours": GREEN_H, "yellow_hours": YELLOW_H,
            "valid_feed_outcomes": list(VALID_FEED_OUTCOMES),
            "source": "Jesse's tiered criterion, 2026-07-24",
        },
        "resolution_evidence": {
            "rule": ("a miss is resolved only when its canonical URL later "
                     "appears in feed_only during an eligible cycle inside the "
                     "observation bound"),
            "limitation": ("proves the feed had acquired the URL by that later "
                           "cycle; it does NOT prove the original comparison was "
                           "valid. Peer review 2026-08-06, Finding 3."),
        },
    }
    # ── redacted replay input ────────────────────────────────────────────────
    #
    # The manifest above records what this run CONCLUDED. That cannot be checked
    # without rerunning the same script against the same private database. This
    # block records what the algorithm CONSUMED, hashed, so the resolution
    # calculation can be recomputed from repository contents alone.
    if args.replay_out:
        replay = {
            "generated_at": artifact["generated_at"],
            "source_sha256": source_digest,
            "bounds": artifact["bounds"],
            "grading_rule": artifact["grading_rule"],
            "eligibility_predicate": artifact["cohort"]["eligibility_predicate"],
            "url_ids_are": ("plaintext" if args.include_urls
                            else "sha256(canonical_url) full digest"),
            "cycles": [
                {
                    "cycle_uuid": c["uuid"],
                    "completed_at": c["at"],
                    "eligible": c["eligible"],
                    "normal_feeds_complete": c["complete"],
                    "in_observation_window": c["observable"],
                    "admitted": c["uuid"] in admitted_cycle_ids,
                    "provenance": c["provenance"],
                    # The two difference sets the resolution rule reads. Without
                    # these a reviewer cannot replay "later appeared in
                    # feed_only" at all.
                    "listing_only_ids": sorted(
                        url_id(u, plaintext=args.include_urls)
                        for u in c["listing_only"]),
                    "feed_only_ids": sorted(
                        url_id(u, plaintext=args.include_urls)
                        for u in c["feed_only"]),
                }
                for c in sorted(cyc.values(), key=lambda x: str(x["at"]))
            ],
            "misses": [
                {
                    "url_id": url_id(m["u"], plaintext=args.include_urls),
                    "source_cycle": m["cycle_uuid"],
                    "first_seen": m["at"],
                    "status": m["status"],
                    "media_type": m["media_type"],
                    "attribution_basis": m["attribution_basis"],
                    "source_cycle_normal_feeds_complete": m["complete"],
                    "source_cycle_provenance": m["provenance"],
                }
                for m in sorted(all_misses, key=lambda x: str(x["at"]))
            ],
        }
        with open(args.replay_out, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(replay, handle, indent=2, sort_keys=False)
            handle.write("\n")
        artifact["replay_dataset"] = {
            "path": args.replay_out,
            "sha256": sha256_file(args.replay_out),
            "cycles": len(replay["cycles"]),
            "misses": len(replay["misses"]),
            "note": ("Sufficient to recompute the resolution calculation "
                     "without the production database. Hash equality is all the "
                     "algorithm needs."),
        }

    json.dump(artifact, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
