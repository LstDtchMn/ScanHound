"""Multiple managed tags per title: the layer badge plus derived group tags.

One verdict is true at several widths at once — a FEL title IS FEL, IS Profile
7, and IS Dolby Vision. Before this, reconcile_movie computed removals as
"every managed label except THE one", which could only express a single tag per
title: a correct second tag was always classified stale and removed on the next
pass, so two writers would have fought forever.

The reconciliation is now set arithmetic. These tests pin the property that
matters -- a title ends up with EXACTLY the set its verdict calls for, no more
and no less -- and the controls that stop a wrong "fix" from passing: user
labels outside the managed set are still untouchable, authoritative 'none'
still clears everything, and a detection failure still removes nothing.
"""
from unittest.mock import MagicMock

import pytest

from backend.rename.dv_labeler import (
    MANAGED, _LAYER_LABELS, desired_label, desired_labels, reconcile_movie)

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


class TestDesiredLabels:
    @pytest.mark.parametrize("layer,expected", [
        ("fel", {"DV FEL", "DV7", "DV"}),
        ("mel", {"DV MEL", "DV7", "DV"}),
        ("profile8", {"DV P8", "DV8", "DV"}),
        ("profile5", {"DV P5", "DV5", "DV"}),
    ])
    def test_each_layer_yields_its_full_tag_set(self, layer, expected):
        assert desired_labels(layer, VOCAB) == expected

    def test_fel_and_mel_share_dv7_but_differ_on_the_layer_badge(self):
        """DV7 is the point of the grouping: one Kometa rule covers both."""
        fel, mel = desired_labels("fel", VOCAB), desired_labels("mel", VOCAB)
        assert "DV7" in fel and "DV7" in mel
        assert fel - mel == {"DV FEL"} and mel - fel == {"DV MEL"}

    @pytest.mark.parametrize("layer", ["none", "unknown", None, ""])
    def test_no_tags_for_non_findings(self, layer):
        assert desired_labels(layer, VOCAB) == set()

    def test_every_produced_label_is_managed(self):
        """Anything we ADD must be something we are allowed to REMOVE, or the
        labeler would create labels it can never clean up."""
        for layer in ("fel", "mel", "profile8", "profile5"):
            assert desired_labels(layer, VOCAB) <= MANAGED

    def test_desired_label_still_returns_only_the_layer_badge(self):
        """The single-label helper must not start returning group tags."""
        assert desired_label("fel", VOCAB) == "DV FEL"
        assert desired_label("fel", VOCAB) in _LAYER_LABELS

    def test_a_renamed_layer_label_still_gets_its_group_tags(self):
        v = dict(VOCAB, fel="DV MEL")     # odd rename, but a valid layer label
        assert desired_labels("fel", v) == {"DV MEL", "DV7", "DV"}


class TestReconcileWithSets:
    def test_a_fel_title_gains_all_three_tags(self):
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], [])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

        assert set(res["added"]) == {"DV FEL", "DV7", "DV"}
        assert res["removed"] == []

    def test_the_second_tag_is_NOT_treated_as_stale(self):
        """The core defect: DV7 beside DV FEL used to be removed as stale."""
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV7", "DV"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

        assert res["added"] == [] and res["removed"] == []   # idempotent
        pm.remove_label.assert_not_called()

    def test_a_layer_change_swaps_only_what_actually_changed(self):
        """FEL -> P8 keeps DV, swaps the badge, and swaps the profile group."""
        idx = {"y:/a.mkv": "profile8"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV7", "DV"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

        assert set(res["added"]) == {"DV P8", "DV8"}
        assert set(res["removed"]) == {"DV FEL", "DV7"}
        assert "DV" not in res["added"] and "DV" not in res["removed"]

    def test_fel_to_mel_keeps_dv7_and_dv(self):
        idx = {"y:/a.mkv": "mel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV7", "DV"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

        assert res["added"] == ["DV MEL"] and res["removed"] == ["DV FEL"]

    def test_user_labels_outside_the_managed_set_are_untouchable(self):
        """The historical 'DV Cut' bug: a 'DV ' prefix wildcard deleted it.

        This matters more now that MANAGED contains the bare tag 'DV'.
        """
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV Cut", "DV FEL", "DV7", "DV"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

        assert res["removed"] == []
        assert "DV Cut" not in res["added"] + res["removed"]

    def test_authoritative_none_clears_the_WHOLE_set(self):
        """Control: a fix that never removes would pass every test above."""
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV7", "DV", "DV Cut"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=True)

        assert set(res["removed"]) == {"DV FEL", "DV7", "DV"}
        assert "DV Cut" not in res["removed"]

    def test_detection_failure_removes_nothing(self):
        """Control: 'unknown' must stay inert now that more tags are managed."""
        idx = {"y:/a.mkv": "unknown"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL", "DV7", "DV"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False, additive_only=False)

        assert res["removed"] == []
        pm.remove_label.assert_not_called()

    def test_partial_existing_set_is_completed_not_churned(self):
        """A title already carrying DV gains only the two it lacks."""
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV"])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=False)

        assert set(res["added"]) == {"DV FEL", "DV7"}
        assert res["removed"] == []

    def test_dry_run_reports_the_set_but_writes_nothing(self):
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], [])

        res = reconcile_movie(mv, idx, VOCAB, pm, dry_run=True)

        assert set(res["added"]) == {"DV FEL", "DV7", "DV"}
        pm.add_label.assert_not_called()
        pm.remove_label.assert_not_called()
