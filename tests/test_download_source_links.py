"""What authorises a source link on a LIVE download row, and what the date means.

Peer review Finding 1 rejected the previous answer. Links used to be resolved by
JDownloader package NAME, refusing only when two ScanHound releases shared one.
That is a closed-world guard: poll_results() enumerates JDownloader's ENTIRE
package list, so a package added by hand has no history row, collides with
nothing, and was handed a confident link to an unrelated release.

Name matching is gone from this path. A row gets a link because
`provenance_url` was recorded -- its file-host links matched links ScanHound
recorded submitting -- or it gets none.

The peer's required cases are here: an unproven package whose name matches
history must resolve to NOTHING, with a positive control beside it so the first
assertion cannot pass merely because resolution is broken.
"""
import pytest

from backend.download_links import annotate_source_links

A = "https://source.example/release-A"


@pytest.fixture
def db(tmp_path):
    from backend.database import DatabaseManager
    return DatabaseManager(str(tmp_path / "links.db"))


class TestFindingOne:
    def test_a_name_match_without_provenance_gets_nothing(self, db):
        """THE regression. The package calls itself exactly what a real release
        is called, and that must now buy it nothing at all."""
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        rows = [{"name": "A.Release.2026", "state": "downloading", "provenance_url": None}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_seen_at"] is None

    def test_the_same_name_WITH_provenance_resolves(self, db):
        """Positive control. Without it, the test above would also pass if
        annotation were broken outright."""
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        rows = [{"name": "A.Release.2026", "state": "downloading", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == A

    def test_an_unproven_row_BESIDE_a_proven_one_still_gets_nothing(self, db):
        """The case that actually exercises the guard.

        annotate_source_links returns early when NO row carries provenance, so a
        list of only-unproven rows never reaches the per-row resolution at all --
        a test using one passes for the wrong reason. Found by mutation: a
        name-matching fallback reintroduced into that loop was NOT caught until
        this case existed, because the early return short-circuited it. A live
        JDownloader list is exactly this mixed shape: our packages beside
        whatever else the user added.
        """
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        other = "https://source.example/release-B"
        db.add_to_history(other, "B Release", package_name="B.Release.2026")
        rows = [
            {"name": "B.Release.2026", "provenance_url": other},   # proven
            {"name": "A.Release.2026", "provenance_url": None},    # name matches, unproven
        ]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == other, "the proven row lost its link"
        assert rows[1]["source_url"] is None, "a name match bought an unproven row a link"

    def test_a_foreign_package_is_unaffected_by_history(self, db):
        """A hand-added package shares nothing with ScanHound: no provenance, and
        a name that happens to collide changes nothing."""
        db.add_to_history(A, "A Release", package_name="Movie (2026) [2160p]")
        rows = [{"name": "Movie (2026) [2160p]", "state": "downloading"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None


class TestFirstSeenDate:
    def test_the_date_comes_from_the_PROVEN_release(self, db):
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        db._mutate("UPDATE downloads SET date_added = ? WHERE url = ?",
                   ("2021-06-05 12:00:00", A))
        rows = [{"name": "whatever", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["first_seen_at"] == "2021-06-05 12:00:00"

    def test_a_regrab_does_not_move_it(self, db):
        """date_added is second-resolution, so two writes in one second would
        agree no matter what ON CONFLICT did -- pin it to a known past value
        first, then a reset to CURRENT_TIMESTAMP is unmissable."""
        db.add_to_history(A, "A Release", package_name="A.Release.2026")
        db._mutate("UPDATE downloads SET date_added = ?, last_grabbed_at = ? WHERE url = ?",
                   ("2020-01-01 00:00:00", "2020-01-01 00:00:00", A))

        db.add_to_history(A, "A Release", package_name="A.Release.2026")

        row = db._query_dicts(
            "SELECT date_added, last_grabbed_at FROM downloads WHERE url = ?", (A,))[0]
        assert row["date_added"] == "2020-01-01 00:00:00", "the regrab moved the date"
        # Positive control: without this the assertion above would also pass if
        # the second write had silently done nothing at all.
        assert row["last_grabbed_at"] != "2020-01-01 00:00:00", "the regrab never wrote"

    def test_a_proven_release_with_no_history_row_still_links(self, db):
        """Provenance authorises the LINK; the date is separate information that
        may simply be absent. Withholding the link too would hide a package we
        can actually prove is ours."""
        rows = [{"name": "x", "provenance_url": "https://source.example/never-in-history"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == "https://source.example/never-in-history"
        assert rows[0]["first_seen_at"] is None


class TestAnnotationShape:
    def test_both_keys_are_always_set(self, db):
        """Consistent shape across the REST poll and the WebSocket push. A
        consumer that has to distinguish missing from None will guess wrong."""
        rows = [{"name": "unproven", "state": "downloading"}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_seen_at"] is None

    def test_a_lookup_failure_does_not_break_the_download_list(self):
        """Decoration must never take down the live progress view, nor the
        poller loop that broadcasts it."""
        class Exploding:
            def get_release_identity(self, urls):
                raise RuntimeError("db is gone")

        rows = [{"name": "x", "state": "downloading", "provenance_url": A}]

        annotate_source_links(Exploding(), rows)

        assert rows[0]["source_url"] is None
        assert rows[0]["first_seen_at"] is None
        assert rows[0]["state"] == "downloading"
        # Fail CLOSED: a lookup failure must leave identity unknown, because
        # unknown is what makes the UI withhold the destructive action.
        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_source"] == "unknown"

    def test_no_db_is_tolerated(self):
        rows = [{"name": "x", "provenance_url": A}]
        annotate_source_links(None, rows)
        assert rows[0]["source_url"] is None

    def test_the_retired_name_resolver_is_gone(self, db):
        """Deleted rather than left unused: a name-based resolver sitting in the
        codebase is a loaded gun for the next caller who reaches for it."""
        assert not hasattr(db, "get_download_source_links")


class TestSemanticIdentity:
    """The identity carried on the wire, and why a name parser cannot replace it.

    The Downloads page groups rows to offer "keep the best copy, cancel the
    rest". Deciding whether two rows are the same thing by reading their
    JDownloader package name is not a parser that needs improving -- it is
    unanswerable from that string. Live data: `Law & Order: LA (2010) [1080p]`
    is ONE package name recorded against 13 distinct seasons.
    """

    def test_a_tv_grab_carries_its_season(self, db):
        db.add_to_history(A, "The Repair Shop", season=2, year=2017,
                          package_name="The Repair Shop (2017) S02 [1080p]")
        rows = [{"name": "whatever", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "tv_season"
        assert rows[0]["identity_title"] == "The Repair Shop"
        assert rows[0]["identity_year"] == 2017
        assert rows[0]["identity_season"] == 2
        assert rows[0]["identity_source"] == "provenance"

    def test_a_movie_grab_reports_no_season(self, db):
        """The positive control for `movie`. Without it, a bug that reported
        everything as unknown would pass every fail-closed test here."""
        db.add_to_history(A, "Notting Hill", year=1999,
                          package_name="Notting Hill (1999) [4K]")
        rows = [{"name": "whatever", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "movie"
        assert rows[0]["identity_season"] is None
        assert rows[0]["identity_year"] == 1999

    def test_THE_case_a_name_parser_can_never_get_right(self, db):
        """Two rows whose package names are character-for-character identical,
        recorded against different seasons. This is live data, not a contrived
        input: 17 rows sit behind such a name today.

        Any parser reading the name must give these the same answer. The
        recorded identity gives them different ones, which is the whole point of
        putting it on the wire.
        """
        b = "https://source.example/release-B"
        name = "Law & Order: LA (2010) [1080p]"
        db.add_to_history(A, "Law & Order: LA", season=11, year=2010,
                          package_name=name)
        db.add_to_history(b, "Law & Order: LA", season=21, year=2010,
                          package_name=name)
        rows = [{"name": name, "provenance_url": A},
                {"name": name, "provenance_url": b}]

        annotate_source_links(db, rows)

        assert rows[0]["name"] == rows[1]["name"]           # indistinguishable
        assert rows[0]["identity_season"] == 11             # ...but not really
        assert rows[1]["identity_season"] == 21

    def test_an_unproven_row_gets_no_identity_at_all(self, db):
        """Provenance is what authorises identity, exactly as it authorises the
        link. A package added to JDownloader by hand must stay unknown."""
        db.add_to_history(A, "The Repair Shop", season=2, year=2017,
                          package_name="The Repair Shop (2017) S02 [1080p]")
        rows = [{"name": "The Repair Shop (2017) S02 [1080p]",
                 "provenance_url": None}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_season"] is None
        assert rows[0]["identity_source"] == "unknown"

    def test_a_proven_url_with_no_history_row_keeps_its_link_but_not_identity(self, db):
        """The two answers are independent. Provenance proves the link; the
        history row carries the identity, and it may simply be absent."""
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == A
        assert rows[0]["identity_kind"] == "unknown"

    def test_a_row_with_no_recorded_title_claims_no_kind(self, db):
        """`movie` here would be worse than useless: every title-less row would
        share an identity and group together."""
        db.add_to_history(A, "", year=2001)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_title"] is None

    #: Named here rather than imported from UNPROVEN ON PURPOSE. Asserting
    #: `set(UNPROVEN) <= set(row)` compares production against itself: deleting
    #: a key from UNPROVEN deletes it from BOTH sides and the assertion still
    #: passes while that key silently vanishes from the wire. Verified -- with
    #: `identity_year` removed from UNPROVEN the old form stayed green at 20/20.
    #: This list is the contract; it has to be written down independently.
    WIRE_KEYS = frozenset({
        "source_url", "first_seen_at", "identity_kind", "identity_title",
        "identity_year", "identity_season", "identity_source",
    })

    def test_the_declared_wire_keys_match_what_production_defaults(self):
        """The two lists must agree, checked in BOTH directions so neither a
        forgotten key nor a stale one can hide."""
        from backend.download_links import UNPROVEN
        assert set(UNPROVEN) == self.WIRE_KEYS

    def test_every_row_carries_the_full_shape_even_when_unproven(self, db):
        """The flicker defect this module exists to prevent was a row shape that
        differed between transports. Identity must not reintroduce it."""
        db.add_to_history(A, "The Repair Shop", season=2, year=2017)
        rows = [{"name": "proven", "provenance_url": A},
                {"name": "not proven", "provenance_url": None}]

        annotate_source_links(db, rows)

        for row in rows:
            missing = self.WIRE_KEYS - set(row)
            assert not missing, f"keys never reached the wire: {missing}"

    def test_a_placeholder_title_is_not_an_identity(self, db):
        """`Untitled` is the POST route's default and `RSS Candidate` the RSS
        path's. Both are stand-ins for "none recorded", and several unrelated
        releases carry the same string -- so treating one as a title hands a
        whole set of unrelated packages ONE identical identity, which is worse
        than no title because it looks answered."""
        b = "https://source.example/release-B"
        db.add_to_history(A, "Untitled", year=2020)
        db.add_to_history(b, "RSS Candidate", year=2021)
        rows = [{"name": "x", "provenance_url": A}, {"name": "y", "provenance_url": b}]

        annotate_source_links(db, rows)

        for row in rows:
            assert row["identity_kind"] == "unknown"
            assert row["identity_title"] is None
            assert row["identity_source"] == "unknown"

    def test_a_real_title_that_merely_contains_a_placeholder_word_is_fine(self, db):
        """The guard matches the WHOLE title, not a substring -- `Untitled` must
        not disqualify a real film whose name happens to include the word."""
        db.add_to_history(A, "The Untitled Star Wars Project", year=2020)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "movie"
        assert rows[0]["identity_title"] == "The Untitled Star Wars Project"

    def test_the_RSS_path_url_shape_fails_CLOSED(self, db):
        """The known coverage gap, pinned as a test so it is a documented
        behaviour rather than a surprise. The RSS action path records provenance
        under the canonical release url but writes history under each file-host
        LINK, so the two never meet. That must yield UNKNOWN -- never a
        confident wrong identity -- and must still render the source link."""
        canonical = "https://hdencode.org/some-show-s03-1080p/"
        db.add_to_history("https://rapidgator.net/file/abc", "Some Show",
                          season=3, year=2019)
        rows = [{"name": "Some Show (2019) S03 [1080p]", "provenance_url": canonical}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == canonical   # the link still resolves
        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_season"] is None


class TestIdentityReachesTheConsumer:
    """The unit tests above hand-build their `rows`, so none of them can detect
    that nothing in production PRODUCES those inputs.

    This walks the real chain instead: the writer the poller calls, the reader
    the REST route calls, and the annotator both transports share. If any link
    stopped carrying `provenance_url`, every identity assertion elsewhere in
    this file would still pass while the feature was dead on the wire.
    """

    def test_identity_survives_write_then_read_then_annotate(self, db):
        db.add_to_history(A, "The Repair Shop", season=2, year=2017,
                          package_name="The Repair Shop (2017) S02 [1080p]")
        db.upsert_download_result(
            name="The Repair Shop (2017) S02 [1080p]", package_uuid="uuid-1",
            title="The Repair Shop", state="downloading",
            provenance_url=A, provenance_observed=True)

        # The exact call the /downloads/results route makes.
        rows = annotate_source_links(db, db.get_download_results(limit=200))

        assert len(rows) == 1, "the writer/reader pair produced no row"
        assert rows[0]["provenance_url"] == A, "provenance did not survive the round trip"
        assert rows[0]["identity_kind"] == "tv_season"
        assert rows[0]["identity_season"] == 2
        assert rows[0]["identity_source"] == "provenance"

    def test_a_row_written_without_provenance_stays_unknown(self, db):
        """The negative half of the same chain, so the test above cannot pass
        merely because everything is labelled tv_season."""
        db.add_to_history(A, "The Repair Shop", season=2, year=2017)
        db.upsert_download_result(
            name="hand added by the user", package_uuid="uuid-2",
            state="downloading", provenance_url=None, provenance_observed=True)

        rows = annotate_source_links(db, db.get_download_results(limit=200))

        assert len(rows) == 1
        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_season"] is None


class TestIdentityGuardsThatNearlyWentUntested:
    """Cases an adversarial pass named. Each is a way a test above could pass
    while the property it claims is broken."""

    def test_a_whitespace_only_title_is_not_an_identity(self, db):
        """The placeholder fix stripped for the COMPARISON but tested emptiness
        on the raw value, so "   " is truthy, strips to nothing, matches no
        placeholder, and sailed through as a confident movie identity."""
        db.add_to_history(A, "   ", year=2020)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_title"] is None

    def test_a_padded_title_lands_on_the_wire_stripped(self, db):
        """Otherwise the same show padded differently would carry two different
        identities, which is the collision this module exists to prevent."""
        db.add_to_history(A, "  The Repair Shop  ", season=2, year=2017)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_title"] == "The Repair Shop"

    def test_an_unproven_row_BESIDE_a_proven_one_gets_no_identity(self, db):
        """THE early-return trap, which this file already documents for links
        and which the identity tests had NOT covered.

        annotate_source_links returns before the resolution loop when NO row
        carries provenance, so a list of only-unproven rows never reaches the
        per-row code at all -- a fail-closed test built that way passes for the
        wrong reason, and a name-matching identity fallback added to that loop
        would survive it. This list is the mixed shape a live JDownloader
        session actually has.
        """
        db.add_to_history(A, "The Repair Shop", season=2, year=2017,
                          package_name="The Repair Shop (2017) S02 [1080p]")
        other = "https://source.example/release-B"
        db.add_to_history(other, "Other Show", season=9, year=2001)
        rows = [
            {"name": "Other Show (2001) S09 [1080p]", "provenance_url": other},
            # Name matches a real history row exactly, but is UNPROVEN.
            {"name": "The Repair Shop (2017) S02 [1080p]", "provenance_url": None},
        ]

        annotate_source_links(db, rows)

        assert rows[0]["identity_season"] == 9, "the proven row lost its identity"
        assert rows[1]["identity_kind"] == "unknown", "a name match bought an identity"
        assert rows[1]["identity_season"] is None
        assert rows[1]["identity_title"] is None

    def test_identity_source_stays_unknown_when_only_the_link_resolves(self, db):
        """A proven url with no history row gets its LINK but no identity. If
        identity_source were set on the link path, a consumer would read
        'provenance' beside kind='unknown' and title=None."""
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["source_url"] == A
        assert rows[0]["identity_source"] == "unknown"
        assert rows[0]["identity_kind"] == "unknown"

    def test_THE_live_shape_that_would_have_collapsed_three_seasons(self, db):
        """The real rows, from the live table. `Law & Order: Special Victims
        Unit` has three history rows whose source urls say seasons 1, 2 and 3
        but whose `season` column was never filled in. Under "no season means
        movie" all three became ONE identity -- kind=movie, same title, year
        None, season None -- and a consumer grouping on that would have offered
        to cancel between three different seasons.

        Requiring a year for `movie` closes it: all 16 such live rows also lack
        a year. This asserts they stay UNKNOWN and, critically, that they do not
        share an identity."""
        urls = [f"https://hdencode.org/law-and-order-svu-s0{n}-1080p/" for n in (1, 2, 3)]
        for u in urls:
            db.add_to_history(u, "Law & Order: Special Victims Unit")  # no season, no year
        rows = [{"name": f"row-{i}", "provenance_url": u} for i, u in enumerate(urls)]

        annotate_source_links(db, rows)

        for row in rows:
            assert row["identity_kind"] == "unknown"
            assert row["identity_source"] == "unknown"
            assert row["identity_title"] is None
        # No two rows may present the same non-null identity.
        ids = [(r["identity_title"], r["identity_year"], r["identity_season"])
               for r in rows if r["identity_title"] is not None]
        assert ids == [], f"rows share an identity: {ids}"

    def test_a_movie_WITH_a_year_still_gets_its_identity(self, db):
        """The positive control. Without it, "always return unknown" would pass
        every fail-closed assertion in this class."""
        db.add_to_history(A, "Notting Hill", year=1999)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "movie"
        assert rows[0]["identity_year"] == 1999

    def test_a_tv_row_with_a_season_but_no_year_is_still_tv(self, db):
        """The year requirement applies ONLY to the movie verdict. A recorded
        season is itself the discriminator, so a year-less TV row keeps its
        identity rather than being caught by the new guard."""
        db.add_to_history(A, "Some Show", season=4)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "tv_season"
        assert rows[0]["identity_season"] == 4
        assert rows[0]["identity_year"] is None

    def test_a_placeholder_is_matched_WHOLE_not_by_prefix(self, db):
        """Loosening the guard to startswith would survive a mid-string test."""
        db.add_to_history(A, "Untitled Horror Project", year=2020)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "movie"
        assert rows[0]["identity_title"] == "Untitled Horror Project"
