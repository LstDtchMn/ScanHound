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
migration as a fault. What we actually want is not absence -- it is that a
retired label never appears as CURRENT vocabulary. That is a property of
MEANING, and the rules below approximate it with string shapes; see LIMITS for
how far the approximation reaches.

AND A NOTE ON THE ROUND AFTER THAT, which is why there is more than one rule.
Written as an absence-of-a-LITERAL check, this file reported GREEN while the
caption printed under the destructive "Sync Plex labels" button still read
"Applies DV FEL/MEL/P8/P5 ... Only these four labels are managed".
It contains no literal 'DV P8', because the UI factors the shared prefix out of
the slash-run -- the same substring trap as 'DV P8' vs 'DV8', in the direction
nobody had looked. And nothing here asserted anything about the COUNT, so the
same caption with the CURRENT names would have passed too, though the
understatement of what an apply can DELETE would be untouched.

So there are three rules. None of them knows a label string it did not import,
and each is named for the SHAPE it matches, not for the idea behind it:

  retired_literal_offenders
      a retired name, in full, on a line that does not mark the retirement
  retired_slash_run_offenders
      a retired name inside a "DV "-prefixed slash-run: "DV FEL/MEL/P8/P5"
  explicit_label_count_offenders
      a spelled or digit COUNT immediately before the word "label(s)", on a
      closure line, where the count is not len(MANAGED)

A guard reporting green on the highest-proximity live violation in the repo is
worse than no guard: it converts an unswept surface into an apparently swept
one. TestEachRuleFiresOnTheDefectItExistsToCatch therefore runs each rule
against the defect it exists to catch AND against the correct prose it must not
condemn.


=============================== LIMITS ===============================

READ THIS BEFORE TRUSTING A GREEN RUN. These rules are LITERAL SHAPE MATCHERS.
An earlier round of this file claimed they "detect the invariant, not the
literal". That claim was FALSE and has been retracted: an invariant about
natural-language prose is not something a regex holds, and three rounds of
widening did not change that. What follows is the honest reach.

WHAT IS CAUGHT -- executed by TestTheWhatIsCaughtBulletIsNotOverstatedEither,
because a summary of a rule's reach drifts in BOTH directions and only the
uncaught half used to be pinned.

  * "DV P8" / "DV P5" written out, on a line that does not mark the retirement.
  * The exact prefix-factored form "DV FEL/MEL/P8/P5" (any DV-prefixed
    slash-run one of whose later members is P8 or P5) -- SUBJECT TO TWO GATES,
    named here because the bullet below names all three of its own and this one
    used to name none:

      - a RETIREMENT_MARKERS word anywhere on the line suppresses the WHOLE
        line, so "Renamed set: Applies DV FEL/MEL/P8/P5 ..." is not reported;
      - the run's FIRST token being a retired abbreviation skips the run before
        the later-member test runs, so "Applies DV P8/P5 ..." is not reported
        by THIS rule. That one survives only because the literal rule catches
        it -- which is coverage by a different rule, not by this one.
  * A WRONG count immediately before "label(s)": "four labels", "4 managed
    labels" -- a count word or digit, then at most three words, then
    "label"/"labels", on a line that also says "managed" / "closed set" /
    "closed to" with DV vocabulary nearby.

    WRONG is the operative word. The rule reports only when the count is not
    len(MANAGED), so a count of NINE can never be reported: "closed to nine
    Plex DV labels" returns []. That is correct behaviour -- the rule exists to
    report DRIFT -- but it means this bullet describes catching a WRONG count,
    and "nine Plex DV labels" was the wrong example for it.

    Three shapes that match the pattern are deliberately EXCLUDED and are not
    caught, each explained in explicit_label_count_offenders and its helpers:
      - a lower/upper bound (_QUANTIFIER_BEFORE): "at least four labels";
      - a distributive (_DISTRIBUTIVE_WITHIN): "one badge per managed label";
      - "one" used as a singular reference (_SINGULAR_REFERENCE): "HDR10 is
        the one managed DV label ...".
    All three are correct prose that happens to contain a count, so condemning
    them would be the pressure-to-delete-the-rule failure this file keeps
    warning about. They are listed here so a green run is not read as "no
    count claim on this line".

KNOWN UNCAUGHT -- each of these was demonstrated to pass a full green run, and
each is pinned by a test in TestTheseKnownGapsAreOpen so this list cannot rot:

  1. AN UNDERSTATING ENUMERATION WITH NO COUNT WORD AT ALL. "The managed set is
     closed to: DV FEL, DV MEL, DV8, DV5." This is the shape the ORIGINAL
     finding was written in, and no rule here sees it. It is the largest hole
     and it is deliberate: deciding whether a list is exhaustive or illustrative
     is not a regex judgement.
  2. The count without the noun: "these four are managed".
  3. The count after the noun, or with a different noun: "closed to those four
     names".
  4. Markup between the count and the noun: "**four** labels", "`four` labels",
     "four **labels**", "<b>four</b> labels".
  5. A claim split across two source lines. Every rule is line-at-a-time.
  6. An abbreviated retired name standing alone, outside any DV-prefixed
     slash-run: "the P8 label". SPACING IS NOT WHAT SAVES A RUN -- _DV_SLASH_RUN
     allows optional whitespace on BOTH sides of each slash, so the spaced form
     "DV FEL / MEL / P8 / P5" IS caught, spaces and all. What actually breaks a
     run is RE-PREFIXING it: in "DV FEL / DV MEL / P8 / P5" the second token
     matches as the bare word "DV", the run terminates at "DV FEL / DV", and
     P8/P5 never enter toks at all.
  7. Any surface this sweep does not read: only .md/.yml/.yaml under docs/,
     the repo root, scripts/ and frontend/, plus .py/.ps1/.svelte/.ts/.js under
     backend/, scripts/ and frontend/src/. Strings assembled at run time, help
     text stored in the database, and anything under ARCHIVE_DIRS are all
     outside it.

     ONE FILE INSIDE THOSE ROOTS IS EXEMPT, AND ONLY FROM ONE RULE:
     backend/rename/dv_labeler.py is where RETIRED_LABELS is defined, so it is
     the one place the retired names belong unmarked -- but that is a reason to
     exempt it from the LITERAL rule, not from the sweep. It used to be skipped
     inside _code_files(), which removed it from _all_live_surfaces() and
     therefore from the slash-run and count rules too: a literal-rule
     justification silently applied to every rule, which is the same defect the
     flat exemption set had. It is now an ordinary RULE_EXEMPT entry under
     'retired_literal' alone, so the other two rules read it and a fourth rule
     added tomorrow gets it by default. (The alternative -- leaving the skip in
     _code_files and listing the file here beside ARCHIVE_DIRS -- was rejected:
     it documents the over-reach instead of removing it, and a future rule
     would still inherit the hole.)

