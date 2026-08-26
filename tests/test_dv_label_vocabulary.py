"""The DV label VOCABULARY, pinned to `dv_labeler.py` instead of to prose.

WHY THIS FILE EXISTS. `docs/kometa/DV_BADGE_DESIGN.md` was corrected to say
Kometa loads `/config/dv-layer.yml` and that the design is an unadopted
proposal. Immediately above that correction, the same LIVE runbook still said

    The managed set is closed to:
    DV FEL - DV MEL - DV P8 - DV P5

which was wrong twice over: the layer badges are `DV8`/`DV5` now, and
`dv_labeler.MANAGED` is NINE labels, not four. A reader of the label dry run
uses that list to judge a **destructive** reconciliation, so understating it
understates what the apply may delete.

THREE SETS, DELIBERATELY SEPARATE:

  ``MANAGED``        everything reconcile_movie may add or REMOVE (nine)
  layer badges       one label per detected layer (four) -- a strict SUBSET
  ``RETIRED_LABELS`` pre-rename names kept in MANAGED so the sync removes them

EVERY EXPECTED NAME BELOW IS IMPORTED, never retyped. A test that restates
string literals pins the test author's belief; the defect being guarded against
is precisely an author's belief drifting from the code. So the assertions read
the constants out of the module and compare them to what the documents and the
shipped defaults say.

A NOTE ON THE REVIEW THAT PROMPTED THIS. It asked for a test that fails if any
live doc "reintroduces 'DV P8' or 'DV P5'". Taken literally that is wrong: the
runbook MUST name them, because they are in MANAGED and the dry run will report
removing them, and an operator who has not been told that reads a correct
migration as a fault. The invariant is not absence -- it is that a retired label
never appears as CURRENT vocabulary.
"""

import json
import pathlib
import re

import pytest

from backend.rename import dv_labeler

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "feature-pack-review" / "4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md"
DESIGN = REPO / "docs" / "kometa" / "DV_BADGE_DESIGN.md"

MANAGED = dv_labeler.MANAGED
RETIRED = dv_labeler.RETIRED_LABELS
#: Private by name, but it IS the definition of the layer-badge subset; there is
#: no public accessor and restating it here is exactly what this file forbids.
LAYER_BADGES = dv_labeler._LAYER_LABELS

SEP = "·"

#: Words that make an occurrence a statement ABOUT the retirement rather than a
#: use of the name as current vocabulary.
RETIREMENT_MARKERS = ("retiring", "retired", "renamed", "pre-rename", "rename:")


def _runbook():
    return RUNBOOK.read_text(encoding="utf-8")


def _vocab_block(name, text=None):
    """The label set inside a `<!-- dv-vocab:NAME -->` fenced block.

    The markers exist so this test reads a delimited list rather than
    heuristically parsing prose. They render as nothing.
    """
    text = _runbook() if text is None else text
    m = re.search(
        r"<!-- dv-vocab:%s -->\s*```text\n(.*?)\n```\s*<!-- /dv-vocab:%s -->" % (name, name),
        text, re.DOTALL)
    assert m, f"the runbook has no <!-- dv-vocab:{name} --> block"
    return {tok.strip() for tok in m.group(1).split(SEP) if tok.strip()}


def _block_spans(text):
    """Character ranges of every dv-vocab block, markers included."""
    return [m.span() for m in re.finditer(
        r"<!-- dv-vocab:[a-z-]+ -->.*?<!-- /dv-vocab:[a-z-]+ -->", text, re.DOTALL)]


class TestTheRunbookVocabularyIsTheLabellersVocabulary:
    """Each block is compared by SET EQUALITY, so the runbook fails both when it
    omits a label the labeller manages and when it invents one it does not."""

    def test_the_managed_block_is_exactly_dv_labeler_MANAGED(self):
        assert _vocab_block("managed") == MANAGED, (
            "the runbook's managed set disagrees with dv_labeler.MANAGED; "
            f"doc-only={_vocab_block('managed') - MANAGED}, "
            f"code-only={MANAGED - _vocab_block('managed')}")

    def test_the_layer_badge_block_is_exactly_the_labellers_layer_subset(self):
        assert _vocab_block("layer-badges") == LAYER_BADGES

    def test_the_retiring_block_is_exactly_RETIRED_LABELS(self):
        if not RETIRED:
            # The migration is over. The block must go with it, or the runbook
            # tells an operator to expect removals that can no longer happen.
            assert "dv-vocab:retiring" not in _runbook()
            return
        assert _vocab_block("retiring") == RETIRED

    def test_the_layer_subset_is_a_strict_subset_of_the_managed_set(self):
        """The conflation the finding is about, asserted on the DOCUMENT's own
        two lists: if the runbook ever lists the same four in both places, this
        fails even though each list is internally plausible."""
        managed, layers = _vocab_block("managed"), _vocab_block("layer-badges")
        assert layers < managed, (
            "the runbook presents the layer-badge subset as the whole managed "
            f"set: managed={sorted(managed)} layers={sorted(layers)}")

    def test_the_runbook_never_names_a_retired_label_as_current_vocabulary(self):
        """Outside the delimited blocks, a retired name may only appear on a
        line that says it is retired."""
        text = _runbook()
        spans = _block_spans(text)
        offenders = []
        for label in sorted(RETIRED):
            for m in re.finditer(re.escape(label), text):
                if any(a <= m.start() < b for a, b in spans):
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                line = text[line_start:line_end if line_end != -1 else None]
                if any(w in line.lower() for w in RETIREMENT_MARKERS):
                    continue
                lineno = text.count("\n", 0, m.start()) + 1
                offenders.append(f"line {lineno}: {line.strip()[:100]}")
        assert not offenders, (
            "the runbook names a retired label as if it were current "
            "vocabulary:\n  " + "\n  ".join(offenders))


