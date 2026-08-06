"""A miss may only be asserted from a comparison whose feed side fetched.

Background. compare_shadow() derives misses from listing_only = listing - rss.
When normal_feeds_complete is False the caller passes the LAST PERSISTED feed
snapshot (list_hdencode_current_feed_urls reads the database, not that cycle's
fetch), so listing_only is inflated by everything the feed had merely not
collected yet. The old code booked all of it as misses AND overwrote the
incomplete_feeds label that said the comparison was invalid.

Measured over 2026-07-22..2026-08-05: 41 such cycles produced 89 of 150
recorded misses; grading all 150 against later cycles found zero permanent
losses (median catch-up 1.10h, worst 4.06h).

These tests are written to fail against the old behaviour in BOTH directions.
The risk of this change is silencing a real miss, so the healthy-cycle cases
below are the ones that matter most: they must keep reporting.
"""
import pytest

from backend.hdencode_shadow import compare_shadow


# Two listing rows the feed does not have. 'missing' is a relevant state, so
# each is a miss candidate; 'ignored' must never be one either way.
LISTING = [
    {"url": "https://hdencode.org/a-2026-2160p-web-h265-x-9-9-gb",
     "status": "missing", "title": "A 2026 2160p"},
    {"url": "https://hdencode.org/b-2026-1080p-web-h264-y-5-5-gb",
     "status": "missing", "title": "B 2026 1080p"},
]
FEED_HAS_NEITHER = ["https://hdencode.org/z-2020-1080p-web-h264-q-1-1-gb"]


def cmp_(*, complete, rss_requests, rss=None, listing=None):
    return compare_shadow(
        rss_urls=FEED_HAS_NEITHER if rss is None else rss,
        listing_items=LISTING if listing is None else listing,
        rss_requests=rss_requests, listing_requests=7,
        normal_feeds_complete=complete)


class TestFeedNeverFetched:
    """rss_requests=0 -> the feed URL set is not this cycle's, so no miss.

    This is the ONLY excluded case. Completeness is not the criterion; see
    TestTheAuditRuleIsPreservedNotReversed below.
    """

    def test_no_misses_are_recorded(self):
        r = cmp_(complete=False, rss_requests=0)
        assert r.relevant_miss_count == 0
        assert r.relevant_misses == ()

    def test_the_invalid_label_survives(self):
        # The specific old bug: `if misses: outcome='relevant_miss'` ran
        # unconditionally and erased this.
        assert cmp_(complete=False, rss_requests=0).outcome == "incomplete_feeds"

    def test_diagnostic_detail_is_not_lost(self):
        # Dropping the miss rows must not drop the evidence. listing_only is
        # persisted in details_json, which is how the 41 cycles were diagnosed
        # in the first place -- if this regressed, the next investigation of the
        # same question would have nothing to read.
        r = cmp_(complete=False, rss_requests=0)
        assert r.listing_only_count == 2
        assert len(r.listing_only) == 2
        assert all("hdencode.org" in u for u in r.listing_only)

    def test_counts_still_describe_the_comparison(self):
        r = cmp_(complete=False, rss_requests=0)
        assert r.rss_count == 1
        assert r.listing_count == 2
        assert r.duplicate_count == 0


