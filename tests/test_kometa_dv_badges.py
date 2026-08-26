"""`docs/kometa/DV_BADGE_DESIGN.md` is a proposal, and the repo must keep saying so.

WHAT THESE CAN AND CANNOT PROVE (KOM-2). Kometa's real config lives on the host
at `/config/dv-layer.yml`, outside this repository. No test here can prove it
exists, is loaded, or still has the geometry the design document records. That
geometry is **owner-observed evidence with a date**, and it will go stale.

So these test only repo-owned invariants:

  * the artifact is structurally a design, not a droppable config;
  * no live doc tells anyone to install it as production;
  * the design covers the labels it claims to;
  * every label it gates on is one the labeller actually applies.

An earlier version of this file asserted the presence of warning PROSE. That is
a tripwire, not evidence, and it was doing the work the file's own NAME should
do. The `.yml` is gone; the name now carries the signal.
"""

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

REPO = pathlib.Path(__file__).resolve().parent.parent
DESIGN = REPO / "docs" / "kometa" / "DV_BADGE_DESIGN.md"

#: The layer-badge SUBSET this design covers. Deliberately not called "managed":
#: dv_labeler.MANAGED is nine labels including DV7, DV, HDR10 and the retiring
#: pair. Conflating the two is what the previous version of this file got wrong.
LAYER_BADGES = ("DV FEL", "DV MEL", "DV8", "DV5")

#: Group tags for filtering, never badged: every block draws at one corner, so
#: badging these beside a layer badge would stack labels on one poster.
FILTER_ONLY = {"DV7", "DV"}


def _design_yaml():
    """The candidate YAML, extracted from its fenced block."""
    text = DESIGN.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert blocks, "no ```yaml block in the design document"
    doc = yaml.safe_load(blocks[0])
    assert "overlays" in doc, "the design block has no overlays"
    return doc["overlays"]


def _labels():
    return {b["plex_search"]["all"]["label"] for b in _design_yaml().values()}


class TestTheArtifactIsStructurallyADesign:
    """The property a warning comment could not carry: a .yml under kometa/
    looks droppable no matter what it says inside."""

    def test_the_deployable_looking_yml_is_gone(self):
        assert not (REPO / "docs" / "kometa" / "dv_badges.yml").exists(), (
            "docs/kometa/dv_badges.yml is back. A YAML file in this folder reads "
            "as something to drop into Kometa; that is what misled a developer "
            "into shipping the version badges over the DV badge.")

    def test_the_design_document_exists(self):
        assert DESIGN.exists(), f"{DESIGN} is missing"

    def test_it_declares_itself_undeployed(self):
        head = DESIGN.read_text(encoding="utf-8")[:1500]
        assert "Not deployed" in head, "the status line no longer says it is undeployed"

    def test_it_names_what_kometa_actually_loads(self):
        """A reader must be able to find the real config without a container."""
        assert "/config/dv-layer.yml" in DESIGN.read_text(encoding="utf-8")

    def test_the_observed_geometry_is_dated(self):
        """Owner-observed evidence, not repo truth. Undated, it silently rots."""
        head = DESIGN.read_text(encoding="utf-8")[:1500]
        assert re.search(r"20\d\d-\d\d-\d\d", head), (
            "the deployed-reality observation has no date, so a reader cannot "
            "tell how stale it is")


class TestNoLiveDocTellsAnyoneToInstallIt:
    """The install instruction is what would actually have caused harm."""

    #: Point-in-time records of work done. They describe what was true when
    #: written and are not instructions to anyone today.
    ARCHIVES = ("superpowers/plans", "superpowers/specs", "reviews/peer-rounds",
                ".superpowers")

    def _live_docs(self):
        for p in (REPO / "docs").rglob("*"):
            if p.suffix.lower() not in (".md", ".yml", ".yaml"):
                continue
            rel = p.relative_to(REPO).as_posix()
            if any(a in rel for a in self.ARCHIVES):
                continue
            yield p, rel

    def test_no_live_doc_references_the_removed_yml_as_a_file_to_use(self):
        offenders = []
        for p, rel in self._live_docs():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "dv_badges.yml" not in line:
                    continue
                # A struck-through or explicitly-superseded mention is the
                # record of the correction, not a live instruction.
                if "~~" in line or "SUPERSEDED" in line or "replaces" in line:
                    continue
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
        assert not offenders, (
            "live docs still point at the removed file:\n  " + "\n  ".join(offenders))

    def test_the_priority_plan_no_longer_instructs_the_copy(self):
        """That instruction, followed, would have moved every badge to the left
        edge and reverted the image design."""
        plan = REPO / "docs" / "PRIORITY-PLAN-2026-08-16.md"
        if not plan.exists():
            pytest.skip("priority plan not present")
        text = plan.read_text(encoding="utf-8")
        assert "SUPERSEDED" in text and "do NOT do this" in text


class TestTheDesignCoversWhatItClaims:
    def test_every_layer_badge_has_a_block(self):
        missing = set(LAYER_BADGES) - _labels()
        assert not missing, f"the design omits {missing}"

    def test_no_filter_only_tag_is_badged(self):
        assert not (_labels() & FILTER_ONLY)

    def test_it_is_a_single_anchor_design(self):
        """One corner for every block. Mixed anchors would make the collision
        arithmetic in test_version_labeler.py meaningless."""
        anchors = {(b["overlay"].get("horizontal_align"),
                    b["overlay"].get("vertical_align"))
                   for b in _design_yaml().values()}
        assert len(anchors) == 1, f"mixed anchors: {anchors}"

    def test_each_block_gates_on_the_label_it_is_named_for(self):
        for name, block in _design_yaml().items():
            label = block["plex_search"]["all"]["label"]
            assert name == label or label in name, f"{name!r} gates on {label!r}"


class TestItOnlyGatesOnLabelsScanHoundApplies:
    def test_no_block_gates_on_a_label_nothing_emits(self):
        """Dead config rots silently. Retiring blocks are allowed -- they exist
        so previously-applied labels keep rendering during a migration -- but
        each must still appear in the labeller."""
        src = (REPO / "backend" / "rename" / "dv_labeler.py").read_text(encoding="utf-8")
        for label in _labels():
            assert f'"{label}"' in src or f"'{label}'" in src, (
                f"{label} is badged in the design but dv_labeler.py never applies it")

    def test_the_layer_subset_is_smaller_than_the_managed_set(self):
        """The distinction the previous version of this file got wrong: these
        four are the layer badges, NOT everything ScanHound manages."""
        src = (REPO / "backend" / "rename" / "dv_labeler.py").read_text(encoding="utf-8")
        for wider in ("DV7", "HDR10"):
            assert f'"{wider}"' in src or f"'{wider}'" in src, (
                f"{wider} should be in the labeller's managed set")
        assert wider not in LAYER_BADGES