class TestTheRunbookSeparatesDeployedRealityFromTheDesign:
    def _kometa_section(self):
        text = _runbook()
        i = text.index("## How Kometa displays the result")
        j = text.find("\n## ", i + 1)
        return text[i:j if j != -1 else None]

    def test_it_names_the_labels_kometa_renders_nothing_for(self):
        """Derived, not listed: whatever ScanHound applies beyond the two PNGs
        that exist on the host must be named here as un-badged."""
        rendered = {"DV FEL", "DV MEL"}          # dv-fel.png, dv-mel.png
        unrendered = (LAYER_BADGES - rendered) | {dv_labeler.HDR10_LABEL}
        section = self._kometa_section()
        # Not a bare substring test: the section also discusses HDR10+, a
        # different thing Kometa derives from Plex metadata, and 'HDR10' inside
        # 'HDR10+' would satisfy this assertion without the label ever being
        # mentioned.
        missing = {lab for lab in unrendered
                   if not re.search(re.escape(lab) + r"(?![+\w])", section)}
        assert not missing, (
            f"the runbook does not tell the operator that {sorted(missing)} get "
            "no Kometa badge, so a missing badge reads as a labelling failure")
        assert "no badge at all" in section

    def test_it_marks_the_design_document_as_not_the_running_config(self):
        section = self._kometa_section()
        assert "DV_BADGE_DESIGN.md" in section
        assert "proposal" in section and "/config/dv-layer.yml" in section


class TestTheShippedDefaultsMatchTheLabeller:
    """The Settings field showed `DV P8`/`DV P5` as the vocabulary in force. It
    was inert -- `_vocab_from_config` drops a value that is not a layer label --
    but an operator reading Settings had no way to know that."""

    def test_the_config_default_parses_to_the_labellers_default_vocab(self):
        from backend.config import _DEFAULT_CONFIG
        assert json.loads(_DEFAULT_CONFIG["dv_label_vocab"]) == dv_labeler._DEFAULT_VOCAB

    def test_the_dv_host_export_default_parses_to_the_same_thing(self):
        from backend.app_service import _DV_EXPORT_DEFAULTS
        assert json.loads(_DV_EXPORT_DEFAULTS["dv_label_vocab"]) == dv_labeler._DEFAULT_VOCAB

    def test_the_default_vocab_maps_only_onto_current_layer_badges(self):
        """Belt and braces: a default that mapped a layer onto a retired name
        would survive both assertions above if someone changed both files."""
        assert set(dv_labeler._DEFAULT_VOCAB.values()) == LAYER_BADGES
        assert not set(dv_labeler._DEFAULT_VOCAB.values()) & RETIRED


