"""A miss may only be asserted when the feed responsible for it was observed.

HISTORY, because this file has been wrong once already. Its first version gated
on ``rss_requests > 0``. A 2026-08-06 peer review refuted that, and the
production code confirms the refutation:

    # hdencode_rss_service.poll_cycle
    feeds = normal + (list(catchup_feeds()) if include_catchup else [])
    ...
    "requests": sum(1 for r in results if r.get("requested"))

    # hdencode_rss_service.poll_feed, exception path
    return {"feed": feed.key, "outcome": "failed", ..., "requested": True}

So ``rss_requests > 0`` is satisfied by a catch-up-only cycle, by an attempted
request that failed, and by a cycle where one normal feed succeeded and the other
did not. In all of those, candidate_urls is wholly or partly the persisted
snapshot from an earlier cycle. The count means "something was attempted
somewhere", not "this release's feed was validly observed" -- and the earlier
version of this file codified that proxy as a test rather than proving it.

The rule now under test is per-feed attribution: a listing row is booked as a
miss only when the normal feed that should have carried it
(movies_all for a film, tv_all for a series) returned changed or not_modified in
THAT cycle.

These are unit-level. tests/test_hdencode_shadow_provenance_paths.py drives the
real HDEncodeRSSService.poll_cycle for the same rules, which is what the review
required -- injecting an integer cannot test what that integer means.
"""
import pytest

from backend.hdencode_shadow import (
    attribute_listing_media_type,
    compare_shadow,
    feed_observation_valid,
    normal_feed_outcomes_from_results,
)

# category is what production supplies (scanner_service sets "4k"/"remux"
# for the movie listings). A fixture WITHOUT it is now "unknown", which is
# correct: absence of an sNN token is not evidence of a film.
MOVIE = {"url": "https://hdencode.org/a-movie-2026-2160p-web-x-9-9-gb",
         "status": "missing", "title": "A Movie 2026", "category": "4k"}
SHOW = {"url": "https://hdencode.org/a-show-s02-1080p-web-y-5-5-gb",
        "status": "missing", "title": "A Show S02"}
FEED_HAS_NEITHER = ["https://hdencode.org/z-2020-1080p-web-q-1-1-gb"]

BOTH_OK = {"movies_all": "changed", "tv_all": "not_modified"}
MOVIE_OK_TV_DEAD = {"movies_all": "changed", "tv_all": "failed"}
TV_OK_MOVIE_DEAD = {"movies_all": "failed", "tv_all": "changed"}
BOTH_DEAD = {"movies_all": "failed", "tv_all": "failed"}
NOTHING_RAN = {}


def cmp_(*, outcomes, listing=None, rss_requests=2, complete=True):
    return compare_shadow(
        rss_urls=FEED_HAS_NEITHER,
        listing_items=listing if listing is not None else [MOVIE, SHOW],
        rss_requests=rss_requests, listing_requests=7,
        normal_feeds_complete=complete, normal_feed_outcomes=outcomes)


def titles(records):
    return sorted(r["title"] for r in records)


