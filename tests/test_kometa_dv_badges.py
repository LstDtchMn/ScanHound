"""`docs/kometa/dv_badges.yml` is a DESIGN, and must say so.

It is not what Kometa renders. Kometa loads `/config/dv-layer.yml` -- two image
overlays anchored top-RIGHT -- while this file describes seven text overlays
anchored top-LEFT. Both are legitimate; they are different things.

The divergence was not harmless, because nothing said it existed. A developer
read this file as a description of current behaviour, placed the version-count
badges top-RIGHT to clear the DV badge, and shipped them drawing at exactly the
DV badge's coordinates -- the real badge being top-right too.
`tests/test_version_labeler.py` takes its `DV_TOP, DV_HEIGHT = 15, 96` constants
from the DEPLOYED file for precisely that reason.

A first attempt at this test asserted the file MIRRORED the deployed one. That
was wrong twice over: it destroyed the intended design, and it broke
`test_metadata_scan_runbook.py::test_kometa_badges_cover_the_closed_managed_label_set`,
which exists to keep this file covering the managed label set. CI caught it;
running only the two files I had touched did not.

So this pins the two properties that actually matter: the file stays internally
coherent as a design, and it keeps warning that it is not deployed.
"""

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

DOC = pathlib.Path("docs/kometa/dv_badges.yml")

#: What ScanHound manages and this design is expected to badge. Kept in step with
#: test_metadata_scan_runbook.py, which asserts the same coverage from the other
#: direction.
MANAGED = ("DV FEL", "DV MEL", "DV8", "DV5")

#: Group/filter tags, deliberately never badged: a group tag earns its place only
#: when it spans more than one badge. Profile 7 does (FEL + MEL); Profiles 8 and
#: 5 are one badge each, so a `DV8` group tag beside a `DV8` badge is an alias.
FILTER_ONLY = {"DV7", "DV"}


def _overlays():
    return yaml.safe_load(DOC.read_text(encoding="utf-8"))["overlays"]


def _labels():
    return {b["plex_search"]["all"]["label"] for b in _overlays().values()}


class TestItWarnsThatItIsNotDeployed:
    """The property whose absence caused a shipped defect."""

    def test_the_header_says_it_is_not_what_kometa_runs(self):
        head = DOC.read_text(encoding="utf-8")[:2000]
        assert "NOT** WHAT KOMETA IS RUNNING" in head or \
               "NOT WHAT KOMETA IS RUNNING" in head, (
            "the file no longer warns that it is a design rather than a mirror; "
            "that silence is what let a developer read it as current behaviour")

    def test_it_names_the_file_kometa_actually_loads(self):
        head = DOC.read_text(encoding="utf-8")[:2000]
        assert "/config/dv-layer.yml" in head, (
            "a reader cannot check the divergence without the real path")

    def test_it_records_the_deployed_anchor(self):
        """top-RIGHT is the fact that was missing. Naming it here means the two
        descriptions of one badge can be compared without a container."""
        head = DOC.read_text(encoding="utf-8")[:2000]
        assert "top-RIGHT" in head, head[:200]

    def test_it_records_the_unbadged_labels_as_an_open_gap(self):
        """DV8/DV5/HDR10 are applied to Plex and render nothing today. Written
        down so it stays a decision rather than becoming folklore."""
        head = DOC.read_text(encoding="utf-8")[:3000]
        assert "OPEN GAP" in head
        for label in ("DV8", "DV5", "HDR10"):
            assert label in head, label


class TestTheDesignStaysCoherent:
    def test_every_managed_label_has_a_block(self):
        missing = set(MANAGED) - _labels()
        assert not missing, (
            "labels ScanHound applies with no block in this design: %s" % missing)

    def test_no_filter_only_tag_is_badged(self):
        assert not (_labels() & FILTER_ONLY), _labels() & FILTER_ONLY

    def test_every_block_is_internally_consistent(self):
        """`name:` and the gating label must agree, or the block badges one
        thing and is named another."""
        for name, block in _overlays().items():
            label = block["plex_search"]["all"]["label"]
            assert name == label or label in name, (
                "%r gates on %r" % (name, label))

    def test_it_is_a_single_anchor_design(self):
        """Every block in one column. Mixed anchors would make the collision
        arithmetic in test_version_labeler.py meaningless for this file."""
        anchors = {(b["overlay"].get("horizontal_align"),
                    b["overlay"].get("vertical_align"))
                   for b in _overlays().values()}
        assert len(anchors) == 1, "mixed anchors: %s" % anchors


class TestItStillDescribesRealLabels:
    def test_no_block_gates_on_a_label_nothing_applies(self):
        """Dead config rots silently. Retiring blocks are allowed -- they exist
        so previously-applied labels keep rendering during a migration -- but
        every one must still appear in the labeller."""
        src = pathlib.Path("backend/rename/dv_labeler.py").read_text(
            encoding="utf-8")
        for label in _labels():
            assert f'"{label}"' in src or f"'{label}'" in src, (
                "%s is badged here but dv_labeler.py never applies it" % label)