class TestNoLiveSurfaceNamesARetiredLabelAsCurrent:
    """The lesson from the previous round of this review: fixing one consumer
    while four others still carried the same defect WAS the defect."""

    #: Point-in-time records. They describe what was true on a stated date and
    #: are not instructions to anyone today; rewriting them to match current
    #: knowledge would destroy the provenance they exist to carry.
    ARCHIVE_DIRS = ("superpowers/plans", "superpowers/specs", "reviews/peer-rounds",
                    ".superpowers")

    DATED_RECORDS = {
        "docs/feature-pack-review/RENAMING_PIPELINE_AND_4K_METADATA_AUDIT_2026-07-22.md":
            "audit dated 2026-07-22; records the vocabulary as it was then",
        "docs/feature-prompts/dv-feature-status-review.md":
            "the mention sits inside a block already marked SUPERSEDED 2026-08-04 "
            "and quotes Plex label COUNTS observed on that date",
        "docs/reviews/2026-08-06-overnight-report.md":
            "dated report quoting label counts observed 2026-07-26",
        # Covered by TestTheDesignDocKeepsTheRetiringLabelsMarked instead: it
        # SHOULD name them, marked, for as long as the migration runs.
        "docs/kometa/DV_BADGE_DESIGN.md":
            "the retiring blocks are the migration, asserted separately",
    }

    def test_every_exempted_path_still_exists(self):
        """A renamed or deleted exemption must be noticed, not left to rot into
        a hole this sweep no longer looks through."""
        missing = [rel for rel in self.DATED_RECORDS if not (REPO / rel).exists()]
        assert not missing, f"stale exemptions in DATED_RECORDS: {missing}"

    def _live_docs(self):
        # Repo-root markdown as well as docs/: README is a live surface too, and
        # scoping a sweep to one directory is how a consumer gets missed.
        candidates = sorted((REPO / "docs").rglob("*")) + sorted(REPO.glob("*.md"))
        for p in candidates:
            if p.suffix.lower() not in (".md", ".yml", ".yaml"):
                continue
            rel = p.relative_to(REPO).as_posix()
            if any(a in rel for a in self.ARCHIVE_DIRS) or rel in self.DATED_RECORDS:
                continue
            yield p, rel

    def _offenders(self, files):
        out = []
        for p, rel in files:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                hits = [lab for lab in sorted(RETIRED) if lab in line]
                if not hits:
                    continue
                if any(w in line.lower() for w in RETIREMENT_MARKERS):
                    continue
                # Quote around the MATCH, not the start of the line: a long line
                # truncated at its head hides the very text being complained
                # about, which is how an unhelpful failure message happens.
                at = line.index(hits[0])
                out.append(f"{rel}:{i}: ...{line[max(0, at - 40):at + 60].strip()}...")
        return out

    def test_no_live_doc_names_a_retired_label_as_current(self):
        # The runbook is excluded HERE only because it has a stricter test of
        # its own -- one that understands the dv-vocab blocks, where a retired
        # name is required. It is not exempt from the invariant.
        files = [(p, rel) for p, rel in self._live_docs()
                 if rel != RUNBOOK.relative_to(REPO).as_posix()]
        assert files, "the live-doc sweep found nothing to scan"
        assert not self._offenders(files), (
            "live docs still use the pre-rename label names:\n  "
            + "\n  ".join(self._offenders(files)))

    def test_no_shipped_code_or_ui_names_a_retired_label_as_current(self):
        """`dv_labeler.py` is where RETIRED_LABELS is defined and explained, so
        it is the one place the names belong unmarked."""
        defn = (REPO / "backend" / "rename" / "dv_labeler.py").resolve()
        files = []
        for root, patterns in (("backend", ("*.py",)),
                               ("scripts", ("*.py",)),
                               ("frontend/src", ("*.svelte", "*.ts", "*.js"))):
            base = REPO / root
            if not base.exists():
                continue
            for pat in patterns:
                for p in sorted(base.rglob(pat)):
                    if p.resolve() == defn:
                        continue
                    files.append((p, p.relative_to(REPO).as_posix()))
        assert files, "the code sweep found nothing to scan"
        assert not self._offenders(files), (
            "shipped code or UI still uses the pre-rename label names:\n  "
            + "\n  ".join(self._offenders(files)))

    def test_the_labeller_module_docstring_does_not_understate_the_closed_set(self):
        """The docstring said the module reconciles ONLY within the four layer
        badges under their pre-rename names -- contradicted nine lines below by
        MANAGED itself."""
        doc = dv_labeler.__doc__ or ""
        named = sorted(lab for lab in RETIRED if lab in doc)
        assert not named, f"dv_labeler's module docstring still names {named}"
        assert "MANAGED" in doc, (
            "the docstring must point at MANAGED rather than restate a subset")


class TestTheDesignDocKeepsTheRetiringLabelsMarked:
    def test_every_retired_label_it_names_is_marked_retiring(self):
        if not RETIRED:
            assert not [lab for lab in ("DV P8", "DV P5")
                        if lab in DESIGN.read_text(encoding="utf-8")], (
                "RETIRED_LABELS is empty, so the design's retiring blocks are "
                "dead config and must go")
            return
        text = DESIGN.read_text(encoding="utf-8")
        for label in sorted(RETIRED):
            if label not in text:
                continue
            assert f"{label} (retiring)" in text, (
                f"{label} appears in the design without a (retiring) marker, so "
                "a reader cannot tell it from a current badge")

    def test_it_invents_no_retired_label_of_its_own(self):
        """A block marked '(retiring)' whose label is not in RETIRED_LABELS is
        dead config: the labeller will never remove it and nothing applies it."""
        text = DESIGN.read_text(encoding="utf-8")
        marked = set(re.findall(r"^\s*(\S+(?: \S+)?) \(retiring\):", text, re.M))
        assert marked <= RETIRED, f"design marks {sorted(marked - RETIRED)} retiring, "\
                                  f"but RETIRED_LABELS is {sorted(RETIRED)}"


def test_the_three_sets_are_related_the_way_the_docs_claim():
    """A single import-only check of the shape everything above depends on."""
    assert LAYER_BADGES < MANAGED
    assert RETIRED <= MANAGED
    assert not LAYER_BADGES & RETIRED
    assert dv_labeler.HDR10_LABEL in MANAGED and dv_labeler.HDR10_LABEL not in LAYER_BADGES
    assert len(MANAGED) > len(LAYER_BADGES)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
