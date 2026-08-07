"""Mixed-feed cycles must reach the per-feed rule THROUGH THE DATABASE READER.

THE DEFECT THIS EXISTS TO PREVENT. Round 2 implemented `cycle_is_valid_evidence_for()`
correctly and tested it directly with hand-built cycle dicts — 13 tests, all passing.
Peer review then found the helper was unreachable: `get_hdencode_miss_resolution()`
filtered cycles to `outcome in ("success","relevant_miss")`, and `compare_shadow`
stores every aggregate-incomplete comparison as `"incomplete_feeds"`. So a cycle with
`movies_all=changed, tv_all=failed` — precisely the case the helper exists for — was
discarded before the helper could run.

Direct helper tests cannot catch that. **Every test in this file goes through the real
reader**, so the wiring is part of what is asserted.

The second half of the fix is why admitting `incomplete_feeds` is safe at all:
`_rss_normal_feeds_complete()` folds a listing-crawl error into
`normal_feeds_complete`, so that flag cannot distinguish "a feed failed" from "the
listing failed". `listing_complete` now records the listing arm separately, and
resolution requires BOTH authorities. The listing-incomplete control below is the test
that keeps that honest.
"""
import json

from backend.database import DatabaseManager

STAMP = "2026-08-06T12:00:00+00:00"
LATER = "2026-08-06T14:00:00+00:00"
MOVIE = "https://hdencode.org/a-movie-2160p/"
SHOW = "https://hdencode.org/a-show-s02-1080p/"

BOTH = {"movies_all": "changed", "tv_all": "changed"}
MOVIES_ONLY = {"movies_all": "changed", "tv_all": "failed"}
TV_ONLY = {"movies_all": "failed", "tv_all": "changed"}


def _cycle(db, uuid, at, *, outcomes, complete, listing_complete,
           listing_only=(), feed_only=(), outcome=None, miss=None,
           miss_media_type="movie"):
    """Insert a cycle exactly as record_hdencode_shadow_comparison would."""
    if outcome is None:
        outcome = "success" if complete else "incomplete_feeds"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hdencode_shadow_cycles "
            "(cycle_uuid, started_at, completed_at, normal_feeds_complete, "
            " rss_requests, listing_requests, rss_count, listing_count, "
            " duplicate_count, feed_only_count, listing_only_count, "
            " relevant_miss_count, request_reduction_pct, outcome, details_json, "
            " normal_feed_outcomes, listing_complete) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid, at, at, 1 if complete else 0, 2, 1, 10, 5, 3,
             len(feed_only), len(listing_only), 1 if miss else 0, 50.0, outcome,
             json.dumps({"listing_only": list(listing_only),
                         "feed_only": list(feed_only)}),
             None if outcomes is None else json.dumps(outcomes),
             None if listing_complete is None else (1 if listing_complete else 0)))
        if miss:
            conn.execute(
                "INSERT INTO hdencode_shadow_misses "
                "(cycle_uuid, canonical_url, media_type) VALUES (?,?,?)",
                (uuid, miss, miss_media_type))


def _states(db):
    res = db.get_hdencode_miss_resolution()
    return {r["url"]: r["state"] for r in res["rows"]}, res


