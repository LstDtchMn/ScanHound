"""The coverage canary's decision logic: sampling, replay, and the four
post-promotion coverage states -- with no database, no network, and no clock
of its own.

REVEAL-FREE ZONE. Every function here takes plain dicts, lists and sets and
returns plain values. Nothing imports ``backend.database``, nothing opens a
socket, nothing calls ``datetime.now()``. The only notion of "now" any
function accepts is a value the caller passes in. That is deliberate, not an
oversight: it is what lets this module -- the part of the canary hybrid that
decides whether a promotion is safe -- be tested exhaustively without a
crawler, a schema, or a running listing source. The wiring that turns real
crawl rows into the shapes below lives elsewhere and is not this module's
concern.

VOCABULARY. A **cycle** is one recorded observation round of a listing
source. **Membership** is what that source showed in a cycle: rows of
``{cycle_uuid, source_key, canonical_url, observed_at, page_index,
rank_on_page, rss_present}``. **Depth** is how many listing pages the canary
crawls each cycle; a row is *within depth D* when ``page_index <= D``. Rows
beyond depth are not evidence of anything -- the canary never looked there --
so every function here either ignores them or is documented as trusting the
caller to have already dropped them.

THE FOUR COVERAGE STATES mirror ``classify_miss_resolution`` in
``backend/hdencode_shadow.py`` (see lines 531-603 there), which this project
already spent several peer-review rounds correcting. That function answers
"was a listing-only release ever provably acquired by RSS", with four
outcomes: ``acquired``, ``never_acquired``, ``undetermined``,
``not_yet_assessable``. This module answers the same shape of question about
a different, but structurally identical, situation -- "did RSS cover a URL
the canary is protecting after promotion" -- and reuses its evidence rules
rather than inventing new ones:

* RSS demonstrably carrying the URL is the only affirmative proof of
  coverage, at any later point, regardless of what the listing did.
* A URL leaving view is NOT loss. The listing pages posts off over time as
  ordinary churn, so "it stopped appearing" proves nothing by itself.
* Only a URL the canary *re-observed* within depth, in a later complete
  cycle, while RSS still had not carried it, is provable as a gap. That is
  the one state allowed to be called ``gap_proven``.
* Everything else that cannot be resolved -- it left view before a later
  canary could re-confirm it, one way or the other -- is
  ``coverage_unassessable``: still a fail-closed outcome for whoever consumes
  it, but a DIFFERENT claim from a proven gap. Conflating the two would put
  an unprovable statement into a demotion record, which is exactly the
  mistake the mirrored function's own history (see its docstring, "CORRECTED
  round 9") was written to stop repeating.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


def _within_depth_urls(rows: Sequence[Dict[str, Any]], depth: int) -> Set[str]:
    """The canonical URLs in ``rows`` whose ``page_index`` is within ``depth``.

    Centralised because "within depth" is the one filter every function in
    this module applies, and getting it wrong once (say, using ``<`` instead
    of ``<=``, or letting a missing ``page_index`` slip through as caught)
    would silently under- or over-count coverage everywhere at once.
    """
    urls: Set[str] = set()
    for row in rows:
        page_index = row.get("page_index")
        url = row.get("canonical_url")
        if url and isinstance(page_index, int) and page_index <= depth:
            urls.add(url)
    return urls


def select_sampled_cycles(
    cycles: List[Dict[str, Any]],
    interval_seconds: float,
    *,
    start: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Which of ``cycles`` a canary polling every ``interval_seconds`` would
    actually have sampled.

    ``cycles`` is ``[{"cycle_uuid": str, "at": datetime}, ...]`` sorted
    ascending -- the full, dense record of every cycle that really ran.

    THE RULE: the first cycle at or after each due time is the sample: never
    the closest, never an interpolated point between two real cycles. The due
    time starts at ``start`` (or the first cycle's own time, if ``start`` is
    not given) and advances by ``interval_seconds`` from the SAMPLED cycle's
    own timestamp each time -- not from the due time itself, which is what
    keeps a canary that is running behind schedule from compounding its own
    drift into a shorter effective interval.

    WHY THIS MATTERS: this function's whole purpose is to let ``replay()``
    measure what a slower canary cadence would really have caught, using
    ONLY cycles that genuinely happened. A gap in the cycle series (the
    canary was down, or the listing source failed for a stretch) must not
    become a phantom sample at the missed due time -- there is no cycle
    there, so there is nothing to select. The next real cycle, however late,
    is the sample; if no cycle exists at or after a due time, sampling stops
    with whatever was already collected.
    """
    if not cycles:
        return []
    interval = timedelta(seconds=interval_seconds)
    due = start if start is not None else cycles[0]["at"]
    sampled: List[Dict[str, Any]] = []
    index = 0
    total = len(cycles)
    while index < total:
        while index < total and cycles[index]["at"] < due:
            index += 1
        if index >= total:
            break
        chosen = cycles[index]
        sampled.append(chosen)
        due = chosen["at"] + interval
        index += 1
    return sampled


