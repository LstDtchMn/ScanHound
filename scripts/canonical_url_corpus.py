"""Reproducible canonical-URL corpus measurement (inventory §6.1-§6.3).

Usage:
    python scripts/canonical_url_corpus.py <snapshot.db> [out.json]

Read-only: opens the snapshot with SQLite URI mode=ro. NEVER point this at
the live /dbvol/crawler.db — measure a backup-API snapshot and record its
SHA-256, which this script embeds in the output as provenance.

Controls run FIRST and the script refuses to emit numbers if any control
fails — a zero without a passing positive control is not evidence (four
instruments false-greened in one week here).

Output is deterministic JSON (sorted keys, no timestamps beyond the
snapshot's own provenance) so two runs over the same snapshot are
byte-identical and diffable.
"""
import hashlib
import json
import os
import sqlite3
import sys

# The URL-bearing identity columns, from the 2026-08-03 inventory. Form is
# the EXPECTED convention; the measurement verifies it rather than trusting it.
URL_COLUMNS = [
    ("hdencode_candidates", "canonical_url", "A"),
    ("hdencode_candidate_feeds", "canonical_url", "A"),
    ("hdencode_hydration_queue", "canonical_url", "A"),
    ("hdencode_candidate_details", "canonical_url", "A"),
    ("hdencode_shadow_misses", "canonical_url", "B"),
    ("listing_policy_exclusions", "canonical_url", "B"),
    ("scanned_urls", "url", "raw"),
    ("background_scan_cache", "url", "raw"),
    ("downloads", "url", "raw"),
    ("dismissed_items", "url", "raw"),
    ("download_queue_items", "canonical_url", "raw"),
    ("pipeline_verdicts", "url", "raw"),
    ("scraped_link_map", "source_url", "raw"),
    ("hdencode_feed_state", "feed_url", "feed"),
]


def _controls(cur):
    """Positive + negative controls for the comparators this report relies on."""
    results = {}
    # Positive: the slash-append bridge detects a known cross-form pair.
    cur.execute("SELECT 'https://hdencode.org/ctrl' || '/' = 'https://hdencode.org/ctrl/'")
    results["positive_slash_bridge"] = bool(cur.fetchone()[0])
    # Negative: different slugs must NOT match under the same bridge.
    cur.execute("SELECT 'https://hdencode.org/ctrl-a' || '/' = 'https://hdencode.org/ctrl-b/'")
    results["negative_different_slugs"] = not bool(cur.fetchone()[0])
    # Positive: LIKE '%/' detects a trailing slash; negative: absent one.
    cur.execute("SELECT 'https://x/y/' LIKE '%/', 'https://x/y' LIKE '%/'")
    row = cur.fetchone()
    results["positive_slash_detector"] = bool(row[0])
    results["negative_slash_detector"] = not bool(row[1])
    return results


