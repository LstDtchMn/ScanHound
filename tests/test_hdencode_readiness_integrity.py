"""Adversarial readiness/recovery contract tests."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from backend.database import DatabaseManager
from backend.background_scanner import BackgroundScanner


def _insert_cycle(db, *, uuid, completed_at, normal=1, rss=2, listing=10,
                  misses=0, restart=0, catchup=0, outcome="success",
                  feed_outcomes=None):
    """Insert a shadow cycle.

    ``feed_outcomes`` is per-normal-feed provenance, e.g.
    ``{"movies_all": "changed", "tv_all": "failed"}``. Passing None leaves the
    column NULL, which marks the row PRE-ATTRIBUTION: its relevant_miss_count
    came from the old unfiltered logic, so get_hdencode_shadow_summary bounds it
    conservatively. Every row the live 2026-07-22..08-05 window produced is of
    that kind, because per-feed provenance was never recorded.
    """
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO hdencode_shadow_cycles (
               cycle_uuid, started_at, completed_at, normal_feeds_complete,
               rss_requests, listing_requests, rss_count, listing_count,
               duplicate_count, feed_only_count, listing_only_count,
               relevant_miss_count, request_reduction_pct, catchup_used,
               restart_recovery, outcome, details_json, normal_feed_outcomes
           ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, 0, ?, ?, ?, '{}', ?)""",
        (
            uuid, completed_at, completed_at, normal, rss, listing,
            misses, catchup, restart, outcome,
            None if feed_outcomes is None else json.dumps(feed_outcomes),
        ),
    )
    conn.commit()


def _insert_miss(db, *, uuid, url, media_type, status="missing", title="T"):
    """Insert a miss ROW. The gate re-derives attribution from these, not from
    the cycle's aggregate count, so a test that only sets relevant_miss_count
    proves nothing about attribution."""
    conn = db.get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO hdencode_shadow_misses "
        "(cycle_uuid, canonical_url, title, status, media_type) "
        "VALUES (?, ?, ?, ?, ?)",
        (uuid, url, title, status, media_type),
    )
    conn.commit()