def replay(
    population: Iterable[str],
    sampled_membership: List[Dict[str, Any]],
    depth: int,
) -> Dict[str, Set[str]]:
    """What a canary running only the sampled cycles would have caught.

    ``population`` is the dense truth: every canonical URL seen within depth
    across ALL cycles of the window, sampled or not. ``sampled_membership``
    is membership rows belonging ONLY to the cycles ``select_sampled_cycles``
    picked. Returns ``{"caught": set, "missed": set}``.

    ``caught`` is the union of URLs actually present, within depth, in the
    sampled rows THEMSELVES -- nothing else. ``missed`` is every URL the
    population has that ``caught`` does not.

    THE TRAP THIS GUARDS AGAINST: membership is not continuous. A URL can be
    present at t0, absent at t1, present again at t2 -- an ordinary listing
    reshuffling it off and back onto an early page. A canary that only
    sampled at t1 did NOT see it, however tempting it is to reason "t1 falls
    between two sightings of the same URL, so a real crawl would probably
    have shown it too". That reasoning is interpolation, and it would make a
    cadence look safe when a genuinely faster-churning listing would, in
    fact, have paged the URL off-window before the canary's next real
    sample. This function has no notion of "between" at all: it only reads
    what the sampled rows actually recorded.
    """
    caught = _within_depth_urls(sampled_membership, depth)
    missed = set(population) - caught
    return {"caught": caught, "missed": missed}


def overlap(
    previous_rows: List[Dict[str, Any]],
    current_rows: List[Dict[str, Any]],
    depth: int,
) -> int:
    """How many URLs within depth were seen by both the previous and the
    current canary run.

    NEGATIVE SIGNAL ONLY. A high overlap says nothing about coverage -- it
    only means the two canaries largely agree on what is currently visible.
    A LOW or zero overlap is the useful direction: it means the listing may
    have paged an entire window's worth of posts off-view, unseen, between
    the two runs, which is a warning that the interval may be too slow for
    the source's current churn. Do not read a nonzero overlap as evidence
    that coverage is fine; it is silent on everything the listing already
    paged away before either canary ran.
    """
    previous_urls = _within_depth_urls(previous_rows, depth)
    current_urls = _within_depth_urls(current_rows, depth)
    return len(previous_urls & current_urls)


def churn(
    previous_rows: List[Dict[str, Any]],
    current_rows: List[Dict[str, Any]],
    depth: int,
) -> int:
    """How many URLs within depth are new in ``current_rows`` that were not
    in ``previous_rows`` -- new arrivals only, never re-counted repeats.

    This function has no ``posts_per_page``, so it cannot and does not decide
    what counts as "too much" churn -- it only counts. THE CALLER refuses to
    trust a canary cycle as a coverage witness when this count exceeds half
    the window's capacity, ``depth * posts_per_page / 2``: past that point,
    more than half a page's worth of turnover happened between samples, and
    a URL could plausibly have entered and been paged off again without
    either canary run ever seeing it. That threshold is a caller concern
    (it needs config this module is not given), but the count it is compared
    against is defined here so every caller compares the same thing.
    """
    previous_urls = _within_depth_urls(previous_rows, depth)
    current_urls = _within_depth_urls(current_rows, depth)
    return len(current_urls - previous_urls)


#: The four coverage states `classify_coverage` may return. Kept as a tuple,
#: not re-derived from anything, so a caller can validate against it without
#: importing implementation details.
COVERAGE_STATES = ("acquired", "pending", "gap_proven", "coverage_unassessable")


