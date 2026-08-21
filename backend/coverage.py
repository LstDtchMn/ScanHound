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
a contiguous traversal reached. It is NOT `min(observed posted_date)`: a single
pinned "sticky" post near the top of page one carries an old date and would
manufacture a deep frontier out of a shallow crawl. So the frontier is derived
from LISTING ORDER, and a date that appears out of order is treated as a reason
to refuse rather than as evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

#: Bumped whenever the proof RULES change. Part of the proof, per §8.8: a proof
#: is only meaningful alongside the version of the logic that produced it.
EVALUATOR_VERSION = 1

#: A page is only usable evidence when the request succeeded AND the parser
#: recognised the listing. HTTP 200 alone is not recognition -- a layout change
#: or an interstitial returns 200 and parses to nothing.
PAGE_OK = "ok"
PARSER_RECOGNISED = "recognised"

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
    arm_key: str
    listing_type: str
    parser_version: str = "unknown"
    pages: List[Page] = field(default_factory=list)


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
    evaluator_version: int
    frontier_url: str
    frontier_date_raw: str
    frontier_date: datetime
    pages_traversed: int
    anchors_used: int


@dataclass
class ArmVerdict:
    arm_key: str
    proof: Optional[CoverageProof]
    reason: str

    @property
    def proven(self) -> bool:
        return self.proof is not None


class CoverageEvaluator:
    """Derives a frontier from traversal observations. Grants nothing."""

    version = EVALUATOR_VERSION

    def __init__(self, dates: Dict[str, str], unstable: Optional[Set[str]] = None):
        """`dates` maps canonical_url -> the site's raw publication string.
        `unstable` is the set whose recorded date has been seen to CHANGE
        (`posted_date_changed`); per §8.7 those cannot support timestamp
        coverage until resolved, so they are never anchors.
        """
        self._dates = dates or {}
        self._unstable = set(unstable or ())

    # -- anchors ----------------------------------------------------------

    def _anchor_date(self, s: Sighting) -> Optional[datetime]:
        """Whether this sighting can anchor a frontier, and at what time.

        Excluded, each for its own reason:
          duplicate_in_run  a repeat proves nothing about NEW depth (§8.4)
          policy_excluded   never fetched in detail, so no trustworthy date
          unstable date     the site moved it; ordering by it is unsound (§8.7)
          unparseable/absent  not a date (§8.6 -- cannot anchor, need not block)
        """
        if s.duplicate_in_run or s.policy_excluded:
            return None
        if s.canonical_url in self._unstable:
            return None
        return parse_site_date(self._dates.get(s.canonical_url))

    # -- one arm ----------------------------------------------------------

    def evaluate_arm(self, report: TraversalReport, arm: Arm) -> ArmVerdict:
        """Walk the arm in listing order and find the deepest CORROBORATED anchor.

        Fails closed on the first unusable page: a gap BEFORE the frontier means
        the traversal was not contiguous, and a frontier claim on a broken walk
        is exactly the unearned negative this design exists to refuse.

        TWO DEFENCES AGAINST A STICKY POST, because one is not enough.

        The reviewer's counterexample is a pinned old entry at the BOTTOM of
        page one:

            Aug 20, Aug 20, Aug 19, Jan 2024   <- sticky

        Those dates descend, so an order check alone never fires and a naive
        walk adopts Jan 2024 -- manufacturing months of coverage from one page.
        `min(observed posted_date)` fails the same way, which is why it was
        rejected.

        So an anchor only becomes the frontier once a LATER anchor corroborates
        it by being no newer. Concretely:

          * sticky in the MIDDLE   the next anchor is newer -> inversion -> refuse
          * sticky at the END      nothing corroborates it -> it is held back,
                                   and the frontier stays at the last real anchor

        The cost is that the deepest anchor of any traversal is never claimed --
        the frontier is always one anchor short. That is the conservative
        direction, and being one release shallow can only ever refuse a proof we
        might have been entitled to, never grant one we were not.
        """
        confirmed: Optional[Tuple[datetime, Sighting]] = None
        pending: Optional[Tuple[datetime, Sighting]] = None
        anchors = 0
        pages_done = 0

        for page in sorted(arm.pages, key=lambda p: p.page_number):
            if not page.usable:
                return ArmVerdict(
                    arm.arm_key, None,
                    "page %d unusable (%s/%s%s)" % (
                        page.page_number, page.request_outcome, page.parser_state,
                        ", " + page.page_error if page.page_error else ""))
            pages_done += 1

            for s in sorted(page.sightings, key=lambda x: x.position):
                when = self._anchor_date(s)
                if when is None:
                    # Not an anchor, but traversal continues past it (S8.6).
                    continue
                anchors += 1
                if pending is None:
                    pending = (when, s)
                    continue
                if when <= pending[0]:
                    # The previous anchor is corroborated: something at least as
                    # old came after it, so it really was part of the sequence.
                    confirmed = pending
                    pending = (when, s)
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
        when, s = confirmed
        return ArmVerdict(arm.arm_key, CoverageProof(
            run_id=report.run_id, source=report.source, arm_key=arm.arm_key,
            listing_type=arm.listing_type, parser_version=arm.parser_version,
            evaluator_version=self.version,
            frontier_url=s.canonical_url,
            frontier_date_raw=str(self._dates.get(s.canonical_url) or ""),
            frontier_date=when, pages_traversed=pages_done, anchors_used=anchors,
        ), "frontier reached")

    # -- the question that matters ---------------------------------------

    def covers_release(self, report: TraversalReport, target_date_raw: str,
                       required_types: Sequence[str] = ("movie", "tv"),
                       ) -> Tuple[bool, List[ArmVerdict], str]:
        """Was every contradictory arm traversed PAST this release?

        Target-relative, per §9: a fixed page budget is never evidence by
        itself. The question is always "did we get older than R", never "did we
        read N pages".

        STRICTLY older. Equal timestamps do not prove crossing (§8.5): the site's
        strings are minute-resolution, so two releases in the same minute are
        unordered with respect to each other and one cannot vouch for the other.
        """
        target = parse_site_date(target_date_raw)
        if target is None:
            return (False, [], "the target release has no readable date")

        verdicts: List[ArmVerdict] = []
        by_type: Dict[str, List[ArmVerdict]] = {}
        for arm in report.arms:
            v = self.evaluate_arm(report, arm)
            verdicts.append(v)
            by_type.setdefault(arm.listing_type, []).append(v)

        for want in required_types:
            arms = by_type.get(want) or []
            if not arms:
                return (False, verdicts,
                        "no %s arm was traversed at all" % want)
            crossed = [v for v in arms
                       if v.proven and v.proof.frontier_date < target]
            if not crossed:
                why = "; ".join(
                    v.reason if not v.proven
                    else "%s frontier %s is not strictly older" % (
                        v.arm_key, v.proof.frontier_date_raw)
                    for v in arms)
                return (False, verdicts,
                        "the %s side was not traversed past the target: %s"
                        % (want, why))

        return (True, verdicts, "every contradictory arm crossed the target")
