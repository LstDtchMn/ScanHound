"""The reachable ``background_scan_cache`` row space, as ONE shared enumeration.

PROVENANCE. This is the enumeration the design lane built and used to find V6
and V7 -- ``_listing_rows`` / ``_legacy_rows`` / ``_reachable_rows`` from
``tests/test_media_type_authority_properties.py`` on ``design/media-type-authority``
(commit 1e50214). It is lifted here UNCHANGED, as a module, so the V6/V7 bridge
regression measures the same rows the design package measured rather than a
second enumeration that could quietly differ. When the design lane lands on top
of this branch, its private copies should be deleted in favour of importing
from here -- two copies of an input space is exactly how two lanes come to
disagree about a violation count.

INPUT-SPACE DISCIPLINE, quoted from the original because it is load-bearing and
the first two runs of the design analysis got it wrong, inflating a violation
count from 0 to 220 on rows no scraper can produce:

  * ``category`` and the crawl ``type`` are COUPLED by the source table
    (scanner_service.py:760-778): 4k/remux => 'movie', tv => 'tv'.
  * ``season is not None`` IMPLIES ``is_tv`` (detail_scraper.py:285-287), so the
    detail observation is one bit, not two.

Nothing here restates production logic: every verdict comes from importing and
running ``resolve_listing_media_type`` / ``resolve_rescan_media_type``.
"""
from __future__ import annotations

import itertools

from backend import release_grammar as grammar
from backend.scanner_service import (
    resolve_listing_media_type,
    resolve_rescan_media_type,
)

_CATEGORY_TYPE = {"4k": "movie", "remux": "movie", "tv": "tv"}

# One title that says nothing, one that says TV without a season token, one that
# carries a season token. Anything more is more rows saying the same thing.
_TITLES = (
    "Some Film 2019 1080p",
    "Great Show Complete Series",
    "Great Show S03 1080p",
)


def _persist(verdict, row):
    """Exactly what every writer persists: the verdict, the provisional bit,
    and is_tv derived from the verdict (web_item_facts / _process_posts'
    worker / the rescan route all use this same rule)."""
    out = dict(row)
    out["media_type"] = verdict.media_type.value
    out["media_type_provisional"] = verdict.provisional
    out["is_tv"] = verdict.media_type is grammar.MediaType.TV
    return out


def _listing_rows():
    """Every cached row the LISTING crawl -- the only ex-nihilo writer -- can
    produce, using the production composition rather than a restatement."""
    rows = []
    for category, title, detail_is_tv, conflict in itertools.product(
        _CATEGORY_TYPE, _TITLES, (False, True), (False, True)
    ):
        post = {
            "type": _CATEGORY_TYPE[category],
            "title": title,
            "category": category,
            "category_conflict": conflict,
        }
        details = {"is_tv": detail_is_tv}
        verdict = resolve_listing_media_type(post, details)
        rows.append(
            _persist(
                verdict,
                {
                    "title": title,
                    "category": category,
                    "season": 3 if detail_is_tv else None,
                    "category_conflict": conflict,
                    "category_attested": True,
                },
            )
        )
    return rows


def _detail_bit(row):
    """A rescan re-fetches the SAME page, so the detail observation it makes is
    the one already recorded. Feeding an arbitrary bit here would test a page
    that does not exist."""
    return row.get("season") is not None


def _rescan(row):
    verdict = resolve_rescan_media_type(row, {"is_tv": _detail_bit(row)})
    return _persist(verdict, row)


def _legacy_rows():
    """Rows in the PRE-#93 shape, which is what the deployed corpus is made of:
    no ``media_type``, no ``media_type_provisional``, and ``is_tv`` written by
    the old flat OR (``details['is_tv'] or post_info['type'] == 'tv'``).

    These are not optional. Excluding them was the first version of this file's
    mistake: with only current-format rows, the mutation that reverts R4-94-2
    SURVIVES the idempotence property, because on a current-format row ``is_tv``
    is a shadow of the verdict and re-admitting it changes nothing. A legacy
    ``is_tv`` is independent of the verdict, and that is where the feedback loop
    R4-94-2 closed actually shows up.
    """
    rows = []
    for category, title, detail_is_tv, conflict in itertools.product(
        _CATEGORY_TYPE, _TITLES, (False, True), (False, True)
    ):
        rows.append({
            "title": title,
            "category": category,
            "season": 3 if detail_is_tv else None,
            "category_conflict": conflict,
            "category_attested": True,
            "is_tv": detail_is_tv or _CATEGORY_TYPE[category] == "tv",
        })
    return rows

def _reachable_rows():
    """Closure of the listing rows AND the legacy corpus shape, under rescan and
    under the out-of-band conflict mark (database.mark_scan_category_conflict,
    which sets the bit in place on a row the crawl SKIPS as already cached)."""
    seen = {}

    def key(row):
        return (row["category"], row["title"], row["season"],
                row["category_conflict"], row.get("media_type"),
                row.get("media_type_provisional"), row["is_tv"])

    frontier = []
    for row in _listing_rows() + _legacy_rows():
        if key(row) not in seen:
            seen[key(row)] = row
            frontier.append(row)
    while frontier:
        nxt = []
        for row in frontier:
            marked = dict(row, category_conflict=True)
            for candidate in (_rescan(row), marked):
                if key(candidate) not in seen:
                    seen[key(candidate)] = candidate
                    nxt.append(candidate)
        frontier = nxt
    return list(seen.values())
