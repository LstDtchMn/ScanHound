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

AND A NOTE ON THE ROUND AFTER THAT, which is why the rules below stopped being
string matches. Written as an absence-of-a-LITERAL check, this file reported
GREEN while the caption printed under the destructive "Sync Plex labels" button
still read "Applies DV FEL/MEL/P8/P5 ... Only these four labels are managed".
It contains no literal 'DV P8', because the UI factors the shared prefix out of
the slash-run -- the same substring trap as 'DV P8' vs 'DV8', in the direction
nobody had looked. And nothing here asserted anything about the COUNT, so the
same caption with the CURRENT names would have passed too, though the
understatement of what an apply can DELETE would be untouched.

So there are three rules now, and none of them knows a label string it did not
import:

  literal_offenders     a retired name, in full, as current vocabulary
  abbrev_offenders      a retired name abbreviated, on a line making a LABEL
                        claim (the layer AXIS -- profile 8, profile 5, P8.1 --
                        was never renamed and must stay legal)
  undercount_offenders  any claim that N labels are managed, N != len(MANAGED)

A guard reporting green on the highest-proximity live violation in the repo is
worse than no guard: it converts an unswept surface into an apparently swept
one. TestTheInvariantDetectorCatchesWhatItExistsToCatch therefore runs each
rule against the defect it exists to catch AND against the correct prose it
must not condemn.
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

#: The ABBREVIATED forms of the retired labels, DERIVED by stripping the shared
#: 'DV ' prefix: {'DV P8','DV P5'} -> {'P8','P5'}. A caption that factors the
#: prefix out of a slash-run -- "DV FEL/MEL/P8/P5" -- contains neither literal
#: 'DV P8' nor 'DV P5', so the literal sweep reported GREEN on the renames page
#: while the destructive button's OWN caption still carried retired vocabulary.
#: That is the substring trap in the opposite direction from the 'DV P8' vs
#: 'DV8' one the literal sweep was careful about.
RETIRED_ABBREV = {lab.split(" ", 1)[1] for lab in RETIRED if " " in lab}

#: A retired abbreviation only offends when the surface presents it as LABEL
#: vocabulary. "FEL/MEL/P8/P5 layer evidence" is correct and must stay legal:
#: profile 8 and profile 5 are Dolby Vision PROFILES, which were never renamed
#: -- the layer axis is fel|mel|profile8|profile5 in dv_labeler and always was.
#: What retired is the PLEX LABEL NAME. So this looks for the CLAIM, not the
#: token.
_LABEL_CLAIM = re.compile(r"labell?ed|labels?\b", re.I)

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}

#: "<count> [up to three words] label(s)": 'four labels', 'nine managed Plex DV
#: labels'. The COUNT is compared to len(MANAGED); the names used are
#: irrelevant, which is the whole point -- a caption saying "only these four
#: labels are managed" is just as wrong with the CURRENT names as with the
#: retired ones, and the undercount is what SR2-2 is actually about. An
#: undercount is the dangerous direction (it understates what the apply may
#: DELETE), but any drift is a defect, so this asserts equality.
_COUNT_OF_LABELS = re.compile(
    r"\b(?P<n>%s|\d{1,3})\s+(?:[-\w`']+\s+){0,3}?labels?\b"
    % "|".join(_NUMBER_WORDS), re.I)

#: Only a line claiming CLOSURE is making a managed-set count claim. Without
#: this, "these four names all key as ..." elsewhere in the tree would offend.
_CLOSURE_MARKERS = ("managed", "closed set", "closed to")

#: A count is only a SET-SIZE claim when it is not doing some other job. Found
#: by running these rules over the whole repo with every exemption ignored:
#: "authoritative Plex identities had at least one managed DV label" and
#: "one block per MANAGED label" are a lower bound and a distributive, not
#: claims that MANAGED has one member. Both live in archived docs today and so
#: never reached the live sweep -- but a rule that condemns correct prose the
#: moment someone writes it on a live page becomes pressure to delete the rule.
_QUANTIFIER_BEFORE = re.compile(
    r"\b(at least|at most|more than|fewer than|no more than|greater than|"
    r"up to|as many as)\s+$", re.I)