def measure(db_path: str) -> dict:
    digest = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()

    controls = _controls(cur)
    if not all(controls.values()):
        raise SystemExit(f"CONTROLS FAILED — refusing to report: {controls}")

    tables = {}
    for table, col, expected in URL_COLUMNS:
        try:
            cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT {col}),"
                        f" SUM({col} LIKE '%/'), SUM({col} LIKE 'http:%'),"
                        f" SUM(instr({col}, '?') > 0) FROM {table}")
        except sqlite3.OperationalError as exc:
            tables[f"{table}.{col}"] = {"error": str(exc)}
            continue
        total, distinct, slashed, http, query = (x or 0 for x in cur.fetchone())
        tables[f"{table}.{col}"] = {
            "expected_form": expected, "rows": total, "distinct": distinct,
            "trailing_slash": slashed, "http_scheme": http, "with_query": query,
        }

    joins = {}
    for b_table in ("hdencode_shadow_misses", "listing_policy_exclusions"):
        cur.execute(f"SELECT COUNT(DISTINCT b.canonical_url) FROM {b_table} b "
                    "JOIN hdencode_candidates c ON b.canonical_url = c.canonical_url")
        exact = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(DISTINCT b.canonical_url) FROM {b_table} b "
                    "JOIN hdencode_candidates c ON b.canonical_url || '/' = c.canonical_url")
        bridged = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(DISTINCT canonical_url) FROM {b_table}")
        denominator = cur.fetchone()[0]
        cur.execute(f"SELECT DISTINCT b.canonical_url FROM {b_table} b "
                    "WHERE NOT EXISTS (SELECT 1 FROM hdencode_candidates c "
                    "  WHERE b.canonical_url || '/' = c.canonical_url "
                    "     OR b.canonical_url = c.canonical_url) "
                    "ORDER BY b.canonical_url")
        unmatched = [r[0] for r in cur.fetchall()]
        joins[f"{b_table}_vs_candidates"] = {
            "denominator": denominator, "exact_match": exact,
            "bridged_match": bridged, "unmatched_count": len(unmatched),
            "unmatched": unmatched,
        }

    # ---- actual-function pass (round-10 Q1): run the REAL canonicalizers ----
    # The SQL bridge above proves string relationships; this section proves
    # the shared functions themselves behave on the real corpus.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.url_canonical import (
        canonicalize_hdencode_post_url, canonicalize_listing_url,
        post_to_listing_identity)

    def _fixed_point_stats(rows_sql, fn):
        cur.execute(rows_sql)
        urls = [r[0] for r in cur.fetchall()]
        accepted = rejected = noncanonical = 0
        canon_groups = {}
        for u in urls:
            try:
                c = fn(u)
            except ValueError:
                rejected += 1
                continue
            accepted += 1
            if c != u:
                noncanonical += 1
            canon_groups.setdefault(c, set()).add(u)
        collisions = {c: sorted(v) for c, v in canon_groups.items() if len(v) > 1}
        return {"rows": len(urls), "accepted": accepted, "rejected": rejected,
                "noncanonical_form": noncanonical,
                "canonical_collision_groups": len(collisions),
                "collisions": collisions}

    functional = {
        "form_a_candidates": _fixed_point_stats(
            "SELECT DISTINCT canonical_url FROM hdencode_candidates",
            canonicalize_hdencode_post_url),
        "form_b_shadow_misses": _fixed_point_stats(
            "SELECT DISTINCT canonical_url FROM hdencode_shadow_misses",
            canonicalize_listing_url),
        "form_b_exclusions": _fixed_point_stats(
            "SELECT DISTINCT canonical_url FROM listing_policy_exclusions",
            canonicalize_listing_url),
    }
    # the NAMED bridge over the real corpus (not the SQL || bridge)
    cur.execute("SELECT DISTINCT canonical_url FROM hdencode_candidates")
    cand_bridge = {post_to_listing_identity(r[0]) for r in cur.fetchall()}
    for b_table in ("hdencode_shadow_misses", "listing_policy_exclusions"):
        cur.execute(f"SELECT DISTINCT canonical_url FROM {b_table}")
        b_urls = [r[0] for r in cur.fetchall()]
        matched = sum(1 for u in b_urls if canonicalize_listing_url(u) in cand_bridge)
        functional[f"named_bridge_{b_table}"] = {
            "denominator": len(b_urls), "matched_via_post_to_listing_identity": matched,
            "unmatched": len(b_urls) - matched}

    con.close()
    return {
        "functional_pass": functional,
        "snapshot_sha256": digest,
        "provenance": "backup-API snapshot of /dbvol/crawler.db (sqlite3 "
                      "Connection.backup into the container's /tmp, docker cp out); "
                      "record the capture time next to the file",
        "controls": controls,
        "tables": tables,
        "cross_form_joins": joins,
        "definitions": {
            "bridged_match": "Form-B key || '/' equals a candidates (Form-A) key — "
                             "the named post_to_listing_identity bridge, inverted",
            "denominator": "COUNT(DISTINCT canonical_url) of the Form-B table; "
                           "unmatched rows are LISTED, never dropped",
        },
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    report = measure(sys.argv[1])
    out = json.dumps(report, indent=1, sort_keys=True)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out + "\n")
    print(out[:2000])
