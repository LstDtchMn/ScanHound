"""A gap in dv_label_vocab must never strip a correct label.

Found by adversarial review on 2026-08-15. `_vocab_from_config` kept a PARTIAL
vocab -- it filtered out entries whose value was not in MANAGED and restored the
default only when NOTHING survived. `dv_label_vocab` is stored as a free-text
string with no validation at the settings boundary, so one typo ('DV-FEL' for
'DV FEL') silently dropped that layer's mapping while the rest stayed.

`desired_label` then returned None for a layer that was still AUTHORITATIVE, so
`reconcile_movie` read it as "this title should carry no managed label" and
REMOVED the correct badge -- in every mode, including the unattended
additive-only hourly sync, with nothing added back.

Two independent defences are pinned here, because either alone leaves a hole:

  1. the vocab is merged OVER the defaults, so the four known layers cannot go
     unmapped; and
  2. a positive layer with no mapping never authorises removal anyway -- which
     is what protects the NEW layer values the DV7/DV8/HDR10-only tag set will
     add, since those can reach this code before their vocab entry exists.

Tests use a layer that is genuinely unmapped, not merely absent from the
config: with defence 1 in place, an absent key alone can no longer produce the
failure, so exercising defence 2 requires reaching reconcile_movie directly
with a vocab that lacks the layer.
"""
import json
from unittest.mock import MagicMock

from backend.rename.dv_labeler import (
    MANAGED, _vocab_from_config, desired_label, reconcile_movie)

FULL = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}


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


class TestVocabMerge:
    def test_partial_vocab_keeps_the_other_layers_mapped(self):
        """The reported failure: a vocab naming only fel must not unmap p8."""
        v = _vocab_from_config({"dv_label_vocab": json.dumps({"fel": "DV FEL"})})
        assert v["profile8"] == "DV P8"
        assert v["profile5"] == "DV P5"
        assert v["mel"] == "DV MEL"

    def test_a_typo_in_one_value_does_not_unmap_that_layer(self):
        """'DV-FEL' is not in MANAGED, so it is dropped -- and must fall back."""
        v = _vocab_from_config({"dv_label_vocab": json.dumps(
            {"fel": "DV-FEL", "mel": "DV MEL", "profile8": "DV P8",
             "profile5": "DV P5"})})
        assert v["fel"] == "DV FEL"          # the default, not None
        assert desired_label("fel", v) == "DV FEL"

    def test_a_valid_rename_is_still_honoured(self):
        """Positive control: without this the fix could be 'ignore the config'."""
        v = _vocab_from_config({"dv_label_vocab": json.dumps(
            {"fel": "DV MEL"})})            # odd, but a MANAGED value
        assert v["fel"] == "DV MEL"

    def test_empty_and_broken_config_fall_back(self):
        assert _vocab_from_config({}) == FULL
        assert _vocab_from_config({"dv_label_vocab": "not json"}) == FULL
        assert _vocab_from_config({"dv_label_vocab": ""}) == FULL


class TestUnmappedLayerNeverRemoves:
    def test_unmapped_positive_layer_does_not_strip_the_existing_label(self):
        """Defence 2, on the axis the bug is on.

        A title whose layer is authoritative and positive, but whose layer has
        no vocab entry, must keep its badge. Before the fix desired=None made
        this indistinguishable from 'none' and the label was removed.
        """
        idx = {"y:/a.mkv": "profile8"}
        vocab = {"fel": "DV FEL"}            # profile8 deliberately unmapped
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV P8"])

        res = reconcile_movie(mv, idx, vocab, pm, dry_run=False, additive_only=True)

        assert res["removed"] == [], "an unmapped layer must not strip a label"
        pm.remove_label.assert_not_called()

    def test_unmapped_layer_does_not_strip_under_full_reconcile_either(self):
        """The destructive mode must not convert a config gap into removal."""
        idx = {"y:/a.mkv": "profile8"}
        vocab = {"fel": "DV FEL"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV P8"])

        res = reconcile_movie(mv, idx, vocab, pm, dry_run=False, additive_only=False)

        assert res["removed"] == []
        pm.remove_label.assert_not_called()

    def test_authoritative_none_STILL_removes(self):
        """Control: the fix must not disable legitimate cleanup.

        'none' means the tool ran and found no Dolby Vision. A stale badge on
        such a title SHOULD go, and a fix that blocked this would silently stop
        all label cleanup while every other test still passed.
        """
        idx = {"y:/a.mkv": "none"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

        res = reconcile_movie(mv, idx, FULL, pm, dry_run=False, additive_only=True)

        assert res["removed"] == ["DV FEL"]
        pm.remove_label.assert_called_once_with(1, "DV FEL")

    def test_a_mapped_layer_still_replaces_a_stale_label(self):
        """Control: normal swap behaviour is untouched."""
        idx = {"y:/a.mkv": "fel"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV MEL"])

        res = reconcile_movie(mv, idx, FULL, pm, dry_run=False, additive_only=True)

        assert res["added"] == ["DV FEL"] and res["removed"] == ["DV MEL"]

    def test_a_newly_RANKED_layer_without_a_vocab_entry_is_safe(self, monkeypatch):
        """The forward-looking case, on the axis the bug is actually on.

        An UNRANKED value ('hdr10' today) is already inert: pick_layer folds
        anything outside _LAYER_RANK into 'unknown', which never removes. That
        path predates this fix, so asserting it would pin nothing.

        The real hazard is the next step of the DV7/DV8/HDR10-only work: the
        moment a new value enters _LAYER_RANK, pick_layer returns it as a
        POSITIVE finding, and if its vocab entry is not in place yet the
        pre-fix code would have stripped every managed label from those titles.
        """
        from backend.rename import dv_labeler as L
        monkeypatch.setattr(L, "_LAYER_RANK", ["fel", "mel", "profile8", "profile5", "hdr10"])

        idx = {"y:/a.mkv": "hdr10"}
        pm = MagicMock()
        mv = _movie(1, ["Y:/a.mkv"], ["DV FEL"])

        # sanity: the layer really is a positive finding now, not 'unknown'
        assert L.pick_layer(["y:/a.mkv"], idx) == "hdr10"

        res = L.reconcile_movie(mv, idx, FULL, pm, dry_run=False, additive_only=False)

        assert res["removed"] == [], "a ranked-but-unmapped layer must not strip"
        pm.remove_label.assert_not_called()