_DISTRIBUTIVE_WITHIN = re.compile(r"\bper\b", re.I)
#: "HDR10 is THE ONE managed label not derived from a DV verdict alone" is a
#: singular REFERENCE, not a size. Narrow on purpose: it fires only on the
#: token 'one' preceded by 'the'. The cost is that a surface claiming MANAGED
#: holds exactly one label, phrased "the one label that is managed", would slip
#: through -- an unnatural phrasing for a claim nobody has ever made, traded
#: against a correct sentence the runbook is one word away from writing today
#: (it says "the one TAG not derived...", which only misses by vocabulary).
_SINGULAR_REFERENCE = re.compile(r"\bthe\s+$", re.I)

#: ...and only about THIS closed set. version_labeler has a deliberately
#: separate one, and its docs legitimately count it.
_DV_SCOPE = re.compile(r"\bDV\b|Dolby Vision|dv[_-]label|dv-sync|dv_labeler", re.I)


def _quote(rel, i, line, at):
    """Quote around the MATCH, not the head of the line."""
    return "%s:%d: ...%s..." % (rel, i, line[max(0, at - 40):at + 70].strip())


def literal_offenders(rel, text):
    """A retired label named in FULL as current vocabulary.

    Deliberately separate from abbrev_offenders, which is why that rule carries
    a (?<!DV ) lookbehind: one defect reported by two rules produces a failure
    message that reads like two defects.
    """
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        hits = [lab for lab in sorted(RETIRED) if lab in line]
        if not hits:
            continue
        if any(w in line.lower() for w in RETIREMENT_MARKERS):
            continue
        out.append(_quote(rel, i, line, line.index(hits[0])))
    return out


def abbrev_offenders(rel, text):
    """Retired labels named in ABBREVIATED form as current LABEL vocabulary.

    Two conditions, both required, and the second is what keeps the rule from
    condemning correct prose:

      1. a token from RETIRED_ABBREV not already preceded by "DV " (that form
         is the literal sweep's job, and it is legal on a line that marks the
         retirement);
      2. the line makes a LABEL claim. "FEL/MEL/P8/P5 layer evidence" describes
         the DETECTION AXIS -- fel|mel|profile8|profile5 -- and profiles 8 and
         5 were never renamed, so it must stay legal -- as is a profile
         VERSION such as P8.1. Only a line calling them
         LABELS is asserting Plex vocabulary.
    """
    if not RETIRED_ABBREV:
        return []
    # (?!\.\d) keeps the PROFILE VERSION out of it: "MEL as uninteresting
    # (= P8.1)" in docs/feature-prompts/dv-fel-mel-detection.md is Dolby
    # Vision profile 8.1, correct and permanent vocabulary, and it happened
    # to sit on a line that also says "label vocabulary".
    pat = re.compile(r"(?<!DV )\b(%s)\b(?!\.\d)"
                     % "|".join(sorted(RETIRED_ABBREV)))
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(w in line.lower() for w in RETIREMENT_MARKERS):
            continue
        if not _LABEL_CLAIM.search(line):
            continue
        m = pat.search(line)
        if m:
            out.append(_quote(rel, i, line, m.start()))
    return out


