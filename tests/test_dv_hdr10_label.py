"""HDR10 requires BOTH halves, and UNKNOWN is not False.

dv_scan has no HDR axis: 'none' means "dovi_tool found no Dolby Vision", which
is equally true of an HDR10 remux and a plain SDR 4K file. So the HDR10 label
needs an authoritative 'none' AND Plex's own wide-gamut flag, and is withheld
when either is missing.

The subtle half is REMOVAL. A rating_key absent from the HDR index means
UNKNOWN -- no cached Plex row, or no index at all -- and unknown must not be
read as "not HDR", or a cache gap would strip a correct HDR10 label on the next
unattended sync. That is the same silent-strip shape as the vocab gap and the
stale-row cases before it, so it gets its own guard and its own tests.
"""
from unittest.mock import MagicMock

from backend.rename.dv_labeler import HDR10_LABEL, reconcile_movie

VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}


def _movie(rk, files, labels):
    mv = MagicMock()
    mv.ratingKey = rk
    objs = []
    for t in labels:
        lo = MagicMock(); lo.tag = t; objs.append(lo)
    mv.labels = objs
    medias = []
    for f in files:
        part = MagicMock(); part.file = f
        m = MagicMock(); m.parts = [part]; medias.append(m)
    mv.media = medias
    return mv


class TestHdr10Add:
    def test_no_dv_plus_plex_hdr_earns_the_label(self):
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": True})

        assert res["added"] == [HDR10_LABEL]

    def test_no_dv_but_SDR_earns_nothing(self):
        """The distinction dv_scan cannot make on its own."""
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": False})

        assert res["added"] == []

    def test_a_DV_title_never_gets_hdr10(self):
        """HDR10 means 'HDR and no DV'. A FEL title is not HDR10-only."""
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": True})

        assert HDR10_LABEL not in res["added"]
        assert set(res["added"]) == {"DV FEL", "DV7", "DV"}

    def test_detection_failure_never_gets_hdr10(self):
        """'unknown' is not proof of absence, so it cannot imply HDR10."""
        idx = {"y:/a.mkv": "unknown"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": True})

        assert res["added"] == []


class TestHdr10RemovalSafety:
    def test_unknown_hdr_never_STRIPS_an_existing_label(self):
        """The axis the guard is on: absent from the index means unknown.

        A no-DV title whose Plex row is missing from the cache must keep its
        HDR10 label. Reading absent as False would strip it on the next pass.
        """
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [HDR10_LABEL])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={})            # 7 is absent -> unknown

        assert res["removed"] == []
        pm.remove_label.assert_not_called()

    def test_no_hdr_index_at_all_leaves_hdr10_alone(self):
        """A caller with no Plex cache (or a failed read) must be inert."""
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [HDR10_LABEL])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, hdr_index=None)

        assert res["removed"] == []

    def test_known_NOT_hdr_DOES_strip_it(self):
        """Control: with real evidence the label must still be cleaned up.

        Without this, a fix that simply never removed HDR10 would pass every
        test above while leaving wrong labels on SDR titles forever.
        """
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [HDR10_LABEL])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": False})

        assert res["removed"] == [HDR10_LABEL]
        pm.remove_label.assert_called_once_with(7, HDR10_LABEL)

    def test_a_title_that_gains_DV_loses_hdr10(self):
        """Control: a rescan finding DV must clear the HDR10-only claim."""
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [HDR10_LABEL])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": False})

        assert res["removed"] == [HDR10_LABEL]
        assert set(res["added"]) == {"DV FEL", "DV7", "DV"}

    def test_unknown_hdr_does_not_block_OTHER_removals(self):
        """The exemption must be narrow: only HDR10 is spared by unknown HDR.

        A stale DV badge on a no-DV title must still be cleaned up even when
        the HDR state is unknown.
        """
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], ["DV FEL", "DV7", "DV", HDR10_LABEL])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, hdr_index={})

        assert set(res["removed"]) == {"DV FEL", "DV7", "DV"}
        assert HDR10_LABEL not in res["removed"]

    def test_idempotent_when_correct(self):
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(7, ["Y:/a.mkv"], [HDR10_LABEL])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False,
                              hdr_index={"7": True})

        assert res["added"] == [] and res["removed"] == []
