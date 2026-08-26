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
import pathlib
import re
from unittest.mock import MagicMock

from backend.rename.dv_labeler import HDR10_LABEL, reconcile_movie

REPO = pathlib.Path(__file__).resolve().parent.parent

VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV8", "profile5": "DV5"}


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


class TestTheDocsThatDescribeTheDestructivePassSayHDR10IsSpared:
    """D1/D2: three operator-facing documents describe what a label apply may
    DELETE. Each one used to state removal as a universal over the managed set,
    and each was therefore false: HDR10 is exempt whenever the Plex HDR state is
    UNKNOWN, which is every title absent from the HDR cache, every run whose
    cache read failed, and every caller that supplies no index.

    The error was in the SAFE direction -- the docs overstated destruction --
    but a comment whose whole job is to be exactly right about an unattended
    destructive write does not get to be approximately right, and config.py
    ended up contradicting the runbook about the same label.

    Each test EXECUTES the case first and then asserts the document says what
    just happened, so the assertion cannot drift away from the code: delete the
    qualification from any of the three files and its test fails.
    """

    #: The sentence every one of them has to carry, in the operator's terms
    #: rather than as set arithmetic.
    PHRASE = "neither added nor removed"

    def test_a_MATCHED_authoritative_title_still_keeps_HDR10_when_the_cache_is_silent(self):
        """The D1 case: additive_only=True, may_remove True, HDR10 survives."""
        idx = {"y:/a.mkv": "profile8"}
        mv = _movie(9, ["Y:/a.mkv"], ["DV FEL", HDR10_LABEL])

        for label, kw in (("no index", {}), ("empty index", {"hdr_index": {}})):
            res = reconcile_movie(mv, idx, VOCAB, MagicMock(), dry_run=True,
                                  additive_only=True, **kw)
            assert res["matched"] is True, label
            assert res["removed"] == ["DV FEL"], (label, res["removed"])
            assert HDR10_LABEL not in res["removed"], label

        # Control: with a real 'not HDR' reading it IS removed, so the test
        # above is pinning the exemption and not a blanket never-remove.
        res = reconcile_movie(mv, idx, VOCAB, MagicMock(), dry_run=True,
                              additive_only=True, hdr_index={"9": False})
        assert res["removed"] == ["DV FEL", HDR10_LABEL], res["removed"]

    def test_an_UNMATCHED_title_under_destructive_mode_still_keeps_HDR10(self):
        """The D2 case: additive_only=False strips the rest, not HDR10."""
        idx = {"z:/other.mkv": "fel"}
        mv = _movie(104, ["Y:/nomatch.mkv"], ["DV", HDR10_LABEL])

        for label, kw in (("no index", {}), ("empty index", {"hdr_index": {}})):
            res = reconcile_movie(mv, idx, VOCAB, MagicMock(), dry_run=True,
                                  additive_only=False, **kw)
            assert res["matched"] is False, label
            assert res["removed"] == ["DV"], (label, res["removed"])

        res = reconcile_movie(mv, idx, VOCAB, MagicMock(), dry_run=True,
                              additive_only=False, hdr_index={"104": False})
        assert res["removed"] == ["DV", HDR10_LABEL], res["removed"]

    @staticmethod
    def _flat(rel):
        """Whitespace-collapsed file text.

        The sentence is prose and every one of these files hard-wraps, so a
        literal substring search would pass or fail on where the line break
        happens to land rather than on what the file says."""
        return re.sub(r"\s+", " ", (REPO / rel).read_text(encoding="utf-8"))

    def _hdr_paragraph(self, rel, anchor):
        text = self._flat(rel)
        assert anchor in text, f"{rel}: anchor text moved: {anchor!r}"
        return text

    def test_config_py_says_it_beside_the_dv_auto_sync_setting(self):
        """The scheduled unattended pass. Its comment said every managed label
        the current verdict does not call for is stripped -- a universal the
        code refutes for HDR10."""
        text = self._hdr_paragraph("backend/config.py", '"dv_auto_sync_enabled"')
        assert self.PHRASE in text, (
            "backend/config.py no longer states that HDR10 is neither added nor "
            "removed when the HDR cache is unknown, so its description of what "
            "the hourly pass deletes is a universal the code refutes")
        assert "HDR cache is missing or unreadable" in text, (
            "the condition is stated only as set arithmetic; an operator reading "
            "this comment cannot tell WHEN the exemption applies")

    def test_the_host_detector_README_says_it_in_the_destructive_bullet(self):
        """DV_BADGE_DESIGN sends operators here for the rollout gate before the
        first real label sync -- and a first run is exactly the degraded state
        the exemption fires in. The file used to not mention HDR10 at all."""
        text = self._hdr_paragraph("scripts/host-detector/README.md",
                                   '{"additive_only": false}')
        assert "HDR10" in text, (
            "scripts/host-detector/README.md describes destructive "
            "reconciliation without mentioning HDR10 at all, so it overstates "
            "what the first real label sync will delete")
        assert self.PHRASE in text, (
            "the README mentions HDR10 but no longer says it is neither added "
            "nor removed when the HDR cache is missing or unreadable")

    def test_the_runbook_still_says_it_and_config_py_agrees(self):
        """The two used to disagree about the same label. Assert the shared
        sentence in both rather than trusting them to stay in step."""
        runbook = ("docs/feature-pack-review/"
                   "4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md")
        rb = self._flat(runbook)
        cfg = self._flat("backend/config.py")
        for rel, text in ((runbook, rb), ("backend/config.py", cfg)):
            assert self.PHRASE in text, f"{rel} dropped the shared sentence"
        assert runbook.rsplit("/", 1)[-1] in cfg, (
            "config.py no longer points at the runbook, so the next edit to "
            "either has nothing telling it the other exists")
