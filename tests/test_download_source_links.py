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

    def test_a_SEASONLESS_grab_is_unknown_even_with_a_real_year(self, db):
        """M1. This test used to assert `movie`, which was the blind spot: it
        proved the CONVENTION, not the discriminator. The author knew Notting
        Hill is a film; production never did. `("Notting Hill", 1999, None)` is
        structurally identical to a 1999 TV show whose season was not recorded,
        and `add_to_history` takes no media-type argument, so nothing in the
        data separates them. A year identifies an edition, not a media kind."""
        db.add_to_history(A, "Notting Hill", year=1999,
                          package_name="Notting Hill (1999) [4K]")
        rows = [{"name": "whatever", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "unknown"
        assert rows[0]["identity_source"] == "unknown"
        assert rows[0]["identity_title"] is None

    def test_no_row_is_EVER_labelled_a_movie(self, db):
        """The rule, asserted directly rather than case by case, so restoring a
        `movie` verdict cannot slip back in without this failing."""
        db.add_to_history(A, "Notting Hill", year=1999)
        b = "https://source.example/release-B"
        db.add_to_history(b, "Some Show", season=3, year=2019)
        rows = [{"name": "a", "provenance_url": A}, {"name": "b", "provenance_url": b}]

        annotate_source_links(db, rows)

        assert [r["identity_kind"] for r in rows] == ["unknown", "tv_season"]

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
        db.add_to_history(A, "The Untitled Star Wars Project", season=1, year=2020)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "tv_season"
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

    def test_year_ZERO_is_the_no_year_SENTINEL_not_a_year(self, db):
        """`scanner_service` writes `year=d.get('year', 0) or 0`, so 0 means "we
        did not parse one" -- it is the only sub-1900 value in the live column,
        across 6 rows. Testing `year is None` let every one through, which
        re-opened the collision the year requirement had just been added to
        close."""
        b = "https://source.example/release-B"
        db.add_to_history(A, "Some Show", year=0)   # no season, sentinel year
        db.add_to_history(b, "Some Show", year=0)
        rows = [{"name": "a", "provenance_url": A}, {"name": "b", "provenance_url": b}]

        annotate_source_links(db, rows)

        for row in rows:
            assert row["identity_kind"] == "unknown", "year=0 was treated as a year"
            assert row["identity_title"] is None

    def test_a_sentinel_year_never_reaches_the_wire_as_a_value(self, db):
        """A TV row keeps its identity on the season alone, but must not carry
        year=0 outward. A consumer grouping on (title, year) would treat 0 as a
        real year, which is worse than a missing one -- it looks answered."""
        db.add_to_history(A, "Frankie vs the Internet", season=2, year=0)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "tv_season"
        assert rows[0]["identity_season"] == 2
        assert rows[0]["identity_year"] is None, "the 0 sentinel leaked onto the wire"

    def test_a_TV_row_still_gets_its_identity(self, db):
        """THE positive control for this whole class. Without it, "always return
        unknown" would satisfy every fail-closed assertion here and the feature
        could be dead while the suite stayed green."""
        db.add_to_history(A, "The Repair Shop", season=2, year=2017)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "tv_season"
        assert rows[0]["identity_season"] == 2
        assert rows[0]["identity_year"] == 2017

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
        db.add_to_history(A, "Untitled Horror Project", season=2, year=2020)
        rows = [{"name": "x", "provenance_url": A}]

        annotate_source_links(db, rows)

        assert rows[0]["identity_kind"] == "tv_season"
        assert rows[0]["identity_title"] == "Untitled Horror Project"


class TestFreshnessAgreesAcrossTransports:
    """M2. The poller's in-memory row carries provenance_url=None whenever it
    could not OBSERVE a package's links, while download_results deliberately
    keeps the previous proof -- upsert_download_result writes the new value only
    when provenance_observed is true, because "could not look" is not "no longer
    ours".

    Left alone, the WebSocket row resolves to unknown while a REST poll of the
    same package resolves to a full identity. A blinking source link was an
    accepted cosmetic gap; a blinking IDENTITY is not, because identity is meant
    to authorise cancelling other downloads.

    POLICY: the last proof stays authoritative until retracted, and BOTH
    transports apply it.
    """

    UUID = "pkg-uuid-1"
    NAME = "The Repair Shop (2017) S02 [1080p]"

    def _seed(self, db):
        db.add_to_history(A, "The Repair Shop", season=2, year=2017,
                          package_name=self.NAME)
        db.upsert_download_result(name=self.NAME, package_uuid=self.UUID,
                                  title="The Repair Shop", state="downloading",
                                  provenance_url=A, provenance_observed=True)

    def _rest_row(self, db):
        # Exactly what the REST route hands the annotator.
        return [r for r in db.get_download_results(limit=200)
                if r.get("package_uuid") == self.UUID]

    def _ws_row_unobserved(self, db):
        """Exactly what the poller emits when it could not observe the links.

        It carries the REAL persisted id: poll_results() attaches one from its
        uuid->id cache, else via get_download_result_id(), writing the row first
        if need be -- explicitly so it never emits an id-less row. An earlier
        version of this fixture used id=None, which misrepresented production
        and would have hidden the id-keyed recovery entirely."""
        rid = db.get_download_result_id(self.UUID, self.NAME)
        assert rid is not None, "fixture is not exercising a persisted row"
        return [{"id": rid, "package_uuid": self.UUID, "name": self.NAME,
                 "state": "downloading", "provenance_url": None,
                 "provenance_observed": False}]

    def test_the_two_transports_report_the_SAME_identity(self, db):
        """The finding, stated as the property that matters. Before the fix the
        WS row was unknown and the REST row was tv_season for the same package
        in the same instant."""
        self._seed(db)
        rest = self._rest_row(db)
        ws = self._ws_row_unobserved(db)

        annotate_source_links(db, rest)
        annotate_source_links(db, ws)

        fields = ("identity_kind", "identity_title", "identity_year", "identity_season")
        assert [rest[0][f] for f in fields] == [ws[0][f] for f in fields], (
            f"transports disagree: REST={[rest[0][f] for f in fields]} "
            f"WS={[ws[0][f] for f in fields]}")
        # ...and specifically, the last proof is what both report.
        assert ws[0]["identity_kind"] == "tv_season"
        assert ws[0]["identity_season"] == 2

    def test_an_unobserved_poll_does_not_silently_drop_the_source_link_either(self, db):
        """Same recovery, checked on the pre-existing field, so the fix is not
        quietly identity-only."""
        self._seed(db)
        ws = self._ws_row_unobserved(db)

        annotate_source_links(db, ws)

        assert ws[0]["source_url"] == A

    def test_a_package_that_was_NEVER_proven_stays_unknown(self, db):
        """The negative control. Recovery must resurrect a real prior proof, not
        invent one -- otherwise every unobserved row would acquire an identity."""
        db.add_to_history(A, "The Repair Shop", season=2, year=2017)
        db.upsert_download_result(name="hand added", package_uuid="pkg-uuid-2",
                                  state="downloading", provenance_url=None,
                                  provenance_observed=True)
        ws = [{"id": None, "package_uuid": "pkg-uuid-2", "name": "hand added",
               "state": "downloading", "provenance_url": None,
               "provenance_observed": False}]

        annotate_source_links(db, ws)

        assert ws[0]["identity_kind"] == "unknown"
        assert ws[0]["source_url"] is None

    def test_recovery_only_applies_to_rows_that_could_not_be_observed(self, db):
        """An OBSERVED poll that found no links is a retraction, not a gap. It
        must NOT be handed the old proof back, or a package that genuinely
        stopped being ours would keep its identity forever."""
        self._seed(db)
        ws = [{"id": None, "package_uuid": self.UUID, "name": self.NAME,
               "state": "downloading", "provenance_url": None,
               "provenance_observed": True}]   # looked, and found nothing

        annotate_source_links(db, ws)

        assert ws[0]["identity_kind"] == "unknown"
        assert ws[0]["source_url"] is None

    def test_a_SAME_NAMED_row_can_never_donate_its_provenance(self, db):
        """THE round-2 finding. Recovery used to match on package name with no
        `package_uuid IS NULL` predicate and no ORDER BY, so a different row
        sharing the name -- including a uuid-backed one -- could supply the
        recovered url, and the "last-write-wins" the comment claimed was really
        whichever row SQLite returned last.

        Identical package names across distinct releases are the entire reason
        identity is being moved off names, so that reintroduced the collision
        through the back door, for the source link as well as the identity.

        Two persisted rows, same name, different provenance. The unobserved row
        must recover its OWN url and can never acquire the other's."""
        b = "https://source.example/release-B"
        name = "Same Name (2019) S01 [1080p]"
        db.add_to_history(A, "Show A", season=1, year=2019, package_name=name)
        db.add_to_history(b, "Show B", season=7, year=2001, package_name=name)
        db.upsert_download_result(name=name, package_uuid="uuid-A", state="downloading",
                                  provenance_url=A, provenance_observed=True)
        db.upsert_download_result(name=name, package_uuid="uuid-B", state="downloading",
                                  provenance_url=b, provenance_observed=True)
        id_a = db.get_download_result_id("uuid-A", name)
        id_b = db.get_download_result_id("uuid-B", name)
        assert id_a != id_b, "fixture did not create two distinct rows"

        ws = [{"id": id_a, "package_uuid": "uuid-A", "name": name,
               "state": "downloading", "provenance_url": None,
               "provenance_observed": False}]
        annotate_source_links(db, ws)

        assert ws[0]["source_url"] == A, "recovered the wrong row's url"
        assert ws[0]["identity_season"] == 1, "acquired the other row's identity"
        assert ws[0]["identity_title"] == "Show A"

    def test_a_row_with_no_id_is_left_unrecovered(self, db):
        """Fail closed. If poll_results could not attach an id -- a DB write or
        recovery failure -- there is no durable key, and guessing by name is
        exactly what this fix removed."""
        self._seed(db)
        ws = [{"id": None, "package_uuid": self.UUID, "name": self.NAME,
               "state": "downloading", "provenance_url": None,
               "provenance_observed": False}]

        annotate_source_links(db, ws)

        assert ws[0]["source_url"] is None
        assert ws[0]["identity_kind"] == "unknown"

    def test_an_unobserved_NAME_ADOPTION_does_not_inherit_the_old_proof(self, db):
        """THE round-3 finding. `download_results.id` is durable, but the
        PACKAGE the row belongs to is not.

        upsert_download_result adopts a legacy NULL-uuid row BY NAME when an
        exact uuid lookup misses. The update installs the new uuid
        (COALESCE) while preserving the stored provenance (CASE WHEN
        observed). So the row keeps its id, changes owner, and keeps the OLD
        owner's proof -- and id-keyed recovery then faithfully hands that proof
        to the new package. Identical names across releases are exactly the
        collision this feature exists to remove, so proof must not transfer
        across a name-based ownership change without current evidence."""
        name = "Adopted Name (2019) S01 [1080p]"
        db.add_to_history(A, "Legacy Show", season=1, year=2019, package_name=name)
        # A legacy row: no uuid, with a proof.
        db.upsert_download_result(name=name, package_uuid=None, state="downloading",
                                  provenance_url=A, provenance_observed=True)
        legacy_id = db.get_download_result_id(None, name)
        assert legacy_id is not None

        # A DIFFERENT package, same display name, arriving on an UNOBSERVED poll.
        db.upsert_download_result(name=name, package_uuid="uuid-NEW",
                                  state="downloading", provenance_url=None,
                                  provenance_observed=False)

        adopted = db._query_dicts(
            "SELECT id, package_uuid, provenance_url FROM download_results WHERE name = ?",
            (name,))
        assert len(adopted) == 1, "expected adoption, not a second row"
        assert adopted[0]["id"] == legacy_id, "fixture did not exercise adoption"
        assert adopted[0]["package_uuid"] == "uuid-NEW", "ownership did not change"
        assert adopted[0]["provenance_url"] is None, (
            "the new package inherited the old package's proof")

        ws = [{"id": legacy_id, "package_uuid": "uuid-NEW", "name": name,
               "state": "downloading", "provenance_url": None,
               "provenance_observed": False}]
        annotate_source_links(db, ws)
        assert ws[0]["identity_kind"] == "unknown"
        assert ws[0]["source_url"] is None

    def test_an_OBSERVED_adoption_still_applies_the_current_value(self, db):
        """The control. Clearing on adoption must apply only to the unobserved
        case -- an observed poll is current evidence and stays authoritative,
        including a NULL retraction."""
        name = "Observed Adopt (2019) S02 [1080p]"
        b = "https://source.example/release-B"
        db.add_to_history(A, "Old Show", season=1, year=2019, package_name=name)
        db.add_to_history(b, "New Show", season=2, year=2019, package_name=name)
        db.upsert_download_result(name=name, package_uuid=None, state="downloading",
                                  provenance_url=A, provenance_observed=True)
        db.upsert_download_result(name=name, package_uuid="uuid-OBS", state="downloading",
                                  provenance_url=b, provenance_observed=True)

        row = db._query_dicts(
            "SELECT provenance_url FROM download_results WHERE name = ?", (name,))[0]
        assert row["provenance_url"] == b, "an observed adoption lost its own proof"

    def test_a_uuidless_row_gets_no_last_proof_recovery(self, db):
        """For a package with no uuid the persisted row is resolved BY NAME, so
        the id itself came from the ambiguous signal. Resurrecting a stored
        proof onto it would launder a name match into an identity."""
        name = "No UUID (2019) S03 [1080p]"
        db.add_to_history(A, "Some Show", season=3, year=2019, package_name=name)
        db.upsert_download_result(name=name, package_uuid=None, state="downloading",
                                  provenance_url=A, provenance_observed=True)
        rid = db.get_download_result_id(None, name)

        ws = [{"id": rid, "package_uuid": None, "name": name,
               "state": "downloading", "provenance_url": None,
               "provenance_observed": False}]
        annotate_source_links(db, ws)

        assert ws[0]["source_url"] is None
        assert ws[0]["identity_kind"] == "unknown"

    def test_a_malformed_row_id_is_ignored_rather_than_coerced(self, db):
        """`True` and `1.0` both coerce to 1 under int(), which would look up a
        real row. Strict validation, and never a raise -- annotation must not
        take down the downloads view."""
        self._seed(db)
        real = db.get_download_result_id(self.UUID, self.NAME)
        assert db.get_persisted_provenance([real]) == {real: A}
        for bogus in (True, 1.0, "1", -1, 0, None, object()):
            assert db.get_persisted_provenance([bogus]) == {}, f"accepted {bogus!r}"

    def test_a_uuidless_unobserved_poll_clears_the_PERSISTED_proof_too(self, db):
        """THE round-4 finding, and the honest version of the round-3 test.

        Round 3 enforced "uuid-less rows get no last-proof recovery" in the
        ANNOTATOR only. REST does not go through that gate: get_download_results
        returns the persisted provenance_url directly, so the row already has a
        truthy url and never reaches the recovery filter at all. The result was
        the round-1 transport split, narrowed to uuid-less rows:

            WS   -> unknown        (caller-side uuid gate)
            REST -> release-A      (read straight from the column)

        The previous test built only the WS row, so it could not see the
        persisted half -- exactly the test weakness this branch keeps finding.
        This one drives the real transition and compares BOTH transports.
        """
        name = "UUIDless Transition (2019) S04 [1080p]"
        db.add_to_history(A, "Some Show", season=4, year=2019, package_name=name)
        # 1. a uuid-less row, previously OBSERVED with a real proof
        db.upsert_download_result(name=name, package_uuid=None, state="downloading",
                                  provenance_url=A, provenance_observed=True)
        rid = db.get_download_result_id(None, name)
        assert db._query_dicts(
            "SELECT provenance_url FROM download_results WHERE id = ?",
            (rid,))[0]["provenance_url"] == A, "fixture never established a proof"

        # 2. a later uuid-less poll that could NOT observe the links
        db.upsert_download_result(name=name, package_uuid=None, state="downloading",
                                  provenance_url=None, provenance_observed=False)

        # 3. the persisted proof must be gone, not merely hidden from one transport
        assert db._query_dicts(
            "SELECT provenance_url FROM download_results WHERE id = ?",
            (rid,))[0]["provenance_url"] is None, (
                "the database still holds a proof it cannot attribute")

        # 4/5/6. both transports, and they must agree
        rest = [r for r in db.get_download_results(limit=200) if r.get("id") == rid]
        ws = [{"id": rid, "package_uuid": None, "name": name, "state": "downloading",
               "provenance_url": None, "provenance_observed": False}]
        annotate_source_links(db, rest)
        annotate_source_links(db, ws)

        fields = ("source_url", "identity_kind", "identity_source",
                  "identity_title", "identity_season")
        assert [rest[0][f] for f in fields] == [ws[0][f] for f in fields], (
            f"transports disagree: REST={[rest[0][f] for f in fields]} "
            f"WS={[ws[0][f] for f in fields]}")
        assert rest[0]["identity_kind"] == "unknown"
        assert rest[0]["source_url"] is None

    def test_a_uuidless_OBSERVED_poll_keeps_its_own_current_proof(self, db):
        """The positive control. Directly observed evidence is still valid for
        the current poll, so clearing must apply only to the unobserved case --
        otherwise uuid-less packages could never hold a proof at all and the
        test above would pass for the wrong reason."""
        name = "UUIDless Observed (2019) S05 [1080p]"
        b = "https://source.example/release-B"
        db.add_to_history(b, "Some Show", season=5, year=2019, package_name=name)
        db.upsert_download_result(name=name, package_uuid=None, state="downloading",
                                  provenance_url=b, provenance_observed=True)
        rid = db.get_download_result_id(None, name)

        assert db._query_dicts(
            "SELECT provenance_url FROM download_results WHERE id = ?",
            (rid,))[0]["provenance_url"] == b

        rest = [r for r in db.get_download_results(limit=200) if r.get("id") == rid]
        annotate_source_links(db, rest)
        assert rest[0]["source_url"] == b
        assert rest[0]["identity_kind"] == "tv_season"
        assert rest[0]["identity_season"] == 5