DO NOT "FIX" THIS BY WIDENING. The owner's call after round three was: fix the
correctness bugs, stop the guard arms race. Each widening so far bought one
wording and left the class open, while making the file look more thorough than
it is. If you catch a real defect this file missed, the useful response is to
fix the defect and add its shape to the list above -- not to grow a regex.
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

#: A run of slash-separated names carried under ONE leading "DV ":
#: "DV FEL/MEL/P8/P5". Group 1 is the run itself.
#:
#: THIS REPLACED A BROADER RULE, and the narrowing is the point. The old rule
#: fired on any line that contained a bare 'P8'/'P5' token AND the word
#: "label" anywhere on it. That gate is a line-level substring, so it could not
#: tell a PLEX LABEL NAME from a DOLBY VISION PROFILE, and profiles 8 and 5
#: were never renamed. Four lines of correct prose were being flagged --
#:
#:   docs/superpowers/specs/2026-07-25-hdr10plus-label-kometa-overlay-design.md
#:     :40   "reconciling a P8+HDR10+ title would add one label and..."
#:     :332  "1. P8 + HDR10+ ends with both labels."
#:   docs/superpowers/plans/2026-07-22-4k-metadata-inventory.md
#:     :256  "Include P5/P8 in dry-run output, preserve non-managed labels..."
#:     :348  "Add P5/P8 badge references only after files exist..."
#:
#: -- and stayed silent only because all four sit under ARCHIVE_DIRS. A rule
#: that condemns correct text the moment someone writes it on a live page is
#: pressure to delete the rule, and the acceptance test that was supposed to
#: prove otherwise could not: all three of its "legal" examples lack a "label"
#: token, so the gate stopped them before the pattern ever ran.
#:
#: What is left is the one shape that is mechanically decidable and is the
#: shape the defect was actually written in. A DV-prefixed slash-run is label
#: vocabulary by construction -- profile prose does not write "DV FEL/MEL/P8".
#: Everything else abbreviated is now in LIMITS, uncaught and said so.
_DV_SLASH_RUN = re.compile(r"\bDV\s+([A-Za-z0-9.]+(?:\s*/\s*[A-Za-z0-9.]+)+)")

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