def undercount_offenders(rel, text):
    """A surface claiming a managed-label COUNT that is not len(MANAGED).

    The names are irrelevant here on purpose. "Only these four labels are
    managed" is exactly as false written DV FEL/DV MEL/DV8/DV5 as it is written
    with the retired names, and the UNDERCOUNT -- not the rename -- is what
    understates the destruction an apply can do. Nothing in this function knows
    any label string; it imports the number from dv_labeler.MANAGED.

    Scoping, so that version_labeler's deliberately separate closed set can go
    on counting itself: the line must claim CLOSURE, and DV vocabulary must
    appear within two lines of it (a window, because prose wraps).
    """
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if not any(w in line.lower() for w in _CLOSURE_MARKERS):
            continue
        window = "\n".join(lines[max(0, i - 3):i + 2])
        if not _DV_SCOPE.search(window):
            continue
        for m in _COUNT_OF_LABELS.finditer(line):
            if _QUANTIFIER_BEFORE.search(line[:m.start()]):
                continue
            if _DISTRIBUTIVE_WITHIN.search(m.group(0)):
                continue
            tok = m.group("n").lower()
            if tok == "one" and _SINGULAR_REFERENCE.search(line[:m.start()]):
                continue
            n = _NUMBER_WORDS.get(tok, None)
            if n is None:
                n = int(tok)
            if n != len(MANAGED):
                out.append(_quote(rel, i, line, m.start())
                           + " [claims %d managed labels, MANAGED has %d]"
                           % (n, len(MANAGED)))
    return out


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

    def test_it_states_that_HDR10_is_CONDITIONAL_not_simply_applied(self):
        """HDR10 is the one managed label that is not derived from a DV verdict
        alone. dv_labeler adds it only when an hdr_index was supplied AND that
        title's hdr_state is True, and when hdr_state is None it is exempted
        from removal entirely -- the sync logs "HDR index unavailable; HDR10
        labels left untouched" and does neither.

        The runbook said "So ScanHound applies DV8, DV5 and HDR10 to Plex
        today", flattening it into the same unconditional apply as the two
        layer badges. An operator who reads that, runs the dry run and sees no
        HDR10 has been handed the wrong explanation: nothing is broken, the
        condition simply is not met.
        """
        section = self._kometa_section()
        flat = " ".join(section.split())
        hdr = re.escape(dv_labeler.HDR10_LABEL)
        bare = re.search(r"applies[^.]*%s(?![+\w])[^.]*today" % hdr, flat)
        assert not bare, (
            "the runbook lists HDR10 among the labels applied 'today' with no "
            f"condition attached: {bare.group(0)!r}")
        for phrase in ("neither added nor removed", "wide-gamut"):
            assert phrase in flat, (
                f"the runbook does not state the HDR10 condition ({phrase!r} "
                "missing), so a dry run with no HDR10 reads as a failure")

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

    #: Prose surfaces outside docs/. The sweep globbed only docs/** plus
    #: REPO/*.md, which missed BOTH READMEs -- and DV_BADGE_DESIGN.md sends the
    #: operator to scripts/host-detector/README.md "for the full ordering and
    #: the rollout gate that must be cleared before the first real label sync",
    #: so that file is as live as the runbook. Globs are anchored (scripts/**,
    #: frontend/*) rather than REPO.rglob so node_modules is never walked.
    EXTRA_DOC_GLOBS = ("*.md", "*.yml", "*.yaml",
                       "scripts/**/*.md", "scripts/**/*.yml", "scripts/**/*.yaml",
                       "frontend/*.md")

    def _live_docs(self):
        # Repo-root markdown as well as docs/: README is a live surface too, and
        # scoping a sweep to one directory is how a consumer gets missed. The
        # root glob used to be "*.md" alone, which made the .yml/.yaml branch of
        # the suffix filter DEAD for everything outside docs/ -- the sweep
        # advertised YAML coverage it did not deliver.
        candidates = sorted((REPO / "docs").rglob("*"))
        for g in self.EXTRA_DOC_GLOBS:
            candidates += sorted(REPO.glob(g))
        seen = set()
        for p in candidates:
            if p.suffix.lower() not in (".md", ".yml", ".yaml"):
                continue
            if p in seen:
                continue
            seen.add(p)
            rel = p.relative_to(REPO).as_posix()
            if any(a in rel for a in self.ARCHIVE_DIRS) or rel in self.DATED_RECORDS:
                continue
            yield p, rel

    def _offenders(self, files):
        return [o for p, rel in files
                for o in literal_offenders(rel, p.read_text(encoding="utf-8"))]

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

    def _code_files(self):
        """`dv_labeler.py` is where RETIRED_LABELS is defined and explained, so
        it is the one place the names belong unmarked."""
        defn = (REPO / "backend" / "rename" / "dv_labeler.py").resolve()
        files = []
        # "*.ps1" was missing, so the description string that installer writes
        # into Windows Task Scheduler -- a live operator-facing surface, on the
        # HOST -- was never scanned.
        for root, patterns in (("backend", ("*.py",)),
                               ("scripts", ("*.py", "*.ps1")),
                               ("frontend/src", ("*.svelte", "*.ts", "*.js"))):
            base = REPO / root
            if not base.exists():
                continue
            for pat in patterns:
                for p in sorted(base.rglob(pat)):
                    if p.resolve() == defn:
                        continue
                    files.append((p, p.relative_to(REPO).as_posix()))
        return files

    def test_no_shipped_code_or_ui_names_a_retired_label_as_current(self):
        files = self._code_files()
        assert files, "the code sweep found nothing to scan"
        assert not self._offenders(files), (
            "shipped code or UI still uses the pre-rename label names:\n  "
            + "\n  ".join(self._offenders(files)))

    def _all_live_surfaces(self):
        """Docs AND code, together. The two literal sweeps are kept apart
        because they exempt different things; the invariant below applies to
        every live surface regardless of which file type it lives in, and the
        defect that prompted it was in a .svelte caption while its twin was in
        a .ps1 Task Scheduler description."""
        seen, out = set(), []
        for p, rel in self._live_docs():
            seen.add(rel)
            out.append((rel, p.read_text(encoding="utf-8")))
        for p, rel in self._code_files():
            if rel in seen:
                continue
            out.append((rel, p.read_text(encoding="utf-8")))
        return out

    def test_no_live_surface_names_a_retired_label_in_abbreviated_form(self):
        """D1/D2: the renames caption read "Applies DV FEL/MEL/P8/P5 ..." --
        the retired vocabulary with the shared prefix factored out. It contains
        no literal 'DV P8', so the literal sweep called it clean while it sat
        directly under the destructive Sync Plex labels button."""
        surfaces = self._all_live_surfaces()
        assert surfaces, "the invariant sweep found nothing to scan"
        offenders = [o for rel, text in surfaces for o in abbrev_offenders(rel, text)]
        assert not offenders, (
            "a live surface names a retired label in abbreviated form as "
            "current LABEL vocabulary:\n  " + "\n  ".join(offenders))

    def test_no_live_surface_misstates_how_many_labels_are_managed(self):
        """The other half, and the more dangerous one. "Only these four labels
        are managed - your own labels are never touched" understates what
        reconcile_movie may STRIP by five labels, and it stays false if the
        four are renamed to the current names. The count is compared to
        len(dv_labeler.MANAGED), imported."""
        surfaces = self._all_live_surfaces()
        assert surfaces, "the invariant sweep found nothing to scan"
        offenders = [o for rel, text in surfaces for o in undercount_offenders(rel, text)]
        assert not offenders, (
            "a live surface misstates the size of the managed label set:\n  "
            + "\n  ".join(offenders))

    def test_the_widened_sweep_actually_reaches_the_surfaces_it_claims(self):
        """D4: the sweep advertised coverage it did not deliver. Naming the
        files here means a future narrowing of the globs fails loudly instead
        of quietly reopening the hole."""
        rels = {rel for rel, _ in self._all_live_surfaces()}
        must = {"scripts/host-detector/README.md",   # DV_BADGE_DESIGN sends operators here
                "frontend/README.md",
                "scripts/install-dv-scan-task.ps1",  # writes a live host-side description
                "frontend/src/routes/renames/+page.svelte",
                "README.md"}
        missing = {r for r in must if r not in rels and (REPO / r).exists()}
        assert not missing, f"the sweep no longer reaches: {sorted(missing)}"

    def test_the_yaml_branch_of_the_doc_sweep_is_not_dead(self):
        """The suffix filter accepted .yml/.yaml, but the only non-docs glob
        was '*.md', so outside docs/ that branch could never fire."""
        yml = [rel for rel, _ in self._all_live_surfaces()
               if rel.endswith((".yml", ".yaml"))]
        assert yml, "the doc sweep still advertises YAML coverage it never applies"
        assert any("/" not in rel for rel in yml), (
            "no repo-ROOT yaml is reached, so the root glob is still '*.md' only")

    def test_the_labeller_module_docstring_does_not_understate_the_closed_set(self):
        """The docstring said the module reconciles ONLY within the four layer
        badges under their pre-rename names -- contradicted nine lines below by
        MANAGED itself."""
        doc = dv_labeler.__doc__ or ""
        named = sorted(lab for lab in RETIRED if lab in doc)
        assert not named, f"dv_labeler's module docstring still names {named}"
        assert "MANAGED" in doc, (
            "the docstring must point at MANAGED rather than restate a subset")