def test_incomplete_and_degenerate_cycles_do_not_advance_readiness(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(
        db, uuid="incomplete-early",
        completed_at="2026-07-01T00:00:00+00:00",
        normal=0, rss=0, listing=100,
    )
    _insert_cycle(
        db, uuid="eligible",
        completed_at="2026-07-21T00:00:00+00:00",
        normal=1, rss=2, listing=10,
    )
    _insert_cycle(
        db, uuid="degenerate-late",
        completed_at="2026-08-15T00:00:00+00:00",
        normal=1, rss=0, listing=100,
    )

    summary = db.get_hdencode_shadow_summary()
    assert summary["successful_cycles"] == 1
    assert summary["first_completed_at"] == "2026-07-21T00:00:00+00:00"
    assert summary["last_completed_at"] == "2026-07-21T00:00:00+00:00"
    assert summary["rss_requests"] == 2
    assert summary["listing_requests"] == 10
    assert summary["request_reduction_pct"] == 80.0


def test_relevant_miss_blocks_even_when_cycle_is_incomplete(tmp_path):
    """The 2026-07-21 audit rule (f5e3c6e), restated in terms of attribution.

    ORIGINAL INTENT, unchanged and still enforced: a degraded cycle must not be
    able to HIDE a genuine gap.

    WHAT CHANGED, and why. The original row carried no per-feed provenance and
    asserted that ANY miss from a degraded cycle blocks. A 2026-08-06 peer review
    showed that cannot be right: with rss_requests=1 the feed side may be a
    catch-up feed, or an attempted request that failed, leaving the comparison
    listing-versus-stale-snapshot, which can prove neither success nor failure.
    Counting such a row does not protect against a hidden gap -- it invents one.

    What this now asserts is the rule the review prescribes: a degraded cycle
    still contributes a blocking miss when the normal feed RELEVANT TO THAT
    RELEASE was successfully validated. Here movies_all validated and tv_all
    failed, so the cycle is degraded and the movie miss still blocks. Same
    protection, on a defensible evidence boundary instead of a cycle proxy.

    The other half is
    test_a_degraded_cycle_with_no_valid_relevant_feed_does_not_block -- the case
    the original assertion wrongly admitted.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(
        db, uuid="incomplete-miss",
        completed_at="2026-07-21T00:00:00+00:00",
        # outcome matches what compare_shadow actually labels a mixed-feed
        # cycle: incomplete_feeds, while still persisting its attributable miss
        # rows. The review noted the old fixture said relevant_miss, which no
        # longer reflects the production writer. Miss accounting is independent
        # of the eligible-window outcome, so the assertion is unaffected -- but
        # a fixture that cannot occur in production is a trap for the next
        # reader.
        normal=0, rss=1, listing=1, misses=1, outcome="incomplete_feeds",
        feed_outcomes={"movies_all": "changed", "tv_all": "failed"},
    )
    _insert_miss(db, uuid="incomplete-miss", media_type="movie",
                 url="https://hdencode.org/a-movie-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["successful_cycles"] == 0, (
        "a degraded cycle must still not advance the window length")
    assert summary["relevant_misses"] == 1, (
        "a miss whose own feed validated must still block in a degraded cycle "
        "-- this is the 2026-07-21 protection")


def test_a_degraded_cycle_with_no_valid_relevant_feed_does_not_block(tmp_path):
    """The half the original assertion wrongly admitted.

    Both normal feeds failed, so candidate_urls was entirely the persisted
    snapshot from an earlier cycle. Nothing here can establish that the feed
    lacked the release. This is the case that kept the live window red for 15
    days.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(
        db, uuid="degraded-nothing-valid",
        completed_at="2026-07-21T00:00:00+00:00",
        normal=0, rss=1, listing=1, misses=1, outcome="relevant_miss",
        feed_outcomes={"movies_all": "failed", "tv_all": "failed"},
    )
    _insert_miss(db, uuid="degraded-nothing-valid", media_type="movie",
                 url="https://hdencode.org/a-movie-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["relevant_misses"] == 0
    # AND it must be flagged. A 2026-08-06 review pointed out that this test
    # previously asserted only the zero count, which certified the fail-open it
    # was meant to guard against: compare_shadow could not have written a row
    # whose own feed was unobserved, so this store is self-contradictory and must
    # block rather than merely count zero.
    assert any("unsupported_by_provenance" in r
               for r in summary["miss_evidence_integrity"]), (
        "an unsupported miss row was silently discarded")


def test_a_catchup_only_cycle_cannot_validate_a_comparison(tmp_path):
    """A catch-up fetch must not admit misses.

    The refuted proxy counted this: poll_cycle sums `requested` over
    normal + catch-up feeds, so a catch-up fetch while both normal feeds were
    not_due gave rss_requests=1. Provenance records only the normal feeds, so
    there is nothing to validate against.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(
        db, uuid="catchup-only",
        completed_at="2026-07-21T00:00:00+00:00",
        normal=0, rss=1, listing=4, misses=8, outcome="relevant_miss",
        feed_outcomes={},
    )
    for i in range(8):
        _insert_miss(db, uuid="catchup-only", media_type="movie",
                     url=f"https://hdencode.org/m{i}-2026-1080p-x-1-gb")
    assert db.get_hdencode_shadow_summary()["relevant_misses"] == 0


def test_legacy_rows_are_bounded_conservatively(tmp_path):
    """Pre-attribution rows (provenance NULL) use the cycle-level rule.

    The live window holds 300 such rows and none can be graded under
    attribution -- nothing recorded which feed succeeded. Counting them only
    when both normal feeds completed is a strict LOWER bound on blocking
    misses, because a mixed cycle contributes nothing here whereas attribution
    would admit its valid half.

    DIRECTION OF THAT BOUND. It guarantees the gate never FALSELY ACCUSES the
    feed of a miss. It does NOT establish health: finding zero blockers in the
    smaller admitted set says nothing about the larger attribution-valid set,
    since an omitted mixed-cycle row could itself be permanently missing. An
    earlier revision of this docstring claimed the bound "can understate
    health, never overstate it", which is exactly backwards.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="legacy-complete",
                  completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=3,
                  outcome="relevant_miss")
    _insert_cycle(db, uuid="legacy-degraded",
                  completed_at="2026-07-22T00:00:00+00:00",
                  normal=0, rss=1, listing=4, misses=97,
                  outcome="relevant_miss")
    assert db.get_hdencode_shadow_summary()["relevant_misses"] == 3


class _Registry:
    lifespan_generation = 1
    config = {}
    scanner = None
    db = None

    def owns_lifespan(self, _generation):
        return True


class _FeedDb:
    def __init__(self, present=True):
        self.present = present

    def get_hdencode_feed_state(self, key):
        if not self.present:
            return {}
        return {"feed_key": key, "last_checked_at": "2026-07-21T00:00:00+00:00"}


def test_restart_marker_is_process_lifetime_not_service_lifetime():
    scanner = BackgroundScanner(_Registry())
    assert scanner._rss_first_cycle_after_startup is True
    scanner._rss_first_cycle_after_startup = False
    assert scanner._rss_first_cycle_after_startup is False


def test_the_gate_does_not_trust_the_stored_count(tmp_path):
    """A wrong relevant_miss_count cannot inflate or deflate the gate.

    The summary re-derives attribution from the miss rows and the cycle's
    provenance using the same pure function the writer used. This is the
    review's standing point: two consumers agreeing does not make the producer
    valid. Here the cycle claims 99 misses and carries one attributable row, and
    the gate reports 1.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="lying-count",
                  completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=99,
                  outcome="relevant_miss",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    _insert_miss(db, uuid="lying-count", media_type="movie",
                 url="https://hdencode.org/only-one-2026-2160p-x-9-gb")
    assert db.get_hdencode_shadow_summary()["relevant_misses"] == 1


# ── evidence integrity (2026-08-06 review, Finding 2) ────────────────────────
#
# Each of these silently contributed ZERO before the fix, which DEFLATED the
# gate. Round 2 claimed a writer bug or forgetful caller could not move the
# gate; the review showed both could. These pin the correction.

def _cycle_with_raw_provenance(db, *, uuid, raw, misses=1,
                               completed_at="2026-07-21T00:00:00+00:00"):
    """Insert a cycle with provenance written EXACTLY as given (may be invalid)."""
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO hdencode_shadow_cycles (
               cycle_uuid, started_at, completed_at, normal_feeds_complete,
               rss_requests, listing_requests, rss_count, listing_count,
               duplicate_count, feed_only_count, listing_only_count,
               relevant_miss_count, request_reduction_pct, catchup_used,
               restart_recovery, outcome, details_json, normal_feed_outcomes
           ) VALUES (?, ?, ?, 1, 2, 10, 0, 0, 0, 0, 0, ?, 0, 0, 0,
                     'relevant_miss', '{}', ?)""",
        (uuid, completed_at, completed_at, misses, raw),
    )
    conn.commit()