class TestTheAuditRuleIsPreservedNotReversed:
    """A degraded cycle that DID fetch must still report its misses.

    On 2026-07-21 a ChatGPT adversarial audit (f5e3c6e) added
    test_relevant_miss_blocks_even_when_cycle_is_incomplete, establishing that a
    degraded cycle must not be able to HIDE a real gap. This change narrows that
    rule to exclude only the zero-fetch case; it does not reverse it. These tests
    exist so that a later change cannot quietly widen the exclusion into the
    reversal, which is the failure mode the audit was guarding against.

    Empirically the narrowing is what the data supports: of the 90 contested
    records 89 are zero-fetch, and the single partial-fetch one (rss_requests=2,
    2026-07-28) grades green at 1.25h -- so keeping it costs nothing and losing
    it would have cost the protection.
    """

    def test_a_partial_fetch_still_reports_its_misses(self):
        # The real 2026-07-28 shape: feeds did not complete, but the feed side
        # DID fetch. rss_requests is the criterion, not completeness.
        r = cmp_(complete=False, rss_requests=2)
        assert r.relevant_miss_count == 2, (
            "narrowing became a reversal: a cycle that fetched must still "
            "report its misses (ChatGPT audit f5e3c6e)")
        assert r.relevant_misses != ()

    def test_but_the_invalid_label_is_still_not_overwritten(self):
        # Two independent bugs. The miss survives; the label saying the cycle
        # was degraded must ALSO survive, so the row stays distinguishable from
        # a clean cycle that found a gap.
        assert cmp_(complete=False, rss_requests=2).outcome == "incomplete_feeds"

    def test_one_request_is_enough_to_count(self):
        # Exactly the audit test's shape (normal=0, rss=1, misses>=1). If this
        # regresses, test_relevant_miss_blocks_even_when_cycle_is_incomplete in
        # test_hdencode_readiness_integrity.py breaks too.
        assert cmp_(complete=False, rss_requests=1).relevant_miss_count == 2


class TestFeedDidFetch:
    """normal_feeds_complete=True -> a real gap must still be reported."""

    def test_a_genuine_miss_is_still_recorded(self):
        r = cmp_(complete=True, rss_requests=4)
        assert r.relevant_miss_count == 2
        assert r.outcome == "relevant_miss"
        assert {m["canonical_url"] for m in r.relevant_misses} == {
            "https://hdencode.org/a-2026-2160p-web-h265-x-9-9-gb",
            "https://hdencode.org/b-2026-1080p-web-h264-y-5-5-gb"}

    def test_miss_rows_carry_title_and_status(self):
        r = cmp_(complete=True, rss_requests=4)
        for m in r.relevant_misses:
            assert m["status"] == "missing"
            assert m["title"]

    def test_a_clean_cycle_is_success(self):
        # Feed has everything the listing has -> no gap, no miss.
        r = cmp_(complete=True, rss_requests=4,
                 rss=[row["url"] for row in LISTING])
        assert r.relevant_miss_count == 0
        assert r.outcome == "success"
        assert r.duplicate_count == 2

    def test_irrelevant_states_are_not_misses(self):
        r = cmp_(complete=True, rss_requests=4,
                 listing=[{"url": "https://hdencode.org/c-2026-1080p-web-1-1-gb",
                           "status": "ignored", "title": "C"}])
        assert r.relevant_miss_count == 0
        assert r.outcome == "success"

    @pytest.mark.parametrize("state", ["missing", "upgrade", "missing_season",
                                       "dv_upgrade"])
    def test_every_state_seen_in_the_live_window_still_counts(self, state):
        # The 150 real records were 119 missing, 15 upgrade, 13 missing_season,
        # 3 dv_upgrade. All four must survive as reportable misses.
        r = cmp_(complete=True, rss_requests=4,
                 listing=[{"url": "https://hdencode.org/d-2026-2160p-web-2-2-gb",
                           "status": state, "title": "D"}])
        assert r.relevant_miss_count == 1, f"{state} stopped being a miss"
        assert r.outcome == "relevant_miss"


class TestTheTwoAxesAreIndependent:
    """Completeness and the presence of a gap must not be conflated."""

    @pytest.mark.parametrize("complete,has_gap,expect_outcome,expect_misses", [
        (True,  True,  "relevant_miss",    2),
        (True,  False, "success",          0),
        (False, True,  "incomplete_feeds", 0),
        (False, False, "incomplete_feeds", 0),
    ])
    def test_full_truth_table(self, complete, has_gap, expect_outcome,
                              expect_misses):
        r = cmp_(complete=complete, rss_requests=4 if complete else 0,
                 rss=FEED_HAS_NEITHER if has_gap
                     else [row["url"] for row in LISTING])
        assert r.outcome == expect_outcome
        assert r.relevant_miss_count == expect_misses
        # normal_feeds_complete must be reported as passed in, never inferred
        # from whether a gap was found.
        assert r.normal_feeds_complete is complete
