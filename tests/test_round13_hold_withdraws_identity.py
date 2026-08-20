"""Round 13: a hold must withdraw the WHOLE semantic identity, at the annotator.

This exists because my first attempt at the M13-1 fix masked `media_kind` alone,
and that was not merely incomplete -- for one row shape it was actively harmful.

`annotate_source_links()` decides identity like this:

    if kind == "movie" and season is not None:   -> refuse (contradiction)
    if season is not None:                       -> tv_season
    if kind != "movie":                          -> refuse
    ...                                          -> movie

Two consequences of masking only the kind:

  * **movie + season** the contradiction guard tests `kind == "movie"`. Nulling
    the kind stops it firing, so the row FALLS THROUGH to the tv_season branch.
    A hold turned fail-CLOSED into fail-OPEN and granted the very permission it
    exists to withdraw.

  * **any + season**   the tv_season branch reads `season` alone and never
    consults media_kind at all, so masking the kind withdrew only the movie half.

A classification conflict is two listings disagreeing about movie-vs-TV, which
invalidates the TV reading exactly as much as the movie one.

These assert on `annotate_source_links()` -- the producer of the wire fields the
frontend's `canKeepBest` is computed from -- not on the identity dict.
"""
import pytest

from backend.database import DatabaseManager
from backend.download_links import annotate_source_links

URL = "https://hdencode.example/the-release-2026-2160p/"


@pytest.fixture
def db(tmp_path):
    dm = DatabaseManager(str(tmp_path / "r13id.db"))
    yield dm
    dm.close()


def _seed(db, *, kind, season, year=2026):
    db.add_to_history(URL, "The Release", None, season, "2160p", "20 GB",
                      hdr="HDR", dovi=False, year=year, media_kind=kind)


def _identity(db):
    rows = [{"id": 1, "provenance_url": URL, "provenance_observed": True}]
    annotate_source_links(db, rows)
    return rows[0].get("identity_kind")


class TestAHeldReleaseYieldsNoActionableIdentity:

    def test_movie_with_a_season_does_not_become_tv_season(self, db):
        """THE INVERSION. Unheld this row is already refused as contradictory.
        Masking only the kind let it through as a FULL tv_season identity, so the
        hold made a previously-safe row destructively actionable."""
        _seed(db, kind="movie", season=5)
        assert _identity(db) == "unknown", "precondition: contradiction is refused"
        db.hold_media_kind([URL], reason="classification_conflict")
        assert _identity(db) == "unknown"

    def test_a_recorded_season_alone_is_withdrawn_too(self, db):
        """The TV half. This row is a legitimate tv_season identity until a
        conflict says the listings disagree about what it is."""
        _seed(db, kind=None, season=3)
        assert _identity(db) == "tv_season", "precondition: TV identity is live"
        db.hold_media_kind([URL], reason="classification_conflict")
        assert _identity(db) == "unknown", (
            "the tv_season branch reads season alone -- masking media_kind does "
            "not reach it, so the destructive permission survived the hold")

    def test_a_recorded_movie_is_withdrawn(self, db):
        _seed(db, kind="movie", season=None)
        assert _identity(db) == "movie", "precondition: movie identity is live"
        db.hold_media_kind([URL], reason="classification_conflict")
        assert _identity(db) == "unknown"


class TestUnheldReleasesAreUntouched:
    """POSITIVE CONTROLS. Withdrawing identity from everything would satisfy every
    assertion above while deleting the feature."""

    def test_a_movie_identity_still_works(self, db):
        _seed(db, kind="movie", season=None)
        assert _identity(db) == "movie"

    def test_a_tv_identity_still_works(self, db):
        _seed(db, kind=None, season=3)
        assert _identity(db) == "tv_season"

    def test_the_contradiction_guard_still_refuses(self, db):
        """Unrelated to holds, and it must survive the change: the guard depends
        on media_kind being visible for unheld rows."""
        _seed(db, kind="movie", season=5)
        assert _identity(db) == "unknown"

    def test_releasing_the_hold_restores_the_identity(self, db):
        _seed(db, kind=None, season=3)
        db.hold_media_kind([URL], reason="x")
        assert _identity(db) == "unknown"
        db.release_media_kind_hold([URL])
        assert _identity(db) == "tv_season"