def test_malformed_provenance_blocks_instead_of_counting_zero(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _cycle_with_raw_provenance(db, uuid="bad-json", raw="{not json at all")
    _insert_miss(db, uuid="bad-json", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["miss_evidence_integrity"], "corrupt provenance must be flagged"
    assert any("unparseable" in r for r in summary["miss_evidence_integrity"])
    readiness = db.get_hdencode_rss_readiness(min_cycles=1, min_days=0)
    assert "miss_evidence_integrity_failed" in readiness["reasons"]
    assert readiness["ready"] is False


def test_provenance_that_is_not_an_object_blocks(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _cycle_with_raw_provenance(db, uuid="a-list", raw='["movies_all"]')
    _insert_miss(db, uuid="a-list", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert any("not_an_object" in r for r in summary["miss_evidence_integrity"])


def test_a_miss_row_with_supplied_empty_provenance_blocks(tmp_path):
    """The forgetful-caller path.

    record_hdencode_shadow_comparison serializes a missing
    normal_feed_outcomes as {} rather than NULL. compare_shadow could never
    attribute a row with no observed feed, so a miss row against empty
    provenance is contradictory -- it means the row was inserted directly or the
    writer dropped the field. Previously it silently contributed zero.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _cycle_with_raw_provenance(db, uuid="empty-prov", raw="{}")
    _insert_miss(db, uuid="empty-prov", media_type="tv",
                 url="https://hdencode.org/s-s01-1080p-x-5-gb")
    summary = db.get_hdencode_shadow_summary()
    assert any("empty_provenance" in r for r in summary["miss_evidence_integrity"])
    assert summary["relevant_misses"] == 0
    assert "miss_evidence_integrity_failed" in db.get_hdencode_rss_readiness(
        min_cycles=1, min_days=0)["reasons"]


def test_a_count_with_no_rows_blocks(tmp_path):
    """A cycle claiming misses with nothing on disk to support them."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="count-no-rows",
                  completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=4,
                  outcome="relevant_miss",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    summary = db.get_hdencode_shadow_summary()
    assert any("count_without_rows" in r
               for r in summary["miss_evidence_integrity"])


def test_a_count_that_disagrees_with_the_rows_blocks(tmp_path):
    """The lying-count case, now flagged rather than quietly corrected.

    Round 2 reported 1 here and called it protection. Reporting a number for
    self-contradictory evidence is not protection -- it hides that the store is
    inconsistent.
    """
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="lying", completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=99,
                  outcome="relevant_miss",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    _insert_miss(db, uuid="lying", media_type="movie",
                 url="https://hdencode.org/only-one-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["relevant_misses"] == 1, "the rows still decide the count"
    assert any("disagreement" in r for r in summary["miss_evidence_integrity"])
    assert "miss_evidence_integrity_failed" in db.get_hdencode_rss_readiness(
        min_cycles=1, min_days=0)["reasons"]


def test_consistent_evidence_raises_no_integrity_flag(tmp_path):
    """The positive control: a healthy store must stay silent."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="clean", completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=1,
                  outcome="relevant_miss",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    _insert_miss(db, uuid="clean", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["relevant_misses"] == 1
    assert summary["miss_evidence_integrity"] == []

# -- every row accounted for (2026-08-06 Round 3 review, Finding 1) -----------
#
# The Round 3 check incremented on a valid observation and did nothing otherwise,
# and reconciled counts only where relevant_miss_count > 0. So a row unsupported
# by its own provenance vanished whenever the stored count happened to equal the
# row count. These are the cases the review required.

def _integrity(db):
    return db.get_hdencode_shadow_summary()["miss_evidence_integrity"]


def _blocks(db):
    return "miss_evidence_integrity_failed" in db.get_hdencode_rss_readiness(
        min_cycles=1, min_days=0)["reasons"]


def test_one_row_count_one_relevant_feed_failed(tmp_path):
    """The exact false-pass the review constructed: counts agree, row invalid."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="c", completed_at="2026-07-21T00:00:00+00:00",
                  normal=0, rss=1, listing=1, misses=1,
                  outcome="incomplete_feeds",
                  feed_outcomes={"movies_all": "failed", "tv_all": "failed"})
    _insert_miss(db, uuid="c", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["relevant_misses"] == 0
    assert any("unsupported_by_provenance" in r
               for r in summary["miss_evidence_integrity"])
    assert _blocks(db)


def test_one_row_count_zero_relevant_feed_valid(tmp_path):
    """Stored zero with a row present: previously never reconciled at all."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="c", completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=0, outcome="success",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    _insert_miss(db, uuid="c", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    assert any("count_row_disagreement" in r for r in _integrity(db))
    assert _blocks(db)


def test_one_row_count_zero_relevant_feed_failed(tmp_path):
    """Both contradictions at once; both reported."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="c", completed_at="2026-07-21T00:00:00+00:00",
                  normal=0, rss=1, listing=1, misses=0,
                  outcome="incomplete_feeds",
                  feed_outcomes={"movies_all": "failed", "tv_all": "failed"})
    _insert_miss(db, uuid="c", media_type="tv",
                 url="https://hdencode.org/s-s01-1080p-x-5-gb")
    findings = _integrity(db)
    assert any("unsupported_by_provenance" in r for r in findings)
    assert any("count_row_disagreement" in r for r in findings)
    assert _blocks(db)


@pytest.mark.parametrize("media_type", [None, "film", "MOVIE", "", "tv-show"])
def test_media_type_outside_the_vocabulary_is_corrupt(tmp_path, media_type):
    """unknown is a legitimate classifier result; NULL and arbitrary text are
    not. Coercing these into "unknown" let them count whenever both feeds
    validated, which is how corrupt evidence reached the gate."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="c", completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=1,
                  outcome="relevant_miss",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    _insert_miss(db, uuid="c", media_type=media_type,
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    assert any("media_type_invalid" in r for r in _integrity(db))
    assert _blocks(db)


@pytest.mark.parametrize("media_type", ["movie", "tv", "unknown"])
def test_the_three_legitimate_media_types_are_accepted(tmp_path, media_type):
    """Positive control: the vocabulary check must not fire on valid values."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="c", completed_at="2026-07-21T00:00:00+00:00",
                  normal=1, rss=2, listing=10, misses=1,
                  outcome="relevant_miss",
                  feed_outcomes={"movies_all": "changed", "tv_all": "changed"})
    _insert_miss(db, uuid="c", media_type=media_type,
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    summary = db.get_hdencode_shadow_summary()
    assert summary["miss_evidence_integrity"] == []
    assert summary["relevant_misses"] == 1


def test_an_unrecognised_derived_marker_is_corrupt(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _cycle_with_raw_provenance(
        db, uuid="c", raw=json.dumps({"_derived_from": "something_invented"}))
    _insert_miss(db, uuid="c", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    assert any("derived_marker_unknown" in r for r in _integrity(db))
    assert _blocks(db)


def test_a_derived_marker_contradicting_its_cycle_is_corrupt(tmp_path):
    """The marker records the completeness it was derived from. If that
    disagrees with the cycle column, one of the two was rewritten."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _cycle_with_raw_provenance(
        db, uuid="c",
        raw=json.dumps({"_derived_from": "cycle_level_completeness",
                        "normal_feeds_complete": False}))
    _insert_miss(db, uuid="c", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    # _cycle_with_raw_provenance writes normal_feeds_complete=1.
    assert any("derived_marker_contradicts_cycle" in r for r in _integrity(db))
    assert _blocks(db)


def test_an_orphan_miss_row_is_detected(tmp_path):
    """The join cannot see these, and the declared foreign key is not proof they
    cannot exist: this connection does not enable PRAGMA foreign_keys."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO hdencode_shadow_misses "
        "(cycle_uuid, canonical_url, title, status, media_type) "
        "VALUES ('no-such-cycle', 'https://hdencode.org/x-1-gb', 'T',"
        " 'missing', 'movie')")
    conn.commit()
    assert any("orphan_miss_rows" in r for r in _integrity(db))
    assert _blocks(db)


def test_findings_are_categorised_for_diagnosis(tmp_path):
    """Readiness must block, but an operator needs to tell corruption from a
    coverage miss. The review asked for categorised counts, not one string."""
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _insert_cycle(db, uuid="c1", completed_at="2026-07-21T00:00:00+00:00",
                  normal=0, rss=1, listing=1, misses=1,
                  outcome="incomplete_feeds",
                  feed_outcomes={"movies_all": "failed", "tv_all": "failed"})
    _insert_miss(db, uuid="c1", media_type="movie",
                 url="https://hdencode.org/a-2026-2160p-x-9-gb")
    _cycle_with_raw_provenance(db, uuid="c2", raw="{broken",
                               completed_at="2026-07-22T00:00:00+00:00")
    _insert_miss(db, uuid="c2", media_type="movie",
                 url="https://hdencode.org/b-2026-2160p-x-9-gb")
    by_category = db.get_hdencode_shadow_summary()[
        "miss_evidence_integrity_by_category"]
    assert by_category.get("miss_row_unsupported_by_provenance") == 1
    assert by_category.get("provenance_unparseable") == 1
    assert _blocks(db)
