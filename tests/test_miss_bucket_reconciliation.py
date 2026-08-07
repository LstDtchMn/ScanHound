"""A row counted as bad must have produced its own finding — per bucket.

WHAT ROUND 5 SHIPPED, AND WHY IT WAS NOT ENOUGH. The check asked whether ANY
integrity finding mentioned the cycle:

    if unreported and not any(f.endswith(cycle) or f":{cycle}:" in f
                              for f in integrity):

That is string association, not accounting. One unrelated finding for a cycle
satisfied it for any number of unreported bad rows in that same cycle. And its
test passed with the whole check deleted, because the test could not construct the
state that reaches it: production always reported correctly, so there was nothing
to detect and the guard was never exercised. I wrote that limitation down instead
of rewording the test to look green.

Round 6 makes the state constructible. `reconcile_bucket_reporting` is a pure
function over the per-cycle accounting, so the reviewer's case is three lines of a
dict rather than an unreachable branch:

    unsupported=2, reported_unsupported=1, corrupt=1, reported_corrupt=1

The old string match passes this (a corrupt finding for the cycle exists, so the
`any(...)` is satisfied) while one unsupported row goes unexplained. The new check
must emit exactly ONE finding, naming the unsupported bucket.

THE OTHER HALF, and the reason this file also drives the real query: a pure
function proven correct in isolation is exactly the failure I have made five times
now — right component, nothing calls it. So the last test patches this function
and asserts its output reaches `miss_evidence_integrity`, with a control run
proving the marker does not appear by accident.
"""
import json

import pytest

from backend import database as database_module
from backend.database import DatabaseManager, reconcile_bucket_reporting


class TestTheCaseTheStringMatchGotWrong:

    def test_a_corrupt_finding_does_not_excuse_an_unsupported_row(self):
        """The reviewer's exact numbers."""
        findings = reconcile_bucket_reporting({
            "C1": {"total": 3, "supported": 0,
                   "unsupported": 2, "reported_unsupported": 1,
                   "corrupt": 1, "reported_corrupt": 1},
        })
        assert findings == ["unreported_unsupported_rows:C1:1"], (
            "exactly one finding, naming the unsupported bucket. Round 5 emitted "
            "NOTHING here: a corrupt finding for C1 already existed, so its "
            "any(...) over the findings list was satisfied and the unexplained "
            "unsupported row passed unnoticed.")

    def test_the_shortfall_is_the_exact_count_not_a_flag(self):
        findings = reconcile_bucket_reporting({
            "C1": {"unsupported": 5, "reported_unsupported": 2,
                   "corrupt": 0, "reported_corrupt": 0},
        })
        assert findings == ["unreported_unsupported_rows:C1:3"], (
            "3 unexplained of 5. A boolean would hide how much is missing")

    def test_both_buckets_are_reported_separately(self):
        findings = reconcile_bucket_reporting({
            "C1": {"unsupported": 2, "reported_unsupported": 0,
                   "corrupt": 3, "reported_corrupt": 1},
        })
        assert findings == ["unreported_unsupported_rows:C1:2",
                           "unreported_corrupt_rows:C1:2"], (
            "one bucket must not mask the other -- that was the whole defect")

    def test_each_cycle_is_independent(self):
        findings = reconcile_bucket_reporting({
            "GOOD": {"unsupported": 2, "reported_unsupported": 2,
                     "corrupt": 1, "reported_corrupt": 1},
            "BAD": {"unsupported": 1, "reported_unsupported": 0,
                    "corrupt": 0, "reported_corrupt": 0},
        })
        assert findings == ["unreported_unsupported_rows:BAD:1"], (
            "a correctly-reported cycle must not suppress a broken one, and a "
            "broken one must not implicate a healthy one")

    def test_consistent_accounting_is_silent(self):
        """NEGATIVE CONTROL. If this fired, every healthy run would block on a
        false positive and the finding would be ignored -- as good as silence."""
        assert reconcile_bucket_reporting({
            "C1": {"total": 4, "supported": 1,
                   "unsupported": 2, "reported_unsupported": 2,
                   "corrupt": 1, "reported_corrupt": 1},
        }) == []

    def test_an_all_healthy_cycle_is_silent(self):
        assert reconcile_bucket_reporting({
            "C1": {"total": 9, "supported": 9,
                   "unsupported": 0, "reported_unsupported": 0,
                   "corrupt": 0, "reported_corrupt": 0},
        }) == []

    def test_more_findings_than_bad_rows_is_also_wrong(self):
        """Over-reporting is not the safe direction: it means the accounting is
        broken, and a check that only looks one way would call it healthy."""
        findings = reconcile_bucket_reporting({
            "C1": {"unsupported": 1, "reported_unsupported": 3,
                   "corrupt": 0, "reported_corrupt": 0},
        })
        assert findings == ["overreported_unsupported_rows:C1:2"]

    def test_missing_keys_do_not_crash_the_gate(self):
        """A malformed slot must not take the gate down; an exception here would
        fail OPEN if any caller swallows it."""
        assert reconcile_bucket_reporting({"C1": {}}) == []
        assert reconcile_bucket_reporting({"C1": {"unsupported": None,
                                                 "reported_unsupported": None}}) == []
        assert reconcile_bucket_reporting({}) == []


