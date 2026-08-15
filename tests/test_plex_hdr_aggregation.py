"""Title-level HDR must be deterministic, and must fail in the safe direction.

Peer review 2026-08-15 (#76). ``get_plex_hdr_by_rating_key`` built its map with
a plain dict comprehension over plex_cache rows. But plex_cache holds one row
per media PART/version, so a title with several versions has several rows --
verified on the live database: 1,032 rating_keys have more than one row, and
**225 have rows that DISAGREE about hdr** (a 4K HDR version beside a 4K SDR
one). Whichever duplicate SQLite returned last silently decided the title.

That is destructive, not merely untidy: a False authorises removing an HDR10
label, so 225 titles had a coin-flip chance of losing a correct badge on any
given sync.

These tests drive the REAL helper against a REAL database. The existing HDR10
tests start from a hand-built hdr_index and therefore never touch this
boundary, which is exactly why the defect survived them -- the reviewer's
point, and the reason these live here rather than as more labeler unit tests.
"""
import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db():
    dm = DatabaseManager()
    dm._mutate("DELETE FROM plex_cache WHERE rating_key LIKE 'hdrtest-%'",
               (), label="test_clear")
    yield dm
    dm._mutate("DELETE FROM plex_cache WHERE rating_key LIKE 'hdrtest-%'",
               (), label="test_clear")
    dm.close()


def _row(db, key, hdr, path):
    """One plex_cache row = one media part, which is the unit that duplicates."""
    db._mutate(
        "INSERT INTO plex_cache (title, rating_key, hdr, dovi, res, file_path) "
        "VALUES (?, ?, ?, 0, '4K', ?)",
        ("hdr test", key, hdr, path), label="test_seed")


class TestTitleLevelHdr:
    def test_hdr_then_sdr_reports_HDR(self, db):
        _row(db, "hdrtest-1", 1, "/a/hdr.mkv")
        _row(db, "hdrtest-1", 0, "/a/sdr.mkv")
        assert db.get_plex_hdr_by_rating_key().get("hdrtest-1") is True

    def test_sdr_then_hdr_reports_THE_SAME(self, db):
        """The axis the bug was on: insertion order must not change the answer.

        With the old comprehension these two tests disagreed, which is the
        defect -- 225 live titles sit in exactly this shape.
        """
        _row(db, "hdrtest-2", 0, "/b/sdr.mkv")
        _row(db, "hdrtest-2", 1, "/b/hdr.mkv")
        assert db.get_plex_hdr_by_rating_key().get("hdrtest-2") is True

    def test_all_sdr_reports_NOT_hdr(self, db):
        """Control: 'any version HDR' must not collapse into 'always True'."""
        _row(db, "hdrtest-3", 0, "/c/one.mkv")
        _row(db, "hdrtest-3", 0, "/c/two.mkv")
        assert db.get_plex_hdr_by_rating_key().get("hdrtest-3") is False

    def test_single_row_titles_still_work(self, db):
        _row(db, "hdrtest-4", 1, "/d/only.mkv")
        _row(db, "hdrtest-5", 0, "/e/only.mkv")
        m = db.get_plex_hdr_by_rating_key()
        assert m.get("hdrtest-4") is True and m.get("hdrtest-5") is False

    def test_absent_title_is_UNKNOWN_not_False(self, db):
        """Absent must stay distinguishable from known-not-HDR: the labeler
        exempts HDR10 from removal only when the state is unknown."""
        assert "hdrtest-never-inserted" not in db.get_plex_hdr_by_rating_key()


class TestThroughTheLabeler:
    def test_a_mixed_version_title_cannot_LOSE_hdr10(self, db):
        """The reviewer's required end-to-end: DB boundary -> labeler decision.

        A title with one HDR and one SDR version, verdict 'none', already
        carrying HDR10. Under the old lossy map this could resolve to False and
        strip the label; it must not.
        """
        from unittest.mock import MagicMock
        from backend.rename.dv_labeler import HDR10_LABEL, reconcile_movie

        _row(db, "hdrtest-6", 0, "/f/sdr.mkv")
        _row(db, "hdrtest-6", 1, "/f/hdr.mkv")
        hdr_index = db.get_plex_hdr_by_rating_key()

        mv = MagicMock()
        mv.ratingKey = "hdrtest-6"
        lbl = MagicMock(); lbl.tag = HDR10_LABEL
        mv.labels = [lbl]
        part = MagicMock(); part.file = "Y:/f/hdr.mkv"
        media = MagicMock(); media.parts = [part]
        mv.media = [media]

        res = reconcile_movie(mv, {"y:/f/hdr.mkv": "none"},
                              {"fel": "DV FEL"}, MagicMock(),
                              dry_run=False, hdr_index=hdr_index)

        assert HDR10_LABEL not in res["removed"], \
            "a mixed-version title must not lose HDR10"
