"""`docs/kometa/dv_badges.yml` must describe what Kometa actually renders.

Until 2026-08-24 it did not, and the divergence was not harmless documentation.
It described TEXT overlays anchored top-LEFT; nothing deployed had ever looked
like that. A developer trusted it, placed the version-count badges top-RIGHT to
clear the DV badge, and shipped them drawing at exactly the DV badge's
coordinates -- because the real DV badge is top-RIGHT too.

`tests/test_version_labeler.py` measures version-badge clearance against
`DV_TOP, DV_HEIGHT = 15, 96`, constants taken from the DEPLOYED file. This
module pins the repo copy to the SAME numbers, so the two descriptions of one
badge cannot drift apart again -- which is the only way that class of collision
comes back.

What this cannot check: whether the deployed `/config/dv-layer.yml` still
matches either. Kometa's config is not in this repo and no test here can read
it. The geometry below was verified against the running container on
2026-08-24; if the badge is ever re-cut, these constants and that file must move
together, and only a human can see that they have.
"""

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

# Deliberately duplicated from tests/test_version_labeler.py rather than
# imported. The point is that two independent files agree on one geometry; an
# import would make them one file wearing two names, and a wrong value would
# then satisfy both.
DV_TOP, DV_HEIGHT = 15, 96

#: Labels ScanHound applies that are group/filter tags, NOT badges. A group tag
#: earns its place only when it spans more than one badge: Profile 7 does
#: (FEL + MEL); Profiles 8 and 5 are a single badge each, so a `DV8` group tag
#: beside a `DV8` badge would be a pure alias.
FILTER_ONLY = {"DV7", "DV"}

#: Applied to Plex, intended to be badged, and NOT YET rendered because this
#: design uses pre-rendered PNGs and only dv-fel/dv-mel exist. Listed so the gap
#: is visible in a test run rather than only in a comment.
KNOWN_UNBADGED = {"DV8", "DV5", "HDR10"}


def _doc():
    p = pathlib.Path("docs/kometa/dv_badges.yml")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _overlays():
    return _doc()["overlays"]


class TestTheRepoCopyMatchesTheDeployedGeometry:
    def test_every_badge_is_in_the_DV_column(self):
        """Top-right. The stale copy said top-LEFT, which is what made a
        developer place another badge in this exact box."""
        for name, block in _overlays().items():
            o = block["overlay"]
            assert o["horizontal_align"] == "right", f"{name} is not top-right"
            assert o["vertical_align"] == "top", f"{name} is not top-anchored"

    def test_the_badge_occupies_the_box_the_other_tests_avoid(self):
        """Asserted as the exact anchor the version-badge clearance arithmetic
        depends on. If this moves, that arithmetic is silently wrong."""
        for name, block in _overlays().items():
            o = block["overlay"]
            assert o["vertical_offset"] == DV_TOP, (
                f"{name} starts at y={o['vertical_offset']}, but "
                f"test_version_labeler.py clears y={DV_TOP}..{DV_TOP + DV_HEIGHT}")
            assert o["horizontal_offset"] == DV_TOP

    def test_it_is_an_IMAGE_design_not_text(self):
        """The pre-rendered-pill choice is the reason the geometry is fixed and
        knowable. A `text(...)` overlay renders at font-dependent size, so the
        clearance constants above would stop meaning anything."""
        for name, block in _overlays().items():
            o = block["overlay"]
            assert "file" in o, (
                f"{name} is not an image overlay; the stale copy used "
                f"text(...) overlays and that is what this file must not "
                f"drift back to")
            assert not str(o["name"]).startswith("text("), name

    def test_each_badge_names_an_image_under_the_kometa_badges_dir(self):
        for name, block in _overlays().items():
            f = block["overlay"]["file"]
            assert f.startswith("/config/badges/"), (
                f"{name} points at {f}, which is not where Kometa keeps badges")
            assert f.endswith(".png"), f


class TestTheGapIsDocumentedRatherThanForgotten:
    def test_only_the_two_labels_with_images_are_badged(self):
        """Not an endorsement of the gap -- a record of it. If someone adds a
        block for DV8 without adding dv8.png, Kometa renders nothing and the
        poster looks identical to a movie with no DV at all."""
        badged = {b["plex_search"]["all"]["label"] for b in _overlays().values()}
        assert badged == {"DV FEL", "DV MEL"}, (
            f"badge set changed to {badged}. If images were added, update "
            f"KNOWN_UNBADGED here and in docs/kometa/dv_badges.yml; if not, "
            f"these blocks render nothing.")

    def test_the_filter_only_tags_are_never_badged(self):
        """DV7 and DV are for collections and smart filters. Badging them would
        put a second pill in the same box as DV FEL/DV MEL, since every FEL and
        MEL title carries them too."""
        badged = {b["plex_search"]["all"]["label"] for b in _overlays().values()}
        assert not (badged & FILTER_ONLY), (
            f"a filter-only tag is being badged: {badged & FILTER_ONLY}")

    def test_the_known_gap_and_the_badged_set_do_not_overlap(self):
        """Anti-vacuity for the two lists above: if a label were in both, one of
        them is stale and the first test would still pass."""
        badged = {b["plex_search"]["all"]["label"] for b in _overlays().values()}
        assert not (badged & KNOWN_UNBADGED)
        assert not (FILTER_ONLY & KNOWN_UNBADGED)


class TestTheFileStillDescribesRealLabels:
    def test_every_badged_label_is_one_ScanHound_actually_applies(self):
        """The other direction: a block for a label nothing emits is dead config
        that rots silently."""
        src = pathlib.Path("backend/rename/dv_labeler.py").read_text(
            encoding="utf-8")
        for name, block in _overlays().items():
            label = block["plex_search"]["all"]["label"]
            assert f'"{label}"' in src or f"'{label}'" in src, (
                f"{label} is badged but dv_labeler.py never applies it")