class TestMixedFeedCyclesReachTheRuleThroughTheReader:

    def test_a_movies_valid_cycle_resolves_a_movie_miss(self, tmp_path):
        """THE ROUND-2 BUG. This cycle is stored as 'incomplete_feeds' because
        tv_all failed, and the reader used to discard it."""
        db = DatabaseManager(str(tmp_path / "mixed-resolve.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=MOVIES_ONLY, complete=False,
                   listing_complete=True, feed_only=[MOVIE])
            states, res = _states(db)
            assert states[MOVIE] == "acquired", (
                f"a movies_all-valid later cycle must resolve a movie miss even "
                f"though tv_all failed; got {states[MOVIE]}, rows={res['rows']}")
        finally:
            db.close()

    def test_a_movies_valid_cycle_still_showing_it_is_never_acquired(self, tmp_path):
        """CASE 4: it must count as observed-unacquired, not deferred."""
        db = DatabaseManager(str(tmp_path / "mixed-block.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=MOVIES_ONLY, complete=False,
                   listing_complete=True, listing_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "never_acquired", states
        finally:
            db.close()

    def test_the_tv_mirror(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "mixed-tv.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[SHOW], miss=SHOW,
                   miss_media_type="tv")
            _cycle(db, "c2", LATER, outcomes=TV_ONLY, complete=False,
                   listing_complete=True, feed_only=[SHOW])
            states, _ = _states(db)
            assert states[SHOW] == "acquired", states
        finally:
            db.close()

    def test_the_wrong_feed_cannot_resolve(self, tmp_path):
        """Per-feed cuts both ways: tv_all validating says nothing about a movie."""
        db = DatabaseManager(str(tmp_path / "mixed-wrong.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=TV_ONLY, complete=False,
                   listing_complete=True, feed_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "not_yet_assessable", states
        finally:
            db.close()

    def test_the_own_feed_failing_is_not_an_observation(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "mixed-failed.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER,
                   outcomes={"movies_all": "failed", "tv_all": "failed"},
                   complete=False, listing_complete=True, feed_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "not_yet_assessable", states
        finally:
            db.close()


class TestTheListingArmIsAnIndependentAuthority:
    """Why admitting 'incomplete_feeds' is safe: the listing half is checked too."""

    def test_a_broken_listing_cannot_resolve_even_with_a_healthy_feed(self, tmp_path):
        """THE CONTROL THE REVIEW REQUIRED. Without this, admitting
        'incomplete_feeds' would also admit cycles whose listing crawl errored --
        and the listing is the other half of the comparison."""
        db = DatabaseManager(str(tmp_path / "listing-broken.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=MOVIES_ONLY, complete=False,
                   listing_complete=False, feed_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "not_yet_assessable", (
                "the feed was healthy but the LISTING crawl failed, so this cycle "
                f"is not a valid observation; got {states[MOVIE]}")
        finally:
            db.close()

    def test_a_healthy_listing_plus_healthy_feed_does_resolve(self, tmp_path):
        """POSITIVE CONTROL for the pair, so the test above cannot pass merely
        because nothing ever resolves."""
        db = DatabaseManager(str(tmp_path / "listing-ok.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=MOVIES_ONLY, complete=False,
                   listing_complete=True, feed_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "acquired", states
        finally:
            db.close()


class TestLegacyCyclesThroughTheReader:
    """NULL provenance / NULL listing_complete must fall back, not go blind."""

    def test_a_legacy_complete_cycle_resolves(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "legacy-ok.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=None, complete=True,
                   listing_complete=None, feed_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "acquired", states
        finally:
            db.close()

    def test_a_legacy_incomplete_cycle_is_not_evidence(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "legacy-bad.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=BOTH, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=None, complete=False,
                   listing_complete=None, feed_only=[MOVIE])
            states, _ = _states(db)
            assert states[MOVIE] == "not_yet_assessable", states
        finally:
            db.close()


class TestTheColumnExists:
    def test_listing_complete_is_migrated(self, tmp_path):
        """The ALTER sits beside the shadow CREATEs, not in the shared list that
        runs before them -- an ALTER there fails 'no such table' and the guard only
        swallows 'duplicate column', leaving the column silently absent."""
        db = DatabaseManager(str(tmp_path / "schema.db"))
        try:
            cols = [r["name"] for r in db._query_dicts(
                "PRAGMA table_info(hdencode_shadow_cycles)", default=[])]
            assert "listing_complete" in cols, cols
        finally:
            db.close()


class TestTheCycleLevelMarkerFallsBack:
    """A validated `_derived_from` marker means "use the cycle-level rule".

    THE DEFECT THIS FIXES, from peer review. `_normal_feed_outcomes()` returned
    `{}` for such a marker -- an explicit "no feed was observed" -- and miss
    ADMISSION decides the legacy fallback on `is None`. So `{}` is not None, the
    marker got no fallback, and misses recorded under it were silently omitted from
    the gate entirely. The marker's whole purpose is to say "no per-feed data here",
    which is exactly the legacy case, so it must read as None.

    Collapsing `{}` and the marker together was the round-3 R2-4 finding.
    """

    MARKER = {"_derived_from": "cycle_level_completeness",
              "normal_feeds_complete": True}

    def test_a_miss_under_a_validated_marker_is_admitted_and_resolvable(
            self, tmp_path):
        db = DatabaseManager(str(tmp_path / "marker-ok.db"))
        try:
            _cycle(db, "c1", STAMP, outcomes=self.MARKER, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            _cycle(db, "c2", LATER, outcomes=None, complete=True,
                   listing_complete=None, feed_only=[MOVIE])
            res = db.get_hdencode_miss_resolution()
            assert len(res["rows"]) == 1, (
                "the miss must be ADMITTED; a marker is a legacy signal, not an "
                f"empty observation. rows={res['rows']}")
            assert res["rows"][0]["state"] == "acquired", res["rows"]
        finally:
            db.close()

    def test_a_malformed_marker_is_corrupt_not_a_licence_to_fall_back(
            self, tmp_path):
        """Validating the schema first: an unrecognised marker must not buy the
        fallback it is shaped like."""
        db = DatabaseManager(str(tmp_path / "marker-bad.db"))
        try:
            _cycle(db, "c1", STAMP,
                   outcomes={"_derived_from": "something_else"}, complete=True,
                   listing_complete=True, listing_only=[MOVIE], miss=MOVIE)
            res = db.get_hdencode_miss_resolution()
            assert any("derived_marker_invalid" in p
                       for p in res["evidence_problems"]), \
                res["evidence_problems"]
        finally:
            db.close()

    def test_a_marker_whose_flag_is_not_a_boolean_is_corrupt(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "marker-str.db"))
        try:
            _cycle(db, "c1", STAMP,
                   outcomes={"_derived_from": "cycle_level_completeness",
                             "normal_feeds_complete": "false"},
                   complete=True, listing_complete=True,
                   listing_only=[MOVIE], miss=MOVIE)
            res = db.get_hdencode_miss_resolution()
            assert any("derived_marker_invalid" in p
                       for p in res["evidence_problems"]), (
                'the string "false" is truthy in Python; a pseudo-boolean must '
                "not pass as a validated marker")
        finally:
            db.close()
