"""Coverage: raw traversal observations, and a separate evaluator that judges them.

Peer review round 15, §7. The crawler is the only component that can honestly
observe page order, parser recognition, repeats, gaps and early stops -- so it
must emit those facts. But it must not be the thing that converts them into
"therefore this release is covered". That is the same separation that made the
listing ledger safe: recording an observation is not granting a permission.

    ScannerService     -> TraversalReport   (what was seen, in order)
    CoverageEvaluator  -> CoverageProof     (what that justifies)
    attestation writer -> consumes only a qualifying CoverageProof

Nothing in this module writes `category_attested`, and nothing here touches the
database. It is pure over its inputs so the proof rules can be tested without
crawling anything.

THE RULE THAT MOTIVATES THE DESIGN. A frontier is a claim about how deep in TIME
a contiguous traversal reached. It is NOT `min(observed posted_date)`: a pinned
"sticky" post carries an old date and would manufacture a deep frontier out of a
shallow crawl. So the frontier is derived from LISTING ORDER, and a date that
appears out of order is a reason to refuse rather than evidence.

AND THE FRONTIER IS STILL ONLY TELEMETRY. Round 17 showed that ordering checks
defeat one terminal anomaly and no fixed count defeats k+1 of them, so a
timestamp frontier is a negative proof only where the SOURCE guarantees a
chronological stream. `ORDERING_CONTRACTS` is empty, so nothing here can
currently authorise anything -- by construction, not by convention.
"""
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import (Any, Dict, FrozenSet, List, Mapping, Optional,
                    Sequence, Set, Tuple)

#: Bumped whenever the proof RULES change. Part of the proof, per §8.8: a proof
#: is only meaningful alongside the version of the logic that produced it.
EVALUATOR_VERSION = 1

#: A page is only usable evidence when the request succeeded AND the parser
#: recognised the listing. HTTP 200 alone is not recognition -- a layout change
#: or an interstitial returns 200 and parses to nothing.
PAGE_OK = "ok"
PARSER_RECOGNISED = "recognised"

#: SOURCES WITH A DECLARED ORDERING CONTRACT. Deliberately EMPTY.
#:
#: Round 17 (M17-1). A timestamp frontier is only a negative proof if the source
#: guarantees the listing is a chronological stream. Corroboration defeats one
#: terminal anomaly and no fixed number defeats k+1 of them, so counting is not
#: the missing ingredient -- a source-observable invariant is: a pin/sticky
#: marker the crawler can see and exclude, a documented chronological feed
#: contract, or an API cursor with explicit ordering semantics.
#:
#: Until a source appears here with such a contract, every frontier this module
#: derives is INSPECTABLE TELEMETRY and cannot mint anything. Keeping the gate in
#: code rather than in a comment is deliberate: the previous version of this
#: limitation lived in a docstring, and the docstring was wrong.
#:
#: Adding an entry is a reviewed decision, not a configuration change.
#: Keyed on the COMPLETE revision -- (arm_id, request_definition_version,
#: parser_version). Round 21 (R21-13).
#:
#: Round 18 narrowed this from `source` to (arm_key, parser_version), because a
#: contract for one HDEncode endpoint would otherwise have marked 4K, Remux and
#: TV Packs authoritative together and survived a parser rewrite that changed
#: what the listing order MEANS.
#:
#: That was still one component short. Two request definitions can be published
#: under one arm_id deliberately -- the exact case round 20 rebuilt the ledger
#: identity around -- so a contract reviewed for
#:
#:     arm.hdencode.4k-2160p  ?tag=movies
#:
#: would have been inherited by
#:
#:     arm.hdencode.4k-2160p  ?tag=restored-movies
#:
#: which nobody reviewed and which need not be chronological at all. The ledger
#: carried the full revision while this boundary did not, so the identity fix
#: stopped one layer short of the thing it was protecting.
#:
#: A contract is a claim about a specific feed, requested a specific way, read
#: by a specific parser. It transfers to none of the three.
ORDERING_CONTRACTS: Dict[Tuple[str, str, str], str] = {}

