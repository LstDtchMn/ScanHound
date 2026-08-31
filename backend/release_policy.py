"""Release-shape policy — ONE definition, used by every discovery path.

This module exists because of a specific, already-paid-for failure. ScanHound
had two URL canonicalisers written independently for the listing and RSS paths:
one stripped the trailing slash, the other appended it. Every join between the
two stores silently returned zero rows, which is how a healthy pipeline
(99 of 100 releases acquired, median 1.02 h) came to be reported as "0 of 100
never acquired".

The full-disc exclusion has exactly the same shape — a rule that must hold
identically on the listing path and the RSS path — so its predicate lives here
once rather than being written twice and drifting apart. If the rule changes,
it changes for both paths in the same edit, by construction.
"""
from __future__ import annotations

import re
from typing import Optional

#: Anchored, case-insensitive, and tolerant of whitespace inside the brackets.
#: Deliberately NOT applied to the URL slug: the slug for "[BD]Sorority..." is
#: "bdsorority...", so a substring test there would also match a genuine release
#: whose title merely begins with those letters.
_FULL_DISC_TITLE_RE = re.compile(r"^\s*\[\s*BD\s*\]", re.IGNORECASE)

#: Policy reasons recorded alongside an exclusion. The path is part of the
#: reason so a later audit can tell which discovery route caught it — the two
#: must agree on WHAT is excluded, not pretend to be the same writer.
REASON_LISTING_FULL_DISC = "listing_policy_excluded_full_disc"
REASON_RSS_FULL_DISC = "rss_policy_excluded_full_disc"


def is_full_disc_title(title: Optional[str]) -> bool:
    """True when a release title marks a full-disc rip.

    HDEncode publishes two shapes. A normal encode lists a ``Filename:`` field —
    one video file. A full-disc rip does not, because a whole disc is not a
    single file. ``scrape_details`` requires that filename and returns None
    without logging when it is absent, so full-disc releases were never turned
    into items, never cached, counted as new again next cycle, and re-fetched
    forever.

    Matches only the bracketed ``[BD]`` prefix. ``BD Movie Title`` and
    ``Some BDRip Movie`` are ordinary releases and must not match.
    """
    if not title:
        return False
    return bool(_FULL_DISC_TITLE_RE.match(title))
