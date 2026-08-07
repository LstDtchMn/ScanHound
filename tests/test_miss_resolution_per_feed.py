"""Resolution must honour the SAME per-feed authority as miss creation.

THE REGRESSION THIS EXISTS TO PREVENT, found by peer review on 2026-08-07.

`compare_shadow` emits a miss when the feed responsible for THAT release was
observed. Its own comment says it: "a cycle where movies_all succeeded says
nothing about a tv_all gap." So a movie miss is legitimately recorded in a cycle
where `movies_all` validated and `tv_all` failed — a cycle whose
`normal_feeds_complete` is 0.

My first version of the resolution gate sourced misses with
`WHERE c.normal_feeds_complete = 1` and admitted observation cycles on the same
condition. That is the CYCLE-LEVEL rule five review rounds had replaced. It
dropped legitimately-recorded misses out of the gate entirely, so a real movie gap
stopped blocking because an unrelated TV feed had failed. A false-ready path, and
mine.

The five cases below are the discrimination tests the review required, plus the
reversal of `not_yet_assessable` to blocking.
"""
from datetime import datetime, timedelta, timezone

from backend.hdencode_shadow import (
    classify_miss_resolution,
    summarise_miss_resolutions,
)

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
MOVIE = "https://hdencode.org/some-movie-2160p/"
SHOW = "https://hdencode.org/some-show-s03-1080p/"

BOTH = {"movies_all": "changed", "tv_all": "changed"}
MOVIES_ONLY = {"movies_all": "changed", "tv_all": "failed"}
TV_ONLY = {"movies_all": "failed", "tv_all": "not_modified"}


def cyc(hours, outcomes, *, listing_only=(), feed_only=()):
    return {"at": T0 + timedelta(hours=hours), "outcomes": outcomes,
            "listing_only": set(listing_only), "feed_only": set(feed_only)}


class TestAMixedCycleIsValidEvidenceForItsOwnFeed:

    def test_a_movies_valid_cycle_resolves_a_movie_even_if_tv_failed(self):
        """CASE 3. The whole point: tv_all failing says nothing about a movie."""
        state, hours, _ = classify_miss_resolution(
            MOVIE, "movie", T0, [cyc(2, MOVIES_ONLY, feed_only=[MOVIE])])
        assert state == "acquired", (
            "a cycle where movies_all validated is good evidence about a movie; "
            "rejecting it because tv_all failed is the cycle-level rule this "
            "project replaced")
        assert hours == 2.0

    def test_a_movies_valid_cycle_still_showing_it_is_a_real_failure(self):
        """CASE 4. It must count as observed-unacquired, NOT 'not yet assessable'
        — otherwise a real gap hides behind an unrelated feed's failure."""
        state, _, detail = classify_miss_resolution(
            MOVIE, "movie", T0, [cyc(3, MOVIES_ONLY, listing_only=[MOVIE])])
        assert state == "never_acquired", detail

    def test_a_tv_valid_cycle_cannot_resolve_a_movie(self):
        """CASE 1 mirror. Per-feed cuts both ways: this must NOT resolve."""
        state, _, _ = classify_miss_resolution(
            MOVIE, "movie", T0, [cyc(2, TV_ONLY, feed_only=[MOVIE])])
        assert state == "not_yet_assessable", (
            "tv_all validating says nothing about a movie, so this cycle is not "
            "an observation for this row and must not resolve it")

    def test_a_tv_valid_cycle_resolves_tv(self):
        """CASE 2. The TV mirror of the movie case."""
        state, hours, _ = classify_miss_resolution(
            SHOW, "tv", T0, [cyc(4, TV_ONLY, feed_only=[SHOW])])
        assert state == "acquired"
        assert hours == 4.0

    def test_a_movies_valid_cycle_cannot_resolve_tv(self):
        state, _, _ = classify_miss_resolution(
            SHOW, "tv", T0, [cyc(4, MOVIES_ONLY, feed_only=[SHOW])])
        assert state == "not_yet_assessable"

    def test_unknown_media_type_requires_BOTH_feeds(self):
        """CASE 5. An unattributable row cannot be dismissed on one healthy feed."""
        assert classify_miss_resolution(
            MOVIE, "unknown", T0,
            [cyc(2, MOVIES_ONLY, feed_only=[MOVIE])])[0] == "not_yet_assessable"
        assert classify_miss_resolution(
            MOVIE, "unknown", T0,
            [cyc(2, TV_ONLY, feed_only=[MOVIE])])[0] == "not_yet_assessable"
        assert classify_miss_resolution(
            MOVIE, "unknown", T0,
            [cyc(2, BOTH, feed_only=[MOVIE])])[0] == "acquired"

    def test_a_failed_feed_is_not_an_observation(self):
        """'failed' is an attempt, not evidence. Only changed/not_modified count."""
        assert classify_miss_resolution(
            MOVIE, "movie", T0,
            [cyc(2, {"movies_all": "failed", "tv_all": "failed"},
                 feed_only=[MOVIE])])[0] == "not_yet_assessable"