_DATE_FORMATS = ("%B %d, %Y at %I:%M %p", "%B %d, %Y")


def parse_site_date(raw: Optional[str]) -> Optional[datetime]:
    """The site's own string, or None if we cannot read it.

    Deliberately narrow. An unparseable date is NOT a date -- it must not become
    a frontier anchor, and guessing at a format would invent ordering the site
    never expressed. Values carry no timezone, so comparisons are only valid
    WITHIN one source, which the evaluator enforces by never comparing across
    sources.
    """
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(str(raw).strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


# ── the raw observations ──────────────────────────────────────────────────

@dataclass
class Sighting:
    """One release seen at one position in one listing page."""
    position: int
    canonical_url: str
    raw_url: str = ""
    duplicate_in_run: bool = False
    policy_excluded: bool = False


@dataclass
class Page:
    page_number: int
    request_outcome: str = PAGE_OK
    http_status: int = 200
    parser_state: str = PARSER_RECOGNISED
    page_error: Optional[str] = None
    sightings: List[Sighting] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return (self.request_outcome == PAGE_OK
                and self.parser_state == PARSER_RECOGNISED
                and not self.page_error)


@dataclass
class Arm:
    #: The stable, opaque declared id -- what POLICY names.
    arm_key: str
    listing_type: str
    parser_version: str = "unknown"
    #: What was actually REQUESTED. Empty for a feed the registry does not
    #: declare, which is why such a feed can never match a contract.
    request_definition_version: str = ""
    pages: List[Page] = field(default_factory=list)

    @property
    def revision(self) -> Tuple[str, str, str]:
        """The full evidence identity, in the order contracts are keyed."""
        return (self.arm_key, self.request_definition_version,
                self.parser_version)


@dataclass
class TraversalReport:
    run_id: str
    source: str
    started_at: str = ""
    completed_at: str = ""
    requested_mode: str = ""
    early_stop_enabled: bool = True
    termination: str = "not_run"
    arms: List[Arm] = field(default_factory=list)


# ── the derived conclusion ────────────────────────────────────────────────

@dataclass
class CoverageProof:
    """How deep one arm was CONTIGUOUSLY traversed, and on what evidence."""
    run_id: str
    source: str
    arm_key: str
    listing_type: str
    parser_version: str
    #: Carried so a stored proof records WHICH request definition it covers.
    #: Without it a proof of v1 is indistinguishable from a proof of v2.
    request_definition_version: str
    evaluator_version: int
    frontier_url: str
    frontier_date_raw: str
    frontier_date: datetime
    pages_traversed: int
    anchors_used: int
    #: False unless the SOURCE has a declared ordering contract. A frontier
    #: without one is telemetry: inspectable, comparable over time, and unable
    #: to mint attestation. See ORDERING_CONTRACTS.
    authoritative: bool = False
    ordering_contract: str = ""


@dataclass
class ArmVerdict:
    arm_key: str
    proof: Optional[CoverageProof]
    reason: str

    @property
    def proven(self) -> bool:
        return self.proof is not None


@dataclass(frozen=True)
class CoverageEvidenceSnapshot:
    """The date evidence a proof was derived from, sealed at capture time.

    Round 18 (M18-4). A proof is an argument about a moment. If the evidence it
    cites can change afterwards, the proof stops meaning what it said, and no
    reader can tell -- the proof looks identical either way.

    `capture()` COPIES. A read-only view over the caller's dictionary would not
    be enough: the caller still holds the original and can still write to it,
    and every mutation would be visible through the view.
    """

    dates: Mapping[str, str]
    unstable: FrozenSet[str]

    @classmethod
    def capture(cls, dates: Optional[Dict[str, str]],
                unstable: Optional[Set[str]] = None
                ) -> "CoverageEvidenceSnapshot":
        return cls(dates=MappingProxyType(dict(dates or {})),
                   unstable=frozenset(unstable or ()))


class CoverageEvaluator:
    """Derives a frontier from traversal observations. Grants nothing."""

    version = EVALUATOR_VERSION

    def __init__(self, dates: Any, unstable: Optional[Set[str]] = None):
        """`dates` maps canonical_url -> the site's raw publication string, or
        is an already-captured CoverageEvidenceSnapshot.
        `unstable` is the set whose recorded date has been seen to CHANGE
        (`posted_date_changed`); per §8.7 those cannot support timestamp
        coverage until resolved, so they are never anchors.

        Round 18 (M18-4): the inputs are CAPTURED, not retained. This used to
        hold the caller's dictionary by reference, so a proof could name a
        frontier date that no longer matched what the map said -- an enrichment
        pass writing a corrected date would silently rewrite the evidence that a
        past decision rested on.
        """
        if isinstance(dates, CoverageEvidenceSnapshot):
            if unstable is not None:
                raise ValueError(
                    "pass either a snapshot or raw inputs, not both: a second "
                    "unstable set would contradict the one already sealed")
            self._evidence = dates
        else:
            self._evidence = CoverageEvidenceSnapshot.capture(dates, unstable)

    # -- anchors ----------------------------------------------------------

    def _anchor(self, s: Sighting) -> Optional[Tuple[datetime, str]]:
        """Whether this sighting can anchor a frontier, at what time, and from
        which raw string.

        Returns BOTH, from a SINGLE read, so a proof's `frontier_date` and
        `frontier_date_raw` cannot describe two different observations
        (round 18, M18-4).

        Excluded, each for its own reason:
          duplicate_in_run  a repeat proves nothing about NEW depth (§8.4)
          policy_excluded   never fetched in detail, so no trustworthy date
          unstable date     the site moved it; ordering by it is unsound (§8.7)
          unparseable/absent  not a date (§8.6 -- cannot anchor, need not block)
        """
        if s.duplicate_in_run or s.policy_excluded:
            return None
        if s.canonical_url in self._evidence.unstable:
            return None
        raw = self._evidence.dates.get(s.canonical_url)
        when = parse_site_date(raw)
        return None if when is None else (when, str(raw or ""))

    # -- one arm ----------------------------------------------------------

    def evaluate_arm(self, report: TraversalReport, arm: Arm) -> ArmVerdict:
        """Walk the arm in listing order and find the deepest CORROBORATED anchor.

        WHAT THIS PRODUCES IS TELEMETRY, NOT AUTHORITY, and round 17 (M17-1) is
        why. I previously wrote here that being one anchor shallow "can only ever
        refuse a proof we might have been entitled to, never grant one we were
        not." That is FALSE, and the counterexample is two terminal outliers:

            Aug 20, Aug 19, Jan 2024 (sticky A), Dec 2023 (sticky B)

        The dates never ascend, so no inversion fires; sticky B corroborates
        sticky A, and the frontier becomes January 2024 -- years of coverage
        manufactured from one page. Corroboration defeats exactly ONE terminal
        anomaly, and my claim silently assumed the source has at most one.

        No fixed number of confirmations fixes this: any k is defeated by k+1.
        The missing ingredient is not more counting, it is a SOURCE-OBSERVABLE
        invariant -- a pin/sticky marker the crawler can see and exclude, a
        documented chronological feed contract, or an API cursor with explicit
        ordering. Without one, exhausting the entire contradictory listing is the
        only general negative proof for an unordered source.

        So a proof is marked `authoritative` only when the source has a declared
        ordering contract, and `ORDERING_CONTRACTS` is deliberately EMPTY. Every
        frontier this returns today is inspectable telemetry that cannot mint
        anything. That is a structural refusal rather than a comment, because a
        comment is what failed last time.

        The walk still refuses on the things it CAN see:
          * a gap or unusable page before the frontier (S8.1 / S8.2)
          * an inversion, which catches a sticky in the MIDDLE of a run
          * an uncorroborated terminal anchor, which catches a single sticky
            at the END
        """
        # CONTINUITY IS VALIDATED, NOT ASSUMED. Round 17 (M17-2).
        #
        # This used to sort the pages it was handed and check only whether each
        # PRESENT page was usable -- so an ABSENT page was invisible. The
        # crawler's generic exception path increments the error counter and
        # emits no page observation at all, which makes [1, 3] a reachable
        # report: the walk then carried page-1 depth straight into page 3 and
        # corroborated across a gap nobody observed.
        #
        # Validate rather than normalise. Sorting a broken sequence produces a
        # tidy sequence, which is exactly the wrong response.
        numbers = [p.page_number for p in arm.pages]
        if not numbers:
            return ArmVerdict(arm.arm_key, None, "no pages were observed")
        if len(set(numbers)) != len(numbers):
            return ArmVerdict(arm.arm_key, None,
                              "duplicate page numbers: %s" % sorted(numbers))
        ordered = sorted(numbers)
        if ordered[0] != 1:
            return ArmVerdict(arm.arm_key, None,
                              "traversal does not start at page 1 (starts at %d)"
                              % ordered[0])
        if ordered != list(range(1, len(ordered) + 1)):
            missing = sorted(set(range(1, ordered[-1] + 1)) - set(ordered))
            return ArmVerdict(arm.arm_key, None,
                              "page gap: %s never observed" % missing)

        confirmed: Optional[Tuple[datetime, Sighting]] = None
        pending: Optional[Tuple[datetime, Sighting]] = None
        anchors = 0
        pages_done = 0

        for page in sorted(arm.pages, key=lambda p: p.page_number):
            positions = [s.position for s in page.sightings]
            # Round 18 (M18-3): uniqueness alone accepted [1, 3] -- a page that
            # LOST a sighting -- and [2, 1], a page whose emitted order did not
            # match its claimed order. Sorting afterwards hid both. A complete
            # page numbers its sightings 1..N in the order they were read, so
            # require exactly that and refuse anything else.
            if positions != list(range(1, len(positions) + 1)):
                return ArmVerdict(
                    arm.arm_key, None,
                    "page %d sighting positions are not a complete 1..%d "
                    "sequence in emitted order: %s" % (
                        page.page_number, len(positions), positions))
            if not page.usable:
                return ArmVerdict(
                    arm.arm_key, None,
                    "page %d unusable (%s/%s%s)" % (
                        page.page_number, page.request_outcome, page.parser_state,
                        ", " + page.page_error if page.page_error else ""))
            pages_done += 1

            # Emitted order IS position order -- asserted immediately above.
            for s in page.sightings:
                found = self._anchor(s)
                if found is None:
                    # Not an anchor, but traversal continues past it (S8.6).
                    continue
                when, raw = found
                anchors += 1
                if pending is None:
                    pending = (when, raw, s)
                    continue
                if when <= pending[0]:
                    # The previous anchor is corroborated: something at least as
                    # old came after it, so it really was part of the sequence.
                    confirmed = pending
                    pending = (when, raw, s)
                else:
                    # A newer item BELOW an older one. Either a pinned post or an
                    # ordering we do not understand; a frontier argument depends
                    # on monotonicity, so refuse rather than guess.
                    return ArmVerdict(
                        arm.arm_key, None,
                        "listing order inversion at page %d position %d: %s is "
                        "newer than the anchor above it" % (
                            page.page_number, s.position, s.canonical_url))

        if confirmed is None:
            return ArmVerdict(
                arm.arm_key, None,
                "no corroborated anchor: %d anchor(s) seen, none confirmed by a "
                "later one" % anchors)
        when, raw, s = confirmed
        # the FULL revision -- (arm_id, request_definition_version,
        # parser_version) -- never the source, never across a parser change,
        # and never across a change to what was requested.
        _contract = ORDERING_CONTRACTS.get(arm.revision)
        return ArmVerdict(arm.arm_key, CoverageProof(
            run_id=report.run_id, source=report.source, arm_key=arm.arm_key,
            request_definition_version=arm.request_definition_version,
            listing_type=arm.listing_type, parser_version=arm.parser_version,
            evaluator_version=self.version,
            frontier_url=s.canonical_url,
            frontier_date_raw=raw,
            frontier_date=when, pages_traversed=pages_done, anchors_used=anchors,
            authoritative=bool(_contract),
            ordering_contract=str(_contract or ""),
        ), "frontier reached")

    # -- the question that matters ---------------------------------------

    def covers_release(self, report: TraversalReport, target_date_raw: str,
                       required_revisions: Sequence[Tuple[str, str, str]],
                       ) -> Tuple[bool, List[ArmVerdict], str]:
        """Was EVERY required arm traversed past this release?

        Round 17 (M17-3). This used to group verdicts by `listing_type` and
        accept a type as soon as ANY arm of that type crossed -- existential
        where the contract is universal. HDEncode has two movie arms, 4K and
        Remux, so a deep 4K traversal satisfied "movie" while a contradicting
        movie classification could sit untraversed in Remux. The tests could not
        see it because they used one arm per type, where `any` and `all` agree.

        The required set is passed in EXPLICITLY. Types are not a substitute:
        what has to be ruled out is a contradiction in a specific listing, and
        only the listing identity names it.

        Round 22 (R22-1): it is passed as exact REVISIONS -- `(arm_id,
        request_definition_version, parser_version)` -- not stable arm ids. A
        proof belongs to a revision, so a requirement expressed as a stable id
        could be satisfied by a proof for a RETIRED one. `backend.arms.
        active_revisions_for()` does the resolution, which keeps this module
        free of any dependency on the registry: it decides about the evidence
        in front of it and nothing else.

        Target-relative per S9: the question is always "did we get older than R",
        never "did we read N pages". A fixed page budget is never evidence.

        STRICTLY older. Equal timestamps do not prove crossing (S8.5): the site's
        strings are minute-resolution, so two releases in the same minute are
        unordered with respect to each other and neither can vouch for the other.
        """
        target = parse_site_date(target_date_raw)
        if target is None:
            return (False, [], "the target release has no readable date")
        required = [tuple(r) for r in (required_revisions or ())]
        if not required:
            # An empty requirement would make this vacuously true, which is the
            # most dangerous possible default for a negative proof.
            return (False, [], "no required arm revisions were specified")
        malformed = [r for r in required if len(r) != 3]
        if malformed:
            return (False, [],
                    "required revisions must be (arm_id, request definition, "
                    "parser) triples; got %r" % (malformed[0],))

        verdicts = [self.evaluate_arm(report, a) for a in report.arms]

        # KEYED ON THE FULL REVISION. Round 22 (R22-1).
        #
        # Round 21 keyed this on arm_key and refused when one id appeared
        # twice. That was safe against last-write-wins but could not express
        # the requirement at all: a report containing ONLY a retired revision
        # is not ambiguous, so the duplicate guard never fired and the retired
        # proof satisfied a requirement meant for the active one.
        #
        # Keying on the revision makes an extra retired arm simply irrelevant
        # rather than poisoning the whole id, and makes a lone retired
        # revision unable to satisfy anything.
        by_rev = {}
        for arm, v in zip(report.arms, verdicts):
            if arm.revision in by_rev:
                return (False, verdicts,
                        "arm %s was traversed twice under the identical "
                        "revision; which proof governs is undecidable"
                        % arm.arm_key)
            by_rev[arm.revision] = v

        for key in required:
            v = by_rev.get(key)
            if v is None:
                return (False, verdicts,
                        "required revision %s was not traversed at all "
                        "(the run carried %s)"
                        % (key, sorted(by_rev) or "nothing"))
            if not v.proven:
                return (False, verdicts,
                        "required arm %s has no usable frontier: %s"
                        % (key, v.reason))
            if not v.proof.authoritative:
                # The frontier may be perfectly measured and still prove
                # nothing: without an ordering contract the listing is not
                # known to be a chronological stream, and depth in an unordered
                # sequence is not depth in time.
                return (False, verdicts,
                        "required arm %s (parser %s) has no ordering contract, "
                        "so its frontier is telemetry and cannot support a "
                        "negative proof"
                        % (key, v.proof.parser_version))
            if not (v.proof.frontier_date < target):
                return (False, verdicts,
                        "required arm %s reached only %s, which is not strictly "
                        "older than the target"
                        % (key, v.proof.frontier_date_raw))

        return (True, verdicts,
                "all %d required arm(s) crossed the target" % len(required))