def _seed_one_valid_cycle(db):
    """One cycle with per-feed provenance and one attributable miss row."""
    cycle = "cycle-round6"
    # The real shape: feed key -> that cycle's outcome string. Only "changed" and
    # "not_modified" count as an observation. My first attempt at this fixture
    # used a nested {"observed": True} dict, which the gate correctly rejected --
    # caught by the positive control below, which is the reason it exists.
    provenance = json.dumps({"movies_all": "changed", "tv_all": "not_modified"})
    stamp = "2026-08-06T12:00:00+00:00"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hdencode_shadow_cycles "
            "(cycle_uuid, started_at, completed_at, normal_feed_outcomes, "
            " normal_feeds_complete, rss_requests, listing_requests, rss_count, "
            " listing_count, duplicate_count, feed_only_count, "
            " listing_only_count, relevant_miss_count, request_reduction_pct, "
            " outcome) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle, stamp, stamp, provenance, 1, 2, 1, 20, 4, 3, 1, 1, 1, 85.0,
             "ok"))
        conn.execute(
            "INSERT INTO hdencode_shadow_misses "
            "(cycle_uuid, canonical_url, media_type) VALUES (?,?,?)",
            (cycle, "https://hdencode.org/some-movie-2160p/", "movie"))
    return cycle


class TestTheProductionPathActuallyUsesIt:
    """Five times I have proved a component right and never checked that anything
    calls it. So prove the call."""

    def test_the_summary_includes_the_reconciliation_output(self, tmp_path,
                                                            monkeypatch):
        db = DatabaseManager(str(tmp_path / "round6-consumer.db"))
        try:
            _seed_one_valid_cycle(db)
            monkeypatch.setattr(
                database_module, "reconcile_bucket_reporting",
                lambda per_cycle: ["SENTINEL_FROM_RECONCILER"])
            summary = db.get_hdencode_shadow_summary()
            assert "SENTINEL_FROM_RECONCILER" in (
                summary.get("miss_evidence_integrity") or []), (
                "get_hdencode_shadow_summary must call reconcile_bucket_reporting "
                "and merge its findings. Without this assertion the function "
                "could be perfectly correct and never invoked -- which is the "
                "exact failure mode this codebase keeps producing.")
        finally:
            db.close()

    def test_the_sentinel_does_not_appear_unpatched(self, tmp_path):
        """Control for the test above: proves the sentinel comes from the patch
        and not from somewhere else in the summary."""
        db = DatabaseManager(str(tmp_path / "round6-control.db"))
        try:
            _seed_one_valid_cycle(db)
            summary = db.get_hdencode_shadow_summary()
            assert "SENTINEL_FROM_RECONCILER" not in (
                summary.get("miss_evidence_integrity") or [])
        finally:
            db.close()

    def test_a_healthy_cycle_produces_no_integrity_findings(self, tmp_path):
        """POSITIVE CONTROL for the whole harness. If a well-formed cycle already
        trips the gate, every assertion about detection above is meaningless --
        the gate would be failing on everything."""
        db = DatabaseManager(str(tmp_path / "round6-healthy.db"))
        try:
            _seed_one_valid_cycle(db)
            summary = db.get_hdencode_shadow_summary()
            assert (summary.get("miss_evidence_integrity") or []) == [], (
                "a valid cycle with matching provenance must be clean")
        finally:
            db.close()

    def test_a_genuinely_bad_row_still_blocks_end_to_end(self, tmp_path):
        """And the real branches still fire through _flag, so the reroute did not
        silence the detection it was meant to preserve."""
        db = DatabaseManager(str(tmp_path / "round6-bad.db"))
        try:
            cycle = _seed_one_valid_cycle(db)
            with db.transaction() as conn:
                # media_type outside the vocabulary: corrupt evidence.
                conn.execute(
                    "INSERT INTO hdencode_shadow_misses "
                    "(cycle_uuid, canonical_url, media_type) VALUES (?,?,?)",
                    (cycle, "https://hdencode.org/other/", "nonsense"))
            summary = db.get_hdencode_shadow_summary()
            findings = summary.get("miss_evidence_integrity") or []
            assert any(f.startswith("media_type_invalid") for f in findings), (
                findings)
            assert not any(f.startswith("unreported_") for f in findings), (
                "the branch reported itself, so the reconciliation must stay "
                "quiet; a finding here would mean _flag is not keeping the "
                "bucket and the report in step")
        finally:
            db.close()
