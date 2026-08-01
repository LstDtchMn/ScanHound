"""Pure decision logic for the hybrid listing sweep.

Kept free of I/O and database access on purpose: these are the rules that took
three adversarial review rounds to settle, and they must be testable without a
crawler, a network, or a schema.

Two rules encoded here, both of which earlier designs got wrong:

1. COMPLETION IS CONJUNCTIVE. Rev 1 listed stop signals without saying whether
   one or all were required. If a sweep stops when ANY single signal fires it can
   silently under-cover — the same shape as the full-disc bug, where something
   ran cleanly and found nothing.

2. AN UNEXPECTED PAGE IS A FAILURE, NEVER "NOTHING NEW". A page that does not
   parse, or that yields no recognisable posts where posts were expected, means
   the sweep is INCOMPLETE. Treating it as "no unseen identities" is exactly how
   a template change becomes silent data loss.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# HDEncode listing blocks carry a RELATIVE post time ("Posted 3 hours ago"),
# never an absolute date — verified against /quality/2160p/?tag=movies on
# 2026-08-01. Granularity observed: minutes, hours, days.
_RELATIVE = re.compile(
    r"posted\s+(?:about\s+)?(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 604800, "month": 2592000, "year": 31536000,
}


@dataclass(frozen=True)
class PostedTime:
    """A listing timestamp, kept honest about its own imprecision.

    `observed_at` + `offset_seconds` give the derived absolute time, but the raw
    string is retained so nothing downstream mistakes "2 days ago" for a precise
    datetime. Granularity is the width of the band the true value sits in: a
    "days"-granular reading could be anywhere in a 24-hour window, which is why
    the sweep overlap must exceed the coarsest granularity it will act on.
    """
    observed_at: dt.datetime
    offset_seconds: int
    raw: str
    granularity_seconds: int

    @property
    def absolute(self) -> dt.datetime:
        return self.observed_at - dt.timedelta(seconds=self.offset_seconds)

    @property
    def earliest_possible(self) -> dt.datetime:
        """The oldest the post could actually be, given rounding."""
        return self.absolute - dt.timedelta(seconds=self.granularity_seconds)


def parse_posted(text: str, observed_at: dt.datetime) -> Optional[PostedTime]:
    """Parse a listing block's relative post time. None when absent/unparseable.

    Returning None is a REAL answer meaning "this block carried no time we
    understand" — callers must treat that as a parser concern, not as a very old
    post, because defaulting an unknown time to "old" would stop a sweep early.
    """
    if not text:
        return None
    match = _RELATIVE.search(text)
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2).lower()
    per = _UNIT_SECONDS[unit]
    return PostedTime(
        observed_at=observed_at,
        offset_seconds=count * per,
        raw=match.group(0),
        granularity_seconds=per,
    )


@dataclass
class PageOutcome:
    """What one fetched listing page yielded."""
    page_index: int
    parsed_ok: bool
    posts_found: int
    new_identities: int          # not already in this source's ledger
    oldest_posted: Optional[PostedTime] = None
    error: Optional[str] = None


@dataclass
class CompletionVerdict:
    complete: bool
    reasons: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return not self.complete


def evaluate_completion(
    pages: Iterable[PageOutcome],
    *,
    stop_target: dt.datetime,
    all_persisted: bool,
    page_cap: int,
) -> CompletionVerdict:
    """Decide whether an ordinary sweep may be called complete.

    ALL of these must hold. Any one missing leaves the sweep INCOMPLETE, which
    means the watermark does not advance and the prior coverage stands.
    """
    pages = list(pages)
    reasons: list[str] = []
    blocking: list[str] = []

    if not pages:
        return CompletionVerdict(False, blocking=["no pages fetched"])

    # A parser or transport failure anywhere poisons the whole attempt. We cannot
    # know what an unreadable page contained, so we cannot claim to have covered
    # the interval it belongs to.
    broken = [p for p in pages if not p.parsed_ok]
    if broken:
        blocking.append(
            f"{len(broken)} page(s) failed to parse: "
            + ", ".join(f"p{p.page_index}({p.error or 'unknown'})" for p in broken[:3])
        )

    # A page that parsed but yielded zero posts where posts were expected is a
    # STRUCTURAL failure, not an empty category. Only page 1 may legitimately be
    # empty (a genuinely empty source).
    empty_mid = [p for p in pages if p.parsed_ok and p.posts_found == 0 and p.page_index > 1]
    if empty_mid:
        blocking.append(
            "structurally empty page(s) beyond page 1: "
            + ", ".join(f"p{p.page_index}" for p in empty_mid[:3])
        )

    # A: the timestamp boundary. Uses earliest_possible so a coarse reading is
    # treated as the OLDEST it might be — refusing to claim we crossed a
    # boundary we might not have.
    timed = [p.oldest_posted for p in pages if p.oldest_posted is not None]
    if not timed:
        blocking.append("no page yielded a parseable post time")
    else:
        oldest = min(timed, key=lambda t: t.earliest_possible)
        if oldest.earliest_possible <= stop_target:
            reasons.append(
                f"timestamp target crossed (oldest {oldest.raw!r} "
                f"→ ≤ {oldest.earliest_possible.isoformat(timespec='seconds')})"
            )
        else:
            blocking.append(
                f"timestamp target NOT crossed (oldest {oldest.raw!r} still after "
                f"{stop_target.isoformat(timespec='seconds')})"
            )

    # B + C: a complete page, past page 1, containing nothing this SOURCE has not
    # already seen. Page 1 alone cannot satisfy this even on a quiet source,
    # because a first page of entirely-known posts is also what a stalled crawler
    # produces.
    clean = [p for p in pages if p.parsed_ok and p.posts_found > 0 and p.new_identities == 0]
    if clean:
        reasons.append(f"clean page with no source-new identities (p{clean[0].page_index})")
    else:
        blocking.append("no complete page free of source-new identities")

    if not all_persisted:
        blocking.append("not all discoveries durably persisted")

    if len(pages) >= page_cap and blocking:
        blocking.append(f"page cap {page_cap} reached before completion")

    return CompletionVerdict(complete=not blocking, reasons=reasons, blocking=blocking)