class TestAttribution:
    """Which feed should have carried a row. Pure, so it is tested directly."""

    @pytest.mark.parametrize("url", [
        "https://hdencode.org/will-and-grace-s07-1080p-amzn-x-42-7-gb",
        "https://hdencode.org/show-s01e04-720p-web-y-2-0-gb",
    ])
    def test_a_season_marker_is_positive_tv_evidence(self, url):
        assert attribute_listing_media_type({"url": url}) == "tv"

    @pytest.mark.parametrize("url", [
        "https://hdencode.org/some-film-2026-2160p-web-z-9-0-gb",
        "https://hdencode.org/dune-part-two-2024-2160p-uhd-remux-50-2-gb",
    ])
    def test_a_slug_alone_is_not_movie_evidence(self, url):
        """These asserted "movie" until the 2026-08-06 review.

        The slug regex is positive TV evidence only. Reading its ABSENCE as a
        film is the inference that suppressed a real TV miss. Note the second
        URL contains "remux" as free text -- a substring match on category
        vocabulary would wrongly call it affirmative movie evidence, which is
        why category is read from the field and never from the slug.
        """
        assert attribute_listing_media_type({"url": url}) == "unknown"

    def test_a_season_field_beats_the_slug(self):
        # Production path: MediaItem carries season, so no slug guessing.
        assert attribute_listing_media_type(
            {"url": "https://hdencode.org/x-2026-1080p-a-1-gb", "season": 3}) == "tv"

    def test_a_series_only_status_is_tv(self):
        assert attribute_listing_media_type(
            {"url": "https://hdencode.org/x-2026-1080p-a-1-gb",
             "status": "missing_season"}) == "tv"

    def test_season_none_is_not_tv_but_is_not_movie_evidence_either(self):
        """season=None must not read as a series -- and must not imply a film.

        This assertion was "movie" until a 2026-08-06 peer review pointed out
        that treating the absence of TV evidence as movie evidence is the defect,
        not the safeguard. With no category and no sNN token there is nothing
        affirmative, so the answer is unknown -- which requires both feeds.
        """
        assert attribute_listing_media_type(
            {"url": "https://hdencode.org/x-2026-1080p-a-1-gb",
             "season": None}) == "unknown"

    def test_a_tv_category_with_no_sNN_and_no_season_is_still_tv(self):
        """The false-pass case the review described.

        A TV Packs item whose filename does not match the Sxx pattern. Before
        the fix this attributed to movies_all, so during a movies-failed /
        tv-healthy cycle a genuine TV miss was silently suppressed.
        """
        assert attribute_listing_media_type(
            {"category": "tv", "season": None, "episodes": None,
             "status": "missing",
             "url": "https://hdencode.org/some-show-1080p-web-x-5-gb"}) == "tv"

    def test_search_category_carries_no_evidence(self):
        # scanner_service labels search results "search". They identify nothing.
        assert attribute_listing_media_type(
            {"category": "search",
             "url": "https://hdencode.org/thing-2026-1080p-x-1-gb"}) == "unknown"

    def test_conflicting_signals_resolve_to_unknown(self):
        # Neither misattribution direction is safe, so a disagreement must not
        # be resolved by precedence.
        assert attribute_listing_media_type(
            {"category": "4k", "season": 2,
             "url": "https://hdencode.org/x-1080p-1-gb"}) == "unknown"

    def test_the_basis_is_reported(self):
        from backend.hdencode_shadow import attribution_evidence
        media_type, basis = attribution_evidence(
            {"category": "tv", "url": "https://hdencode.org/x-s03-1080p-1-gb"})
        assert media_type == "tv"
        assert "category=tv" in basis and "slug_season_marker" in basis

    def test_unattributable_is_its_own_answer(self):
        # Deliberately NOT defaulted to "movie". Guessing wrong in that
        # direction would let a TV release be checked against a failed movie
        # feed and silently dropped -- a false pass, the exact class of failure
        # this change removes.
        assert attribute_listing_media_type({"url": "", "status": "missing"}) == "unknown"


class TestFeedValidity:
    """The review's disagreeing cases, each pinned."""

    def test_catchup_only_is_not_an_observation(self):
        # A catch-up feed request while both normal feeds were not_due.
        assert feed_observation_valid("movie", NOTHING_RAN) is False
        assert feed_observation_valid("tv", NOTHING_RAN) is False

    def test_not_due_is_not_an_observation(self):
        assert feed_observation_valid(
            "movie", {"movies_all": "not_due", "tv_all": "not_due"}) is False

    def test_an_attempted_failure_is_not_an_observation(self):
        assert feed_observation_valid("movie", BOTH_DEAD) is False

    def test_the_relevant_feed_decides(self):
        assert feed_observation_valid("movie", MOVIE_OK_TV_DEAD) is True
        assert feed_observation_valid("tv", MOVIE_OK_TV_DEAD) is False
        assert feed_observation_valid("tv", TV_OK_MOVIE_DEAD) is True
        assert feed_observation_valid("movie", TV_OK_MOVIE_DEAD) is False

    def test_unknown_requires_both(self):
        assert feed_observation_valid("unknown", MOVIE_OK_TV_DEAD) is False
        assert feed_observation_valid("unknown", BOTH_OK) is True

    def test_catchup_feeds_never_enter_provenance(self):
        got = normal_feed_outcomes_from_results([
            {"feed": "movies_all", "outcome": "changed"},
            {"feed": "tv_all", "outcome": "failed"},
            {"feed": "movies_10", "outcome": "changed"},
            {"feed": "tv_webdl", "outcome": "changed"},
        ])
        assert got == {"movies_all": "changed", "tv_all": "failed"}


class TestTheRefutedProxyCannotComeBack:
    """rss_requests must have no influence on whether a miss counts."""

    @pytest.mark.parametrize("rss_requests", [0, 1, 2, 17, 999])
    def test_no_request_count_rescues_an_invalid_comparison(self, rss_requests):
        r = cmp_(outcomes=NOTHING_RAN, rss_requests=rss_requests, complete=False)
        assert r.relevant_miss_count == 0, (
            f"rss_requests={rss_requests} made an unobserved comparison count")

    @pytest.mark.parametrize("rss_requests", [0, 1, 999])
    def test_no_request_count_suppresses_a_valid_one(self, rss_requests):
        r = cmp_(outcomes=BOTH_OK, rss_requests=rss_requests)
        assert r.relevant_miss_count == 2