def retired_literal_offenders(rel, text):
    """A retired label written out in FULL on a line that does not mark it.

    Named for what it matches: the literal string. It is deliberately separate
    from retired_slash_run_offenders so that one defect is reported once --
    a DV-prefixed run whose FIRST member is a full retired name would otherwise
    print twice.
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


def retired_slash_run_offenders(rel, text):
    """A retired name inside a "DV "-prefixed slash-run: "DV FEL/MEL/P8/P5".

    ONE SHAPE, and the name says which. This is the form the caption under the
    destructive Sync button was written in -- the shared 'DV ' prefix factored
    out of the run, so the string 'DV P8' never appears and the literal rule
    called it clean.

    A DV-prefixed slash-run is Plex LABEL vocabulary by construction, which is
    why this needs no "is the line talking about labels?" gate and therefore
    cannot mistake a Dolby Vision PROFILE for a label name. Prose about the
    detection axis writes "FEL/MEL/P8/P5" or "P8 + HDR10+"; it does not write
    "DV FEL/MEL/P8".

    A run whose FIRST member is itself a retired abbreviation is skipped and
    left to the literal rule: "DV P8/P5" spells out the literal 'DV P8', so
    reporting it here too would print one defect as two.

    Everything else abbreviated -- "the P8 label", a spaced run that breaks the
    pattern -- is UNCAUGHT. See LIMITS in the module docstring.
    """
    if not RETIRED_ABBREV:
        return []
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(w in line.lower() for w in RETIREMENT_MARKERS):
            continue
        for m in _DV_SLASH_RUN.finditer(line):
            toks = [t.strip() for t in m.group(1).split("/")]
            # "DV P8/..." spells out a full retired name; the literal rule
            # reports it, and reporting it here too would double-count.
            if toks[0] in RETIRED_ABBREV:
                continue
            if any(t in RETIRED_ABBREV for t in toks[1:]):
                out.append(_quote(rel, i, line, m.start()))
                break
    return out


def explicit_label_count_offenders(rel, text):
    """An EXPLICIT count word or digit, immediately before "label(s)", that is
    not len(MANAGED).

    Named for the shape it matches, not for the idea. It does not detect
    "understating the managed set"; it detects one written form of doing so.
    The form it does catch is worth catching -- "Only these four labels are
    managed" is exactly as false with the CURRENT names as with the retired
    ones, and the undercount is what understates the destruction an apply can
    do -- and nothing in this function knows a label string; it imports the
    number from dv_labeler.MANAGED.

    WHAT IT DOES NOT SEE, all demonstrated and pinned in
    TestTheseKnownGapsAreOpen: an enumeration with no count word at all (the
    shape the original finding was written in), the count without the noun
    ("these four are managed"), the count after the noun ("closed to those four
    names"), markup between the two ("**four** labels"), and a claim spread
    over two lines. See LIMITS in the module docstring. Do not widen this.

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

    #: EXEMPTIONS ARE PER RULE. Nothing here is exempt from the sweep as a
    #: whole.
    #:
    #: This used to be one flat set filtered inside _live_docs() -- which runs
    #: BEFORE _all_live_surfaces() is built from it, so a file listed for the
    #: LITERAL rule silently vanished from every other rule as well, and would
    #: have vanished from any rule added later. docs/kometa/DV_BADGE_DESIGN.md
    #: is the file that made this matter: it is the terminology reference the
    #: runbook sends operators to, it needs the literal exemption because its
    #: "(retiring)" blocks ARE the migration, and it was consequently invisible
    #: to the slash-run and count rules too. Restating the original finding
    #: inside it -- "the managed set is closed to ... only these four labels
    #: are managed" -- produced a fully GREEN run. It no longer does.
    #:
    #: Keyed by rule name; a new rule adds a key with an empty dict, and gets
    #: full coverage until someone justifies an exemption from IT specifically.
    RULE_EXEMPT = {
        "retired_literal": {
            "docs/feature-pack-review/RENAMING_PIPELINE_AND_4K_METADATA_AUDIT_2026-07-22.md":
                "audit dated 2026-07-22; records the vocabulary as it was then",
            "docs/feature-prompts/dv-feature-status-review.md":
                "the mention sits inside a block already marked SUPERSEDED 2026-08-04 "
                "and quotes Plex label COUNTS observed on that date",
            "docs/reviews/2026-08-06-overnight-report.md":
                "dated report quoting label counts observed 2026-07-26",
            # Covered by TestTheDesignDocKeepsTheRetiringLabelsMarked instead:
            # it SHOULD name them, marked, for as long as the migration runs.
            "docs/kometa/DV_BADGE_DESIGN.md":
                "the retiring blocks are the migration, asserted separately",
            # NOT a sweep-wide skip. This is the definition site of
            # RETIRED_LABELS, so the retired names belong here unmarked -- a
            # reason to exempt it from the LITERAL rule and from nothing else.
            # It used to be skipped inside _code_files(), which hid it from
            # every rule at once; see that method.
            "backend/rename/dv_labeler.py":
                "defines RETIRED_LABELS; the names must appear unmarked here",
        },
        "retired_slash_run": {},
        "label_count": {},
    }

    #: The rule each key exempts from, so the tests below cannot drift from the
    #: table and so test_every_exemption_is_load_bearing can call the right one.
    RULE_FN = {
        "retired_literal": retired_literal_offenders,
        "retired_slash_run": retired_slash_run_offenders,
        "label_count": explicit_label_count_offenders,
    }

    def test_every_rule_has_an_exemption_table(self):
        """A rule with no key would silently fall back to "exempt nowhere" or
        crash at the first lookup, depending on how it was wired. Naming them
        together makes adding a rule a decision about coverage."""
        assert set(self.RULE_EXEMPT) == set(self.RULE_FN)

    def test_every_exempted_path_still_exists(self):
        """A renamed or deleted exemption must be noticed, not left to rot into
        a hole this sweep no longer looks through."""
        missing = [(rule, rel) for rule, tbl in self.RULE_EXEMPT.items()
                   for rel in tbl if not (REPO / rel).exists()]
        assert not missing, f"stale entries in RULE_EXEMPT: {missing}"

    def test_every_exemption_is_load_bearing(self):
        """An exemption must be silencing something. If the named rule does not
        actually fire on the file, the entry is doing nothing except hiding the
        file from a future version of that rule -- which is exactly the failure
        this table was restructured to prevent. Delete it instead."""
        idle = []
        for rule, tbl in self.RULE_EXEMPT.items():
            fn = self.RULE_FN[rule]
            for rel in tbl:
                text = (REPO / rel).read_text(encoding="utf-8")
                if not fn(rel, text):
                    idle.append(f"{rule}: {rel}")
        assert not idle, (
            "these exemptions silence nothing and must be deleted:\n  "
            + "\n  ".join(idle))

    def _surfaces_for(self, rule):
        """Every live surface this RULE is meant to read."""
        exempt = self.RULE_EXEMPT[rule]
        return [(rel, text) for rel, text in self._all_live_surfaces()
                if rel not in exempt]

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
            # ARCHIVE_DIRS only. Per-FILE exemptions are applied per RULE, in
            # _surfaces_for -- filtering them here is what made a
            # literal-rule exemption an exemption from every rule.
            if any(a in rel for a in self.ARCHIVE_DIRS):
                continue
            yield p, rel

    def _offenders(self, files):
        exempt = self.RULE_EXEMPT["retired_literal"]
        return [o for p, rel in files if rel not in exempt
                for o in retired_literal_offenders(rel, p.read_text(encoding="utf-8"))]

    def test_no_live_doc_names_a_retired_label_as_current(self):
        # The runbook is excluded HERE only because it has a stricter test of
        # its own -- one that understands the dv-vocab blocks, where a retired
        # name is required. It is not exempt from the rule.
        files = [(p, rel) for p, rel in self._live_docs()
                 if rel != RUNBOOK.relative_to(REPO).as_posix()]
        assert files, "the live-doc sweep found nothing to scan"
        assert not self._offenders(files), (
            "live docs still use the pre-rename label names:\n  "
            + "\n  ".join(self._offenders(files)))

    def _code_files(self):
        """Every shipped code/UI file, with NO per-file exemption applied.

        dv_labeler.py used to be skipped RIGHT HERE, because it is where
        RETIRED_LABELS is defined and so the one place the retired names belong
        unmarked. That is a LITERAL-rule justification, and skipping the file
        here applied it to every rule: the file dropped out of
        _all_live_surfaces() too, so the slash-run and count rules never read
        it either, and a fourth rule would have inherited the hole silently.
        Exactly the defect the flat exemption set had, one level up. The
        exemption now lives in RULE_EXEMPT["retired_literal"] where it belongs
        and where test_every_exemption_is_load_bearing can check it.
        """
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
                    files.append((p, p.relative_to(REPO).as_posix()))
        return files

    def test_no_shipped_code_or_ui_names_a_retired_label_as_current(self):
        files = self._code_files()
        assert files, "the code sweep found nothing to scan"
        assert not self._offenders(files), (
            "shipped code or UI still uses the pre-rename label names:\n  "
            + "\n  ".join(self._offenders(files)))

    def _all_live_surfaces(self):
        """Docs AND code, together, with NO per-file exemption applied. The two
        literal sweeps above are kept apart because they read different roots;
        the rules below apply to every live surface regardless of which file
        type it lives in, and the defect that prompted them was in a .svelte
        caption while its twin was in a .ps1 Task Scheduler description.

        Per-file exemptions are applied by _surfaces_for, per RULE. Applying
        them here (or in _live_docs, where they used to live) is what turned a
        literal-rule exemption into an exemption from everything."""
        seen, out = set(), []
        for p, rel in self._live_docs():
            seen.add(rel)
            out.append((rel, p.read_text(encoding="utf-8")))
        for p, rel in self._code_files():
            if rel in seen:
                continue
            out.append((rel, p.read_text(encoding="utf-8")))
        return out

    def test_no_live_surface_puts_a_retired_name_in_a_dv_slash_run(self):
        """D1/D2: the renames caption read "Applies DV FEL/MEL/P8/P5 ..." --
        the retired vocabulary with the shared prefix factored out. It contains
        no literal 'DV P8', so the literal sweep called it clean while it sat
        directly under the destructive Sync Plex labels button.

        This catches THAT SHAPE. Other abbreviated forms are uncaught; LIMITS
        says so."""
        surfaces = self._surfaces_for("retired_slash_run")
        assert surfaces, "the slash-run sweep found nothing to scan"
        offenders = [o for rel, text in surfaces
                     for o in retired_slash_run_offenders(rel, text)]
        assert not offenders, (
            "a live surface names a retired label inside a DV-prefixed "
            "slash-run:\n  " + "\n  ".join(offenders))

    def test_no_live_surface_states_a_label_count_that_is_not_len_MANAGED(self):
        """The other half, and the more dangerous one. "Only these four labels
        are managed - your own labels are never touched" understates what
        reconcile_movie may STRIP by five labels, and it stays false if the
        four are renamed to the current names. The count is compared to
        len(dv_labeler.MANAGED), imported.

        Only an EXPLICIT count immediately before the noun is seen. An
        understating list with no number in it passes; that is gap 1 in
        LIMITS."""
        surfaces = self._surfaces_for("label_count")
        assert surfaces, "the label-count sweep found nothing to scan"
        offenders = [o for rel, text in surfaces
                     for o in explicit_label_count_offenders(rel, text)]
        assert not offenders, (
            "a live surface states a managed-label count that disagrees with "
            "dv_labeler.MANAGED:\n  " + "\n  ".join(offenders))

    def test_a_per_rule_exemption_is_not_an_exemption_from_every_rule(self):
        """B1, asserted directly on the tables rather than on a symptom.

        docs/kometa/DV_BADGE_DESIGN.md is exempt from the literal rule and from
        nothing else, so the other two rules must still be handed it. When the
        exemptions were one flat set filtered inside _live_docs(), this file
        reached NO rule -- and it is the terminology reference the runbook
        points operators at."""
        design_rel = DESIGN.relative_to(REPO).as_posix()
        assert design_rel in self.RULE_EXEMPT["retired_literal"]
        for rule in ("retired_slash_run", "label_count"):
            reached = {rel for rel, _ in self._surfaces_for(rule)}
            assert design_rel in reached, (
                f"{design_rel} is exempt from retired_literal and is not being "
                f"swept by {rule} either -- an exemption from one rule has "
                "again become an exemption from all of them")

    def test_EVERY_exempted_file_still_reaches_every_OTHER_rule(self):
        """The general form, so a new entry cannot reopen B1 one file over.

        The test above names DV_BADGE_DESIGN.md specifically because that is the
        file the defect was found on. This one holds for the whole table, and it
        is what pins backend/rename/dv_labeler.py: that file used to be skipped
        inside _code_files(), which is upstream of _all_live_surfaces(), so a
        LITERAL-rule justification removed it from the slash-run and count rules
        as well. Nothing hid at the time -- both returned [] on it -- so only a
        structural assertion catches it. LIMITS gap 7 says this in prose; this
        is the executable half of that sentence."""
        for rule, table in self.RULE_EXEMPT.items():
            for rel in table:
                for other in self.RULE_EXEMPT:
                    if other == rule:
                        continue
                    reached = {r for r, _ in self._surfaces_for(other)}
                    assert rel in reached, (
                        f"{rel} is exempt from {rule} and is not swept by "
                        f"{other} either -- an exemption from one rule has "
                        "again become an exemption from all of them")

    def test_the_labeller_definition_site_is_exempt_only_from_the_literal_rule(self):
        """D4 named, so the two options cannot silently swap back.

        Skipping it inside _code_files() and listing it in LIMITS beside
        ARCHIVE_DIRS was the rejected alternative: it documents the over-reach
        instead of removing it, and a fourth rule would still inherit it."""
        rel = "backend/rename/dv_labeler.py"
        assert rel in self.RULE_EXEMPT["retired_literal"], (
            "the definition site is no longer a per-rule exemption -- if it "
            "went back into _code_files(), it is hidden from every rule again")
        assert rel not in self.RULE_EXEMPT["retired_slash_run"]
        assert rel not in self.RULE_EXEMPT["label_count"]
        code_rels = {r for _, r in self._code_files()}
        assert rel in code_rels, (
            "_code_files() is skipping the definition site again, which takes "
            "it out of _all_live_surfaces() and so out of every other rule")

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


class TestEachRuleFiresOnTheDefectItExistsToCatch:
    """A guard written beside the code it checks passes BY CONSTRUCTION. The
    literal sweep did exactly that: it went green against a tree whose most
    dangerous surface still carried the defect. So each rule is run here
    against the defect it exists to catch, AND against the correct prose it
    must not condemn.

    This class shows each rule fires on ONE example. It does not show, and
    cannot show, that the rule covers the class of defects that example belongs
    to -- see LIMITS, and TestTheseKnownGapsAreOpen below, which is the other
    half of the same honesty.

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
        assert retired_slash_run_offenders(
            "frontend/src/routes/renames/+page.svelte", self.PRE_FIX_CAPTION), (
            "the slash-run rule does not catch the caption that prompted it")

    def test_it_flags_the_count_in_that_same_caption(self):
        assert explicit_label_count_offenders("x.svelte", self.PRE_FIX_CAPTION), (
            "the count rule does not catch 'only these four labels are managed'")

    def test_it_flags_a_wrong_count_that_uses_the_CURRENT_names(self):
        """The rename is not the defect. This line names DV8/DV5 correctly and
        is still false: MANAGED is nine, and reconcile_movie can strip DV7, DV,
        HDR10 and the two retiring names too. So the count rule must catch it
        with the slash-run rule silent."""
        synthetic = ("Applies <code>DV FEL/DV MEL/DV8/DV5</code> to the exact copy "
                     "Plex serves. Only these four labels are managed.")
        assert not retired_slash_run_offenders("x.svelte", synthetic), (
            "premise broken: the synthetic line uses only CURRENT names, so the "
            "slash-run rule must be silent and the count rule must be "
            "catching it unaided")
        assert explicit_label_count_offenders("x.svelte", synthetic), (
            "a current-names wrong count passes, so the count rule is keyed to "
            "the retired NAMES rather than to the number")

    def test_it_accepts_a_correct_count(self):
        assert not explicit_label_count_offenders(
            "x.svelte", "Applies the DV layer badges to the copy Plex serves. "
                        "Nine labels are managed in total.")

    def test_the_count_is_read_from_MANAGED_at_run_time(self, monkeypatch):
        """Not hardcoded: it is len(dv_labeler.MANAGED). Grow MANAGED and the
        same correct sentence must become an offence."""
        import sys
        mod = sys.modules[explicit_label_count_offenders.__module__]
        ok = "Nine DV labels are managed in total."
        assert not explicit_label_count_offenders("x.md", ok)
        monkeypatch.setattr(mod, "MANAGED", MANAGED | {"DV Synthetic"})
        assert explicit_label_count_offenders("x.md", ok), (
            "'Nine labels are managed' stayed legal after MANAGED grew to ten, "
            "so the rule is not reading MANAGED at all")

    def test_it_accepts_the_layer_axis_shorthand(self):
        """Profiles 8 and 5 were never renamed -- only the LABEL names were.
        Prose about the detection axis must stay legal, or the rule becomes
        pressure to rewrite correct text.

        NOTE ON WHAT THESE THREE USED TO PROVE: nothing. Under the old rule a
        line reached the pattern only if it also contained the token "label",
        and none of these does -- the gate stopped all three before the pattern
        ran, so they would have passed against any pattern whatsoever. The
        MIXED-line test below is the one that exercises the profile-versus-label
        distinction; these are kept because the shorthand is real prose in the
        tree and must stay legal.
        """
        for legal in (
            "FEL/MEL/P5/P8 layer evidence, file signature, tool evidence, and scan time.",
            "FEL/MEL/P5/P8 spot checks agree with independent dovi_tool evidence;",
            "/** Read-only DV layer joined from dv_scan by path at serialize "
            "time (FEL/MEL/P8/P5). Null = unknown. */",
        ):
            assert not retired_slash_run_offenders("x.md", legal), (
                f"false positive on: {legal}")

    def test_it_accepts_profile_prose_that_ALSO_says_label(self):
        """C2, and the reason the old rule had to be narrowed. Its "is this
        line about labels?" gate was a line-level substring, so ANY sentence
        about Dolby Vision PROFILES that happened to contain the word "label"
        was flagged -- profiles 8 and 5 were never renamed, so all four of
        these are CORRECT prose being condemned.

        VERBATIM from the tree, with the paths, because a paraphrase is the
        test author's belief again. All four are archive-exempt today, which is
        the only reason nobody saw the rule firing: written on a live page,
        each one turns this file red and creates pressure to delete the rule.
        """
        for rel, line in (
            ("docs/superpowers/specs/2026-07-25-hdr10plus-label-kometa-overlay-design.md:40",
             "make the two compete: reconciling a P8+HDR10+ title would add one label and"),
            ("docs/superpowers/specs/2026-07-25-hdr10plus-label-kometa-overlay-design.md:332",
             "1. P8 + HDR10+ ends with both labels."),
            ("docs/superpowers/plans/2026-07-22-4k-metadata-inventory.md:256",
             "Preserve the managed label set. Do not write labels automatically "
             "from a scan. Include P5/P8 in dry-run output, preserve non-managed "
             "labels, and make live/seed disagreement visible to the caller."),
            ("docs/superpowers/plans/2026-07-22-4k-metadata-inventory.md:348",
             "Document a 25-50 item pilot, storage-load telemetry, backup paths "
             "discovered at execution time, a zero-write Plex label dry-run, and "
             "all full-scan stop conditions. Add P5/P8 badge references only "
             "after files exist and a Kometa config syntax check passes. Do not "
             "run Kometa or change production settings in this task."),
        ):
            assert "label" in line.lower(), (
                "premise broken: this example no longer mixes profile prose with "
                "the word 'label', so it cannot show the false positive at all")
            assert not retired_slash_run_offenders(rel, line), (
                f"correct profile prose is being flagged: {rel}")

    def test_it_accepts_a_profile_version(self):
        assert not retired_slash_run_offenders(
            "x.md", "treat MEL as uninteresting (= P8.1). Affects label vocabulary.")

    def test_it_accepts_a_line_that_marks_the_retirement(self):
        """The runbook and the caption MUST be able to name the retiring
        labels: the dry run reports removing them, and an operator who was not
        told reads a correct migration as a fault.

        ISOLATED on purpose: this line IS a DV-prefixed slash-run with P8/P5 in
        it, so the pattern matches and ONLY the retirement marker can save it.
        A phrasing the marker and something else both protect proves nothing --
        that is how two mutations survived the previous round.
        """
        line = "The retiring DV FEL/MEL/P8/P5 label names are removed by this sync."
        assert _DV_SLASH_RUN.search(line), (
            "premise broken: this line no longer reaches the pattern, so the "
            "retirement marker cannot be shown doing anything")
        assert not retired_slash_run_offenders("x.md", line)

    def test_it_leaves_the_FULL_names_to_the_literal_rule(self):
        """One defect, one report. "DV P8/P5" spells out the literal 'DV P8',
        so retired_literal_offenders owns it and the slash-run rule must stay
        quiet -- otherwise the failure message reads like two defects."""
        line = "Applies DV P8/P5 to Plex."
        assert retired_literal_offenders("x.svelte", line), (
            "premise broken: the literal rule does not catch this line, so the "
            "slash-run rule staying silent leaves a hole")
        assert not retired_slash_run_offenders("x.svelte", line), (
            "both rules report the same occurrence; one defect will print as "
            "two")

    def test_it_accepts_a_lower_bound_rather_than_a_set_size(self):
        """Verbatim from the whole-repo audit. "at least one managed DV label"
        counts LABELLED TITLES, not the managed set."""
        assert not explicit_label_count_offenders(
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
        assert not explicit_label_count_offenders("x.md", line)

    def test_it_accepts_a_singular_reference(self):
        """Third shape from the audit. The runbook is one word from writing
        this sentence today -- it currently says "the one TAG not derived from
        a DV verdict alone"."""
        assert not explicit_label_count_offenders(
            "x.md", "HDR10 is the one managed DV label not derived from a "
                    "Dolby Vision verdict alone.")

    def test_it_leaves_a_different_closed_set_alone(self):
        """version_labeler's set is deliberately separate and legitimately
        counts itself. A rule that condemned it would be pressure to make a
        correct doc wrong."""
        assert not explicit_label_count_offenders(
            "docs/specs/versions.md",
            "Version labels need their own closed set: the four labels "
            "{2,3,4,5+} Versions are managed by a different pass."), (
            "the count rule escaped DV scope and is policing the version set")


class TestTheWhatIsCaughtBulletIsNotOverstatedEither:
    """D5: LIMITS is a claim in BOTH directions, and only one of them was
    executed.

    TestTheseKnownGapsAreOpen pins everything LIMITS says is UNCAUGHT. Nothing
    pinned what it says IS caught, and that half had drifted: the count bullet
    offered "nine Plex DV labels" as an example of a catch, when a count of nine
    is the one count the rule can never report; and it listed none of the three
    exclusion gates, so three correct-prose shapes read as caught.

    That is the same failure mode as the retracted "detects the invariant"
    claim, one size down: a summary promising more reach than the code has. So
    the positive half gets executed too."""

    def test_a_count_equal_to_len_MANAGED_can_never_be_reported(self):
        """Not a gap -- the rule's entire job is to report DRIFT -- but LIMITS
        used to offer this exact sentence as an example of a catch."""
        line = "The managed DV set is closed to nine Plex DV labels."
        assert _COUNT_OF_LABELS.search(line), (
            "premise broken: the count pattern must match, or something other "
            "than the equality check is doing the work")
        assert len(MANAGED) == 9, (
            "MANAGED is no longer nine, so this example must be renumbered "
            "here and in LIMITS")
        assert not explicit_label_count_offenders("x.md", line)
        # Control, one variable: the same sentence with a WRONG count.
        assert explicit_label_count_offenders(
            "x.md", line.replace("nine", "four")), (
            "premise broken: a wrong count in this sentence must be reported")

    def test_a_lower_bound_is_excluded_not_caught(self):
        line = "The DV managed set has at least four labels."
        assert _COUNT_OF_LABELS.search(line), "premise broken"
        assert not explicit_label_count_offenders("x.md", line), (
            "a bound is now CAUGHT -- it is correct prose, so this is a "
            "regression; fix the rule, not this test")
        # One variable: drop the bound, keep everything else.
        assert explicit_label_count_offenders(
            "x.md", line.replace("at least ", "")), (
            "premise broken: without the bound the same sentence must fire, "
            "or _QUANTIFIER_BEFORE is not what excludes it")

    #: The control swaps ONLY the word "per" for a filler of the same shape.
    #: It is deliberately not idiomatic English: its job is to hold the count
    #: pattern, the closure marker and the DV scope all fixed so that the one
    #: thing that differs is the token _DISTRIBUTIVE_WITHIN keys on.
    DISTRIBUTIVE = "DV overlays: one badge per managed label."
    DISTRIBUTIVE_CONTROL = "DV overlays: one badge with managed label."

    def test_a_distributive_is_excluded_not_caught(self):
        assert _COUNT_OF_LABELS.search(self.DISTRIBUTIVE), (
            "premise broken: the count pattern must match, or something other "
            "than _DISTRIBUTIVE_WITHIN is doing the excluding. Note the gap "
            "between the count and 'label' is capped at three words, so "
            "'one block per managed DV label' does not even reach the gates")
        assert not explicit_label_count_offenders("x.md", self.DISTRIBUTIVE), (
            "a distributive is now CAUGHT -- correct prose; fix the rule")
        assert self.DISTRIBUTIVE_CONTROL == self.DISTRIBUTIVE.replace(
            " per ", " with "), "the control varies more than the word 'per'"
        assert explicit_label_count_offenders("x.md", self.DISTRIBUTIVE_CONTROL), (
            "premise broken: the same line without 'per' must fire, or "
            "_DISTRIBUTIVE_WITHIN is not what excludes it")

    def test_one_as_a_singular_reference_is_excluded_not_caught(self):
        line = "HDR10 is the one managed DV label that needs a Plex cache read."
        assert _COUNT_OF_LABELS.search(line), "premise broken"
        assert not explicit_label_count_offenders("x.md", line), (
            "a singular reference is now CAUGHT -- correct prose; fix the rule")
        # One variable: 'the' before 'one' is what _SINGULAR_REFERENCE keys on.
        assert explicit_label_count_offenders(
            "x.md", line.replace("is the one", "is one")), (
            "premise broken: without the preceding 'the' the same sentence "
            "must fire, or _SINGULAR_REFERENCE is not what excludes it")

    def test_the_shapes_LIMITS_claims_ARE_caught_really_are(self):
        """The rest of the positive half, executed rather than asserted."""
        assert retired_literal_offenders(
            "x.md", "Plex still shows DV P8 on these titles.")
        assert retired_slash_run_offenders(
            "x.md", "Applies DV FEL/MEL/P8/P5 to the copy Plex serves.")
        for line in ("The managed DV set is closed to four labels.",
                     "Only these 4 managed DV labels exist in the closed set."):
            assert explicit_label_count_offenders("x.md", line), line



class TestTheseKnownGapsAreOpen:
    """THE LIMITS SECTION, EXECUTED. Every assertion here says a rule does NOT
    fire on a line that is genuinely wrong.

    This is not a specification and it is not a veto. It exists because the
    previous round of this file claimed a reach it did not have, and a prose
    disclaimer rots the moment someone edits a regex. If you deliberately widen
    a rule so one of these starts being caught, the honest edit is to DELETE
    that case here and delete its line from LIMITS in the module docstring --
    the failure is the reminder that the two must stay in step, not an argument
    against improving the rule.

    Each line below was produced by an adversarial reader against a fully green
    tree. Read them as "green does not mean swept".
    """

    #: Gap 1, and the important one: this is the shape the ORIGINAL SR2-2
    #: finding was written in -- an enumeration presented as the whole managed
    #: set, with no number anywhere in it.
    ENUMERATION_NO_COUNT = ("The managed set is closed to: DV FEL, DV MEL, "
                            "DV8, DV5.")

    @staticmethod
    def _reaches_the_count_rule(line):
        """The line passes both of the count rule's GATES, so if it escapes it
        escapes on the count pattern alone. Without this a gap test proves
        nothing: any line missing 'managed' or DV vocabulary is silent for a
        reason that has nothing to do with the gap being described."""
        return (any(w in line.lower() for w in _CLOSURE_MARKERS)
                and _DV_SCOPE.search(line))

    def test_an_understating_enumeration_with_no_count_word_is_invisible(self):
        line = self.ENUMERATION_NO_COUNT
        assert self._reaches_the_count_rule(line), "premise broken"
        assert LAYER_BADGES <= {t.strip(" .") for t in
                                line.split(": ")[1].split(", ")}, (
            "premise broken: this line no longer presents the layer-badge "
            "SUBSET as the whole managed set, so it is not the defect")
        assert not explicit_label_count_offenders("x.md", line)
        assert not retired_slash_run_offenders("x.md", line)
        assert not retired_literal_offenders("x.md", line)

    def test_the_count_without_the_noun_is_invisible(self):
        line = ("Only these four are managed; your own DV labels are never "
                "touched.")
        assert self._reaches_the_count_rule(line), "premise broken"
        assert explicit_label_count_offenders("x.md", line.replace(
            "four are managed", "four labels are managed")), (
            "premise broken: the same sentence WITH the noun must be caught, "
            "or this shows nothing about dropping the noun")
        assert not explicit_label_count_offenders("x.md", line)

    def test_the_count_attached_to_a_different_noun_is_invisible(self):
        line = "The managed DV set is closed to those four names."
        assert self._reaches_the_count_rule(line), "premise broken"
        assert explicit_label_count_offenders("x.md", line.replace(
            "four names", "four labels")), (
            "premise broken: the same sentence with 'labels' must be caught")
        assert not explicit_label_count_offenders("x.md", line)

    def test_markup_between_the_count_and_the_noun_is_invisible(self):
        plain = "Only these four labels are managed (DV)."
        assert explicit_label_count_offenders("x.md", plain), (
            "premise broken: the unmarked-up sentence must be caught, or the "
            "markup is not what is doing the hiding")
        for line in ("Only these **four** labels are managed (DV).",
                     "Only these `four` labels are managed (DV).",
                     "Only these four **labels** are managed (DV).",
                     "Only these <b>four</b> labels are managed (DV).",
                     "Only these <em>four</em> labels are managed (DV)."):
            assert not explicit_label_count_offenders("x.md", line), (
                "this shape is now CAUGHT -- delete it from here and from "
                f"LIMITS gap 4: {line}")

    def test_a_claim_split_across_two_lines_is_invisible(self):
        wrapped = "The DV managed set is closed to\nfour labels in total."
        assert explicit_label_count_offenders("x.md", wrapped.replace("\n", " ")), (
            "premise broken: unwrapped, this sentence must be caught -- "
            "otherwise the line-at-a-time reading is not what hides it")
        assert not explicit_label_count_offenders("x.md", wrapped)

    #: Gap 6 needs THREE sentences, not two, because the earlier version of
    #: this test varied two things at once between its premise and its control
    #: (it added spaces AND re-prefixed the run) and then attributed the miss to
    #: the spacing. The spacing is not what hides it.
    RUN_UNSPACED = "Applies DV FEL/MEL/P8/P5 to the copy Plex serves."
    RUN_SPACED = "Applies DV FEL / MEL / P8 / P5 to the copy Plex serves."
    RUN_RE_PREFIXED = "Applies DV FEL / DV MEL / P8 / P5 to the copy Plex serves."

    def test_spacing_alone_does_NOT_hide_a_retired_name_in_a_slash_run(self):
        """One variable: spaces around the slashes, nothing else.

        _DV_SLASH_RUN allows optional whitespace on both sides of each slash, so
        both forms are CAUGHT. This is the control for the test below: it proves
        the miss there cannot be blamed on the spacing."""
        assert retired_slash_run_offenders("x.md", self.RUN_UNSPACED), (
            "premise broken: the unspaced run must be caught")
        assert retired_slash_run_offenders("x.md", self.RUN_SPACED), (
            "spacing now BREAKS the run -- LIMITS gap 6 says the opposite and "
            f"must be corrected: {self.RUN_SPACED}")

    def test_re_prefixing_the_run_IS_what_hides_it(self):
        """One variable against RUN_SPACED: the second member gains a 'DV '.

        Both lines are spaced identically, so spacing is held fixed and the only
        difference is the repeated prefix. The run then matches as
        'DV FEL / DV' -- toks == ['FEL', 'DV'] -- and P8/P5 are never inspected."""
        assert retired_slash_run_offenders("x.md", self.RUN_SPACED), (
            "premise broken: the spaced run must be caught, or re-prefixing is "
            "not what does the hiding")
        assert self.RUN_RE_PREFIXED == self.RUN_SPACED.replace(
            "/ MEL", "/ DV MEL"), (
            "the control no longer differs from the premise by exactly the "
            "repeated prefix")
        assert not retired_slash_run_offenders("x.md", self.RUN_RE_PREFIXED), (
            "this shape is now CAUGHT -- delete it from here and from "
            f"LIMITS gap 6: {self.RUN_RE_PREFIXED}")
        toks = [t.strip() for t in
                _DV_SLASH_RUN.search(self.RUN_RE_PREFIXED).group(1).split("/")]
        assert toks == ["FEL", "DV"], (
            "the run no longer terminates at the repeated prefix, so the stated "
            f"reason for gap 6 is wrong: toks={toks}")

    def test_a_bare_abbreviation_in_no_run_at_all_is_invisible(self):
        line = "Plex still carries the P8 label on 114 titles."
        assert not _DV_SLASH_RUN.search(line), (
            "premise broken: this line must contain no DV slash-run at all")
        assert not retired_slash_run_offenders("x.md", line), (
            "this shape is now CAUGHT -- delete it from here and from "
            f"LIMITS gap 6: {line}")

    def test_the_sweep_reads_no_other_file_type(self):
        """Gap 7 as a fact about the globs, not a claim about prose. A managed-
        set claim living in an .html partial, a .json fixture or a database row
        is simply not read."""
        cls = TestNoLiveSurfaceNamesARetiredLabelAsCurrent()
        suffixes = {rel.rsplit(".", 1)[-1].lower()
                    for rel, _ in cls._all_live_surfaces() if "." in rel}
        assert suffixes <= {"md", "yml", "yaml", "py", "ps1", "svelte", "ts", "js"}
        assert "html" not in suffixes and "json" not in suffixes


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