class TestTheInvariantDetectorCatchesWhatItExistsToCatch:
    """A guard written beside the code it checks passes BY CONSTRUCTION. The
    literal sweep did exactly that: it went green against a tree whose most
    dangerous surface still carried the defect. So each rule is run here
    against the defect it exists to catch, AND against the correct prose it
    must not condemn.

    The pre-fix caption is stored verbatim rather than paraphrased. A
    paraphrase would be the test author's belief again, which is the failure
    mode this entire file exists to prevent.
    """

    #: frontend/src/routes/renames/+page.svelte:592 as of f5f371a -- the
    #: caption printed directly under the "Sync Plex labels" button, which
    #: POSTs /rename/dv-sync-labels.
    PRE_FIX_CAPTION = '              Applies <code>DV FEL/MEL/P8/P5</code> to the exact copy Plex serves. Only these four labels are managed — your own labels are never touched.'

    def test_the_literal_sweep_really_was_blind_to_it(self):
        """The premise. If this fails, the finding was misdiagnosed and the
        rules below are solving the wrong problem."""
        assert not [lab for lab in RETIRED if lab in self.PRE_FIX_CAPTION], (
            "the pre-fix caption DOES contain a literal retired label, so the "
            "literal sweep would have caught it")

    def test_it_flags_the_prefix_factored_caption(self):
        assert abbrev_offenders("frontend/src/routes/renames/+page.svelte",
                                self.PRE_FIX_CAPTION), (
            "the abbreviated-form rule does not catch the caption that "
            "prompted it")

    def test_it_flags_the_undercount_in_that_same_caption(self):
        assert undercount_offenders("x.svelte", self.PRE_FIX_CAPTION), (
            "the count rule does not catch 'only these four labels are managed'")

    def test_it_flags_an_undercount_that_uses_the_CURRENT_names(self):
        """The rename is not the defect. This line names DV8/DV5 correctly and
        is still false: MANAGED is nine, and reconcile_movie can strip DV7, DV,
        HDR10 and the two retiring names too."""
        synthetic = ("Applies <code>DV FEL/DV MEL/DV8/DV5</code> to the exact copy "
                     "Plex serves. Only these four labels are managed.")
        assert not abbrev_offenders("x.svelte", synthetic), (
            "premise broken: the synthetic line uses only CURRENT names, so the "
            "abbreviation rule must be silent and the count rule must be "
            "catching it unaided")
        assert undercount_offenders("x.svelte", synthetic), (
            "a current-names undercount passes -- the guard detects the RENAME "
            "and not the INVARIANT, which is the finding")

    def test_it_accepts_a_correct_count(self):
        assert not undercount_offenders(
            "x.svelte", "Applies the DV layer badges to the copy Plex serves. "
                        "Nine labels are managed in total.")

    def test_the_count_is_read_from_MANAGED_at_run_time(self, monkeypatch):
        """Not hardcoded: it is len(dv_labeler.MANAGED). Grow MANAGED and the
        same correct sentence must become an offence."""
        import sys
        mod = sys.modules[undercount_offenders.__module__]
        ok = "Nine DV labels are managed in total."
        assert not undercount_offenders("x.md", ok)
        monkeypatch.setattr(mod, "MANAGED", MANAGED | {"DV Synthetic"})
        assert undercount_offenders("x.md", ok), (
            "'Nine labels are managed' stayed legal after MANAGED grew to ten, "
            "so the rule is not reading MANAGED at all")

    def test_it_accepts_the_layer_axis_shorthand(self):
        """Profiles 8 and 5 were never renamed -- only the LABEL names were.
        Prose about the detection axis must stay legal, or the rule becomes
        pressure to rewrite correct text."""
        for legal in (
            "FEL/MEL/P5/P8 layer evidence, file signature, tool evidence, and scan time.",
            "FEL/MEL/P5/P8 spot checks agree with independent dovi_tool evidence;",
            "/** Read-only DV layer joined from dv_scan by path at serialize "
            "time (FEL/MEL/P8/P5). Null = unknown. */",
        ):
            assert not abbrev_offenders("x.md", legal), f"false positive on: {legal}"

    def test_it_accepts_a_profile_version(self):
        assert not abbrev_offenders(
            "x.md", "treat MEL as uninteresting (= P8.1). Affects label vocabulary.")

    def test_it_accepts_a_line_that_marks_the_retirement(self):
        """The runbook and the new caption MUST be able to name the retiring
        labels: the dry run reports removing them, and an operator who was not
        told reads a correct migration as a fault.

        ISOLATED on purpose. The obvious phrasing for this test -- "...the
        retiring DV P8/DV P5" -- is protected TWICE, by the retirement marker
        AND by the (?<!DV ) lookbehind, so removing either guard left it green
        and both mutations survived. Here the abbreviated form carries no 'DV '
        prefix, so ONLY the marker can save it.
        """
        assert not abbrev_offenders(
            "x.md", "The retiring P8/P5 label names are removed by this sync.")

    def test_it_leaves_the_FULL_names_to_the_literal_rule(self):
        """The other half of that isolation, and the reason the lookbehind
        exists. An unmarked 'DV P8' IS an offence -- but it is literal_offenders
        that must report it. If both rules fire, one defect prints as two."""
        line = "Applies the DV P8 and DV P5 labels to Plex."
        assert literal_offenders("x.svelte", line), (
            "premise broken: the literal rule does not catch the full names, "
            "so the abbreviated rule staying silent leaves a hole")
        assert not abbrev_offenders("x.svelte", line), (
            "both rules report the same occurrence; one defect will print as "
            "two")

    def test_it_accepts_a_lower_bound_rather_than_a_set_size(self):
        """Verbatim from the whole-repo audit. "at least one managed DV label"
        counts LABELLED TITLES, not the managed set."""
        assert not undercount_offenders(
            "x.md", "All 463 authoritative Plex identities had at least one "
                    "managed DV label after the sync."), (
            "a lower bound is being read as a claim that MANAGED has one member")

    def test_it_accepts_a_distributive_count(self):
        """Also from the audit: "one block per MANAGED label" is one block
        EACH, not a claim that MANAGED holds one.

        The first draft of this test paraphrased the audit line as "per MANAGED
        DV label" -- one word longer, which pushed it past the pattern's
        three-word gap so nothing matched and the test passed no matter what
        the rule did. It survived a mutation that deleted the exclusion
        outright. The premise assertion below is what makes that impossible to
        repeat.
        """
        line = ("DV deliverable: a label-gated dv_badges.yml (one block per "
                "MANAGED label) dropped into Kometa.")
        assert _COUNT_OF_LABELS.search(line) and _DV_SCOPE.search(line), (
            "premise broken: this line no longer reaches the count rule at all, "
            "so it cannot show the distributive exclusion doing anything")
        assert not undercount_offenders("x.md", line)

    def test_it_accepts_a_singular_reference(self):
        """Third shape from the audit. The runbook is one word from writing
        this sentence today -- it currently says "the one TAG not derived from
        a DV verdict alone"."""
        assert not undercount_offenders(
            "x.md", "HDR10 is the one managed DV label not derived from a "
                    "Dolby Vision verdict alone.")

    def test_it_leaves_a_different_closed_set_alone(self):
        """version_labeler's set is deliberately separate and legitimately
        counts itself. A rule that condemned it would be pressure to make a
        correct doc wrong."""
        assert not undercount_offenders(
            "docs/specs/versions.md",
            "Version labels need their own closed set: the four labels "
            "{2,3,4,5+} Versions are managed by a different pass."), (
            "the count rule escaped DV scope and is policing the version set")


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