class TestPartialCycles:
    """The case a cycle-level boolean cannot express."""

    def test_a_movie_gap_blocks_when_only_the_tv_feed_failed(self):
        r = cmp_(outcomes=MOVIE_OK_TV_DEAD, complete=False)
        assert titles(r.relevant_misses) == ["A Movie 2026"]
        assert titles(r.unattributable) == ["A Show S02"]

    def test_a_tv_gap_blocks_when_only_the_movie_feed_failed(self):
        r = cmp_(outcomes=TV_OK_MOVIE_DEAD, complete=False)
        assert titles(r.relevant_misses) == ["A Show S02"]
        assert titles(r.unattributable) == ["A Movie 2026"]

    def test_both_block_when_both_feeds_are_healthy(self):
        r = cmp_(outcomes=BOTH_OK)
        assert titles(r.relevant_misses) == ["A Movie 2026", "A Show S02"]
        assert r.unattributable == ()

    def test_neither_blocks_when_both_feeds_died(self):
        r = cmp_(outcomes=BOTH_DEAD, complete=False)
        assert r.relevant_miss_count == 0
        assert titles(r.unattributable) == ["A Movie 2026", "A Show S02"]

    def test_each_miss_records_the_feed_it_was_attributed_to(self):
        r = cmp_(outcomes=BOTH_OK)
        assert {m["title"]: m["media_type"] for m in r.relevant_misses} == {
            "A Movie 2026": "movie", "A Show S02": "tv"}

    def test_each_miss_records_why(self):
        r = cmp_(outcomes=BOTH_OK)
        basis = {m["title"]: m["attribution_basis"] for m in r.relevant_misses}
        assert "category=4k" in basis["A Movie 2026"]
        assert "slug_season_marker" in basis["A Show S02"]


class TestEvidenceIsNotDiscarded:
    """Dropping a miss CLAIM must not drop the underlying observation."""

    def test_listing_only_survives(self):
        r = cmp_(outcomes=BOTH_DEAD, complete=False)
        assert r.listing_only_count == 2
        assert len(r.listing_only) == 2

    def test_suppressed_rows_are_kept_with_a_reason(self):
        r = cmp_(outcomes=BOTH_DEAD, complete=False)
        assert len(r.unattributable) == 2
        assert all(rec.get("unattributable_reason") for rec in r.unattributable)

    def test_provenance_is_recorded(self):
        assert cmp_(outcomes=MOVIE_OK_TV_DEAD).normal_feed_outcomes == MOVIE_OK_TV_DEAD

    def test_derived_provenance_is_not_fabricated(self):
        # A caller supplying no provenance falls back to the cycle-level rule,
        # but the RECORD must not claim feed outcomes that never happened.
        r = compare_shadow(rss_urls=FEED_HAS_NEITHER, listing_items=[MOVIE],
                           rss_requests=2, listing_requests=7,
                           normal_feeds_complete=True)
        assert r.relevant_miss_count == 1, "old callers must keep working"
        assert r.normal_feed_outcomes.get("_derived_from") == "cycle_level_completeness"
        assert "movies_all" not in r.normal_feed_outcomes


class TestOutcomeLabel:
    """The invalidity label must survive, independently of the miss count."""

    @pytest.mark.parametrize("outcomes,complete,expected", [
        (BOTH_OK, True, "relevant_miss"),
        (MOVIE_OK_TV_DEAD, False, "incomplete_feeds"),
        (BOTH_DEAD, False, "incomplete_feeds"),
        (NOTHING_RAN, False, "incomplete_feeds"),
    ])
    def test_label(self, outcomes, complete, expected):
        assert cmp_(outcomes=outcomes, complete=complete).outcome == expected

    def test_a_clean_cycle_with_no_gap_is_success(self):
        r = compare_shadow(rss_urls=[MOVIE["url"], SHOW["url"]],
                           listing_items=[MOVIE, SHOW], rss_requests=2,
                           listing_requests=7, normal_feeds_complete=True,
                           normal_feed_outcomes=BOTH_OK)
        assert r.outcome == "success"
        assert r.relevant_miss_count == 0
        assert r.duplicate_count == 2

    @pytest.mark.parametrize("state", ["missing", "upgrade", "missing_season",
                                       "dv_upgrade"])
    def test_every_state_seen_live_still_counts(self, state):
        # The 150 live records were 119 missing, 15 upgrade, 13 missing_season,
        # 3 dv_upgrade. All four must remain reportable.
        row = {"url": "https://hdencode.org/d-2026-2160p-web-2-2-gb",
               "status": state, "title": "D"}
        r = cmp_(outcomes=BOTH_OK, listing=[row])
        assert r.relevant_miss_count == 1, f"{state} stopped being a miss"

    def test_an_irrelevant_state_is_never_a_miss(self):
        row = {"url": "https://hdencode.org/c-2026-1080p-1-1-gb",
               "status": "ignored", "title": "C"}
        r = cmp_(outcomes=BOTH_OK, listing=[row])
        assert r.relevant_miss_count == 0
        assert r.unattributable == ()
