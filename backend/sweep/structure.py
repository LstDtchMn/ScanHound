"""Listing-page structural integrity — is this page telling us the truth?

Design rev 2.1 §1: *an unexpectedly empty or structurally changed page is a
PARSER FAILURE, never "no unseen identities".*

This is the detection half of that rule (completion.py holds the decision half).
It exists because ScanHound has already been bitten by the shape twice:

  * full-disc releases: ``scrape_details`` required a ``Filename:`` field,
    returned None when it was absent, and logged nothing. 128 posts became 2
    items, every cycle, silently.
  * the listing selectors below fall through three tiers. A site markup change
    that breaks tier 1 keeps working on tier 3 — until tier 3 breaks too, at
    which point the failure arrives with no warning history at all.

So a page that parsed but yielded nothing is never reported as an empty
category unless we can positively distinguish the two, and a fall-through to a
fallback selector is surfaced as DEGRADED while it still works.

FAIL-CLOSED: when the evidence cannot distinguish "genuinely empty" from "our
selectors stopped matching", the verdict is failure. An undercount that reads as
success is the exact failure this module exists to prevent; a false alarm merely
costs a re-fetch.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

#: Below this, a "page" is a truncated response or an error body, not a listing.
MIN_PLAUSIBLE_BODY_BYTES = 512

#: A page yielding less than this fraction of the source's established volume is
#: treated as structurally suspect rather than as a quiet day.
VOLUME_ANOMALY_FRACTION = 0.5


class PageStructure(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"              # a fallback selector matched — drift warning
    STRUCTURE_LOST = "structure_lost"  # nothing matched where posts were expected
    VOLUME_ANOMALY = "volume_anomaly"  # far fewer posts than this source ever yields
    UNUSABLE = "unusable"              # not a listing page at all
    EMPTY_UNVERIFIABLE = "empty_unverifiable"  # zero posts, no history to judge by


#: Everything except OK and DEGRADED blocks completion. DEGRADED still parsed a
#: full page, so it is a warning to act on, not a reason to discard the data.
FAILING = {
    PageStructure.STRUCTURE_LOST,
    PageStructure.VOLUME_ANOMALY,
    PageStructure.UNUSABLE,
    PageStructure.EMPTY_UNVERIFIABLE,
}


@dataclass(frozen=True)
class StructureVerdict:
    structure: PageStructure
    detail: str
    selector_tier: Optional[int] = None

    @property
    def is_failure(self) -> bool:
        return self.structure in FAILING

    @property
    def is_warning(self) -> bool:
        return self.structure is PageStructure.DEGRADED


def classify_page_structure(
    *,
    page_index: int,
    posts_found: int,
    selector_tier: Optional[int],
    body_bytes: int,
    expected_typical: Optional[float] = None,
    volume_fraction: float = VOLUME_ANOMALY_FRACTION,
) -> StructureVerdict:
    """Judge one fetched listing page.

    `selector_tier` is which fallback level matched: 0 = the primary selector,
    higher = a fallback, None = nothing matched.

    `expected_typical` is this source's established posts-per-page, from its own
    observation history. None means no history — which is why a zero-post page 1
    is then EMPTY_UNVERIFIABLE rather than a confident verdict either way.
    """
    if body_bytes < MIN_PLAUSIBLE_BODY_BYTES:
        return StructureVerdict(
            PageStructure.UNUSABLE,
            f"response body is {body_bytes} bytes — a truncated or error page, "
            f"not a listing",
            selector_tier,
        )

    if posts_found == 0:
        # Page 2+ is unambiguous: pagination would not have taken us here if the
        # source had run out, and completion.py already refuses to read this as
        # "nothing new".
        if page_index > 1:
            return StructureVerdict(
                PageStructure.STRUCTURE_LOST,
                f"page {page_index} parsed but yielded no posts — pagination led "
                f"here, so posts were expected",
                selector_tier,
            )
        if expected_typical:
            return StructureVerdict(
                PageStructure.STRUCTURE_LOST,
                f"page 1 yielded no posts, but this source typically yields "
                f"{expected_typical:.0f} — the selectors have stopped matching",
                selector_tier,
            )
        # No history to judge by. Fail closed: we cannot tell an empty category
        # from a broken selector, and guessing "empty" is how an undercount
        # becomes invisible.
        return StructureVerdict(
            PageStructure.EMPTY_UNVERIFIABLE,
            "page 1 yielded no posts and this source has no volume history — "
            "cannot distinguish an empty source from a broken selector",
            selector_tier,
        )

    if expected_typical and posts_found < expected_typical * volume_fraction:
        return StructureVerdict(
            PageStructure.VOLUME_ANOMALY,
            f"page {page_index} yielded {posts_found} posts against a typical "
            f"{expected_typical:.0f} — below the {volume_fraction:.0%} floor",
            selector_tier,
        )

    if selector_tier:
        # Still working, but on a fallback. This is the warning that would have
        # preceded a total selector failure with weeks of notice.
        return StructureVerdict(
            PageStructure.DEGRADED,
            f"primary selector matched nothing; fallback tier {selector_tier} "
            f"yielded {posts_found} posts — site markup has drifted",
            selector_tier,
        )

    return StructureVerdict(
        PageStructure.OK,
        f"{posts_found} posts via the primary selector",
        selector_tier,
    )


def select_with_tier(soup, selectors):
    """Run an ordered selector list, reporting WHICH tier matched.

    Returns ``(elements, tier)`` with tier 0 for the primary selector and None
    when nothing matched. The tier is the whole point: an ``a or b or c`` chain
    hides the fact that it fell through, so drift stays invisible until the last
    fallback breaks too.
    """
    for tier, selector in enumerate(selectors):
        found = soup.select(selector)
        if found:
            return found, tier
    return [], None