def classify_coverage(
    url: str,
    observations: List[Dict[str, Any]],
    rss_carried: Set[str],
    *,
    promotion_at: datetime,
    latest_complete_canary_at: Optional[datetime],
    depth: int,
) -> str:
    """One of ``COVERAGE_STATES`` for ``url``, mirroring the evidence rules in
    ``classify_miss_resolution`` (``backend/hdencode_shadow.py:531-603``).

    ``observations`` is this URL's own sightings by the canary --
    ``[{"at": datetime, "page_index": int}, ...]`` -- restricted to cycles
    the caller has already established were COMPLETE and source-valid. This
    module does not, and cannot, re-derive cycle validity: that requires
    knowing which sources and feeds exist, which is wiring-layer knowledge,
    not policy. ``rss_carried`` is the set of canonical URLs RSS
    demonstrably held (feed-carried or duplicate-with-listing) -- the caller
    is trusted to have already scoped it to the relevant window, since this
    flat set carries no timestamps of its own for this function to check.

    ``promotion_at`` marks where PROTECTED membership begins: a sighting
    before promotion is not something RSS was ever being asked to cover, so
    it does not start this URL's clock. ``latest_complete_canary_at`` is the
    newest complete, source-valid canary's own timestamp -- again handed in
    rather than derived, for the same reason as ``observations``.

    THE FOUR OUTCOMES, in the order they are decided:

    1. ``"acquired"`` -- RSS carried the URL. Checked first and unconditionally:
       once RSS has demonstrably held a URL, nothing the listing did before or
       after changes that, exactly as the mirrored resolver returns
       ``acquired`` the moment any later valid cycle shows ``feed_only`` or
       ``duplicate_urls``, regardless of an earlier ``listing_only`` sighting.
    2. ``"pending"`` -- no complete, source-valid canary has run since this
       URL's first protected sighting (or it was never protected at all), so
       there is nothing yet to judge it against. Matches
       ``not_yet_assessable``.
    3. ``"gap_proven"`` -- a LATER complete canary re-observed the URL within
       depth, and RSS still had not carried it. Re-observation is what makes
       this provable rather than a guess: the listing did not merely lose
       track of the URL, it demonstrably still had it while RSS did not.
       Matches ``never_acquired``.
    4. ``"coverage_unassessable"`` -- the URL left protected membership (no
       later complete canary saw it again within depth) without RSS ever
       carrying it, and without the re-observation that would prove a gap.
       This is NOT the same claim as ``gap_proven``: it fails closed for
       whoever consumes it, but it must never be reported as proof, because
       the listing paging a URL away is ordinary and unrelated to whether
       RSS would have caught it. Matches ``undetermined``.
    """
    protected_sightings = sorted(
        obs["at"]
        for obs in observations
        if obs.get("at") is not None
        and obs["at"] >= promotion_at
        and isinstance(obs.get("page_index"), int)
        and obs["page_index"] <= depth
    )

    if url in rss_carried:
        return "acquired"

    if not protected_sightings:
        # Never entered protected membership at all -- there is no first
        # sighting to measure a "later" canary against.
        return "pending"

    first_seen = protected_sightings[0]

    if latest_complete_canary_at is None or latest_complete_canary_at <= first_seen:
        return "pending"

    later_sightings = [at for at in protected_sightings if at > first_seen]
    if later_sightings:
        return "gap_proven"

    return "coverage_unassessable"


def systematic_gap(
    source_rows: List[Dict[str, Any]],
    rss_carried: Set[str],
    *,
    canaries: int,
) -> Optional[bool]:
    """Has RSS carried NONE of one source's membership over its last
    ``canaries`` complete canaries?

    ``source_rows`` is that source's membership rows -- ``canonical_url``
    plus ``cycle_uuid`` -- drawn from the window the caller believes spans
    the last ``canaries`` complete, source-valid canary cycles. This
    function has no ``depth`` parameter: it trusts the caller already
    dropped rows beyond depth, the same way it trusts cycle validity was
    already decided, because both require knowledge (config, schema) this
    pure module deliberately does not have.

    Returns:

    * ``True`` -- at least ``canaries`` distinct cycles are represented AND
      none of the URLs they carried appear in ``rss_carried``. This is the
      only outcome that should ever feed a systematic-gap demotion.
    * ``None`` -- there are FEWER than ``canaries`` distinct cycles in
      ``source_rows`` (including zero rows entirely). This is deliberately
      NOT ``False``: "not enough evidence to say" and "checked, and it's
      fine" must never look the same to a caller that is supposed to fail
      closed on the former. Returning ``False`` here would let thin evidence
      quietly pass as a clean bill of health.
    * ``False`` -- there is enough evidence, and RSS carried at least one of
      the source's URLs. Not a systematic gap, whatever else may be true
      about the source's coverage generally.
    """
    cycles_seen = {row["cycle_uuid"] for row in source_rows if row.get("cycle_uuid")}
    if len(cycles_seen) < canaries:
        return None

    urls = {row["canonical_url"] for row in source_rows if row.get("canonical_url")}
    if not urls:
        # Distinct cycle_uuids existed but carried no usable URLs -- rows
        # this malformed are not evidence either, so this is still thin
        # evidence, not a clean pass.
        return None

    return not any(u in rss_carried for u in urls)