class TestPendingBlocksAgain:
    """REVERSED on review. I had made not_yet_assessable non-blocking so the gate
    could pass. The review showed the composition that makes it unsafe: the shadow
    comparison is recorded only while discovery_mode == 'rss_shadow', so promoting
    to rss_primary stops producing the observations a pending row needs. The gate
    would open on evidence its own promoted mode destroys."""

    def test_not_yet_assessable_counts_toward_blocking(self):
        summary = summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": "movie", "at": T0}], [cyc(-1, BOTH)])
        assert summary["not_yet_assessable"] == 1
        assert summary["blocking"] == 1, (
            "a live unresolved row must not stop blocking merely because it is "
            "the newest one")

    def test_an_all_acquired_population_is_still_clean(self):
        """POSITIVE CONTROL. Making pending block must not make everything block;
        a genuinely resolved population must still pass."""
        cycles = [cyc(1, BOTH, feed_only=[MOVIE, SHOW])]
        summary = summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": "movie", "at": T0},
             {"url": SHOW, "media_type": "tv", "at": T0}], cycles)
        assert summary["acquired"] == 2
        assert summary["blocking"] == 0

    def test_a_bogus_media_type_blocks_rather_than_resolving(self):
        """A media_type that is PRESENT but outside the vocabulary is corrupt
        evidence and must not be coerced into something resolvable.

        NARROWED after the live measurement: this originally also asserted that
        `None` behaves the same way. It does not, deliberately — NULL means the
        row predates the media_type column (70 of 72 live rows) and reads as
        "unknown". Treating the two alike blocked the gate on every historical
        row. See TestLegacyRowsWithNoMediaType.
        """
        summary = summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": "nonsense", "at": T0}],
            [cyc(1, BOTH, feed_only=[MOVIE])])
        assert summary["undetermined"] == 1
        assert summary["acquired"] == 0
        assert summary["blocking"] == 1


class TestLegacyCyclesFallBackRatherThanGoBlind:
    """The correction to my correction, and why both directions matter.

    319 of 331 live cycles predate per-feed provenance (`normal_feed_outcomes` is
    NULL). My first fix applied the per-feed rule to every cycle, so those 319
    became evidence for nothing and 70 genuinely-acquired rows turned into
    "undetermined". That is not stricter, it is blind -- and it would have been a
    false-BLOCK to match the false-ready it replaced.

    The rule: real provenance wins where it exists; where it does not, the cycle's
    own `normal_feeds_complete` is the best available evidence. The review allowed
    exactly this for legacy rows.
    """

    def test_a_legacy_complete_cycle_still_resolves(self):
        legacy = {"at": T0 + timedelta(hours=2), "outcomes": None,
                  "cycle_complete": True,
                  "listing_only": set(), "feed_only": {MOVIE}}
        assert classify_miss_resolution(MOVIE, "movie", T0, [legacy])[0] == "acquired"

    def test_a_legacy_incomplete_cycle_is_not_evidence(self):
        legacy = {"at": T0 + timedelta(hours=2), "outcomes": None,
                  "cycle_complete": False,
                  "listing_only": set(), "feed_only": {MOVIE}}
        assert classify_miss_resolution(
            MOVIE, "movie", T0, [legacy])[0] == "not_yet_assessable"

    def test_real_provenance_overrides_the_completeness_flag(self):
        """Where per-feed data exists it is authoritative in BOTH directions: it
        can admit a cycle the flag would reject, and reject one the flag admits."""
        admits = {"at": T0 + timedelta(hours=2), "outcomes": MOVIES_ONLY,
                  "cycle_complete": False,
                  "listing_only": set(), "feed_only": {MOVIE}}
        assert classify_miss_resolution(MOVIE, "movie", T0,
                                        [admits])[0] == "acquired"
        rejects = {"at": T0 + timedelta(hours=2), "outcomes": TV_ONLY,
                   "cycle_complete": True,
                   "listing_only": set(), "feed_only": {MOVIE}}
        assert classify_miss_resolution(MOVIE, "movie", T0,
                                        [rejects])[0] == "not_yet_assessable"


class TestLegacyRowsWithNoMediaType:
    """The second correction the live measurement forced.

    `media_type` was added by the RSS accounting work, so every miss recorded
    before that migration has NULL -- 70 of 72 rows in the live database. My first
    pass treated NULL as corrupt and blocked on all of them: acquired went 62 -> 1
    and readiness blocked for the wrong reason. A false BLOCK is not safer than the
    false ready it replaced, it is just differently wrong.

    NULL reads as "unknown": both feeds required where per-feed data exists, the
    cycle's own completeness where it does not. A value that is PRESENT but outside
    the vocabulary stays corrupt -- that distinction is the whole point.
    """

    def test_a_null_media_type_can_still_resolve_via_a_complete_cycle(self):
        legacy_cycle = {"at": T0 + timedelta(hours=2), "outcomes": None,
                        "cycle_complete": True,
                        "listing_only": set(), "feed_only": {MOVIE}}
        summary = summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": None, "at": T0}], [legacy_cycle])
        assert summary["acquired"] == 1, (
            "a pre-attribution row must still be resolvable; blocking on all of "
            "them is a false block")
        assert summary["blocking"] == 0

    def test_a_null_media_type_requires_BOTH_feeds_when_provenance_exists(self):
        """Conservative where the finer data exists: an unattributable row cannot
        be dismissed on one healthy feed."""
        assert summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": None, "at": T0}],
            [cyc(2, MOVIES_ONLY, feed_only=[MOVIE])])["acquired"] == 0
        assert summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": None, "at": T0}],
            [cyc(2, BOTH, feed_only=[MOVIE])])["acquired"] == 1

    def test_a_present_but_bogus_media_type_is_still_corrupt(self):
        """NULL is a legacy gap; 'nonsense' is corruption. Do not merge them."""
        summary = summarise_miss_resolutions(
            [{"url": MOVIE, "media_type": "nonsense", "at": T0}],
            [cyc(2, BOTH, feed_only=[MOVIE])])
        assert summary["acquired"] == 0
        assert summary["undetermined"] == 1
        assert "invalid media_type" in summary["rows"][0]["detail"]
