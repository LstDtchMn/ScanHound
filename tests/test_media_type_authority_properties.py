"""P1-P8: the properties the canonical media-type authority model must hold.

Companion to ``docs/design/2026-08-31-media-type-authority-model.md``. The model
does not exist yet, so this file is written against TODAY'S code and each
property is in one of three states:

    executable + passing   the property already holds; the test pins it so the
                           redesign cannot regress it. Each one is annotated
                           with the mutation that makes it fail, because a test
                           nobody has seen fail proves nothing.
    xfail(strict=True)     the property does NOT hold today. Strict is
                           deliberate: when the model lands and the property
                           starts holding, the suite goes RED until the marker
                           is removed. A skip would go quietly green.
    skip                   the property cannot be STATED without the canonical
                           state, and the reason says so.

NO HYPOTHESIS. The runtime image does not ship it (see
tests/test_queue_liveness_model.py, which records the same finding). These are
exhaustive deterministic enumerations instead, which is adequate here because
the reachable state space is 27 states -- "exhaustive" is literal, and a
reported failure is already the minimal reproducer.

INPUT-SPACE DISCIPLINE. ``_reachable_rows`` builds rows the way production
builds them and nothing else. Two constraints are load-bearing and the first
two runs of this analysis got them wrong, inflating a violation count from 0 to
220 on rows no scraper can produce:

  * ``category`` and the crawl ``type`` are COUPLED by the source table
    (scanner_service.py:760-778): 4k/remux => 'movie', tv => 'tv'.
  * ``season is not None`` IMPLIES ``is_tv`` (detail_scraper.py:285-287), so the
    detail observation is one bit, not two.
"""
from __future__ import annotations

import itertools

import pytest

from backend import release_grammar as grammar
from backend.api.routes import scanner as scanner_route
from backend.scanner_service import (
    MediaItem,
    cached_media_type,
    cached_type_evidence,
    cached_verdict_evidence,
    resolve_listing_media_type,
    resolve_rescan_media_type,
    stored_media_type,
)

# The real source table, reduced to the coupling that matters.
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


# ── P1: exact authority round trip ──────────────────────────────────────────

_ROUND_TRIP_CASES = (
    (grammar.MediaType.MOVIE, grammar.Authority.ROUTE, "listing-route"),
    (grammar.MediaType.TV, grammar.Authority.TITLE, "listing-title"),
    (grammar.MediaType.TV, grammar.Authority.DETAIL, "detail-filename"),
    (grammar.MediaType.MOVIE, grammar.Authority.IDENTITY, "imdb"),
)


def test_p1_todays_representation_loses_authority():
    """CHARACTERISATION of the defect, so it is pinned rather than argued.

    Persist a verdict the way every writer persists it, read it back the way
    ``cached_verdict_evidence`` reads it, and compare the authority. Two of the
    four levels do not survive. This test PASSES today and is expected to FAIL
    the moment the canonical model lands -- that failure is the signal to delete
    it, not to repair it.
    """
    observed = {}
    for media_type, authority, source in _ROUND_TRIP_CASES:
        verdict = grammar.resolve_media_type(
            [grammar.TypeEvidence(media_type, authority, source)])
        row = {"media_type": verdict.media_type.value,
               "media_type_provisional": verdict.provisional}
        observed[authority] = cached_verdict_evidence(row).authority

    # A boolean has two values, so four levels collapse onto two.
    assert observed[grammar.Authority.ROUTE] is grammar.Authority.ROUTE
    assert observed[grammar.Authority.DETAIL] is grammar.Authority.DETAIL
    assert observed[grammar.Authority.TITLE] is grammar.Authority.DETAIL, (
        "TITLE is expected to be UPGRADED to DETAIL by the boolean adapter")
    assert observed[grammar.Authority.IDENTITY] is grammar.Authority.DETAIL, (
        "IDENTITY is expected to be DOWNGRADED to DETAIL by the boolean adapter")


@pytest.mark.xfail(
    strict=True,
    reason="P1 fails BY CONSTRUCTION: the resolver has four authority levels "
           "and persistence stores one boolean, so TITLE and IDENTITY cannot "
           "survive a round trip. Remove this marker when MediaTypeState "
           "stores authority as the slot name (design doc S1).",
)
def test_p1_exact_authority_round_trip():
    """resolve -> persist -> reload must preserve the DECIDING AUTHORITY
    exactly: no upgrade, no downgrade, for every level the resolver defines."""
    for media_type, authority, source in _ROUND_TRIP_CASES:
        verdict = grammar.resolve_media_type(
            [grammar.TypeEvidence(media_type, authority, source)])
        row = {"media_type": verdict.media_type.value,
               "media_type_provisional": verdict.provisional}
        reloaded = cached_verdict_evidence(row)
        assert reloaded is not None
        assert reloaded.authority is authority, (
            f"{authority.name} evidence reloaded as {reloaded.authority.name}")


# ── P2: no self-authorization ───────────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="P2 fails today (design doc V6): resolve_listing_media_type never "
           "reads post_info['category_conflict'], so the listing crawl writes "
           "a verdict that ignores the conflict the SAME operation records, so "
           "the stored verdict disagrees with the effective conflict-aware "
           "cache interpretation of that row. (It is NOT true that every "
           "reader answers 'ambiguous' -- V7 is the counter-example: raw "
           "results.py serves the stored value.) Remove this marker when the "
           "writer/owner table (design doc S2) makes the conflict an "
           "observation the resolver sees -- OR SOONER, when the temporary V6 "
           "bridge on agent/v6-v7-conflict-bridge is in this branch's history, "
           "because that bridge already makes P2 hold and strict=True will "
           "turn this file RED on the rebase.",
)
def test_p2_no_self_authorization():
    """resolve -> persist -> reload -> resolve, with NO new observation, must
    leave the verdict and its authority unchanged.

    The conflict here is recorded BY THE CRAWL, in flight, before the verdict is
    written -- so nothing new is observed between the write and the read. Any
    difference is the representation disagreeing with itself.
    """
    post = {"type": "movie", "title": "Some Film 2019 1080p",
            "category": "4k", "category_conflict": True}
    details = {"is_tv": False}

    written = resolve_listing_media_type(post, details)
    row = _persist(written, {"title": post["title"], "category": "4k",
                             "season": None, "category_conflict": True,
                             "category_attested": True})

    reloaded_type, reloaded_provisional = cached_media_type(row)
    assert (reloaded_type, reloaded_provisional) == (
        written.media_type.value, written.provisional), (
        "the row was written as %r and reads back as %r with nothing observed "
        "in between" % (written.media_type.value, reloaded_type))


def test_p2b_a_row_can_never_clear_its_own_provisional_flag():
    """The half of P2 that DOES hold today, pinned so the redesign keeps it.

    This is the R4-94-2 invariant: a verdict resting only on the crawl route
    must not become a decided verdict because the row was read and rewritten.
    It is stated over the whole reachable space rather than one fixture,
    because the defect it guards was found on a row nobody had thought of.

    SHOWN TO FAIL (executed): deleting the ``legacy_row`` guard at
    scanner_service.py:2249 -- so a cached ``is_tv`` is admitted at DETAIL on a
    CURRENT-FORMAT row, where it is merely a shadow of that row's own verdict --
    makes ``{category:'tv', media_type:'tv', provisional:True, is_tv:True}``
    rescan to ``provisional=False`` with nothing observed. That is the system
    reading its own answer back in one authority level up.
    """
    promoted = []
    for row in _reachable_rows():
        if row.get("media_type_provisional") is not True:
            continue
        after = _rescan(row)
        if (after["media_type"] == row["media_type"]
                and after["media_type_provisional"] is False):
            promoted.append((row, after))
    assert not promoted, (
        "%d row(s) cleared their own provisional flag on a rescan that "
        "observed nothing new; first: %r" % (len(promoted), promoted[:1]))


# ── P3: idempotent rescan ───────────────────────────────────────────────────


def test_p3_rescan_is_idempotent_over_the_reachable_space():
    """state(n+1) == state(n) when a rescan observes the same page again.

    SHOWN TO FAIL (executed): widening R4-94-3's suppression rule at
    scanner_service.py:2296 to ``return True`` -- so a recorded conflict
    suppresses EVERY stored verdict rather than only a route-derived one --
    makes the legacy row ``{category:'tv', is_tv:True, conflict:True}`` resolve
    'tv' on the first rescan and 'ambiguous' on the second, because the first
    pass converts it to a current-format row whose ``is_tv`` is then read as a
    shadow and dropped. Two clicks, two different answers, one unchanged page.

    RECORDED HONESTLY: the obvious candidate mutation -- reverting R4-94-2 by
    deleting the ``legacy_row`` guard at scanner_service.py:2249 -- SURVIVES
    this property. R4-94-2's defect is a one-time promotion, not an
    oscillation, so idempotence cannot see it. That is what
    ``test_p2b_a_row_can_never_clear_its_own_provisional_flag`` is for, and the
    first version of this file asserted otherwise until the mutation was
    actually run.
    """
    violations = []
    for row in _reachable_rows():
        once = _rescan(row)
        twice = _rescan(once)
        if (once["media_type"], once["media_type_provisional"]) != (
                twice["media_type"], twice["media_type_provisional"]):
            violations.append((row, once, twice))
    assert not violations, (
        "%d reachable row(s) change verdict on a second identical rescan; "
        "first: %r" % (len(violations), violations[:1]))


@pytest.mark.skip(
    reason="P3's strong form compares the canonical EVIDENCE state, not just "
           "(media_type, provisional). That state does not exist yet: today a "
           "row stores a verdict and a boolean, so 'the observation set is "
           "unchanged' is unrepresentable. Unskip with MediaTypeState "
           "(design doc S1).",
)
def test_p3_rescan_is_idempotent_on_the_canonical_evidence_state():
    raise AssertionError("unreachable while skipped")


# ── P4: commutativity of independent observations ───────────────────────────


def test_p4_independent_observations_commute():
    """The order evidence is collected in must not change the verdict.

    SHOWN TO FAIL: replacing ``max(e.authority ...)`` in resolve_media_type with
    ``evidence[0].authority`` (a first-wins rule, which is what the pre-grammar
    code did) makes these six permutations produce three different answers.
    """
    evidence = [
        grammar.TypeEvidence(grammar.MediaType.MOVIE, grammar.Authority.ROUTE, "route"),
        grammar.TypeEvidence(grammar.MediaType.TV, grammar.Authority.TITLE, "title"),
        grammar.TypeEvidence(grammar.MediaType.TV, grammar.Authority.DETAIL, "detail"),
    ]
    outcomes = {
        (v.media_type, v.provisional, v.because)
        for order in itertools.permutations(evidence)
        for v in [grammar.resolve_media_type(list(order))]
    }
    assert len(outcomes) == 1, (
        "evidence order changed the verdict: %r" % (outcomes,))

    # And a genuine disagreement at the top level must be order-independent too.
    clash = [
        grammar.TypeEvidence(grammar.MediaType.TV, grammar.Authority.DETAIL, "a"),
        grammar.TypeEvidence(grammar.MediaType.MOVIE, grammar.Authority.DETAIL, "b"),
    ]
    clash_outcomes = {
        (v.media_type, v.provisional, v.because)
        for order in itertools.permutations(clash)
        for v in [grammar.resolve_media_type(list(order))]
    }
    assert len(clash_outcomes) == 1
    assert clash_outcomes.pop()[0] is grammar.MediaType.AMBIGUOUS


# ── P5: writer entitlement / noninterference ────────────────────────────────


def test_p5_a_rescan_does_not_touch_the_route_facts_it_cannot_observe():
    """A detail rescan observes ONE detail page. It observes nothing about which
    listing carried the release, so category, the recorded conflict and the
    tri-state attestation must survive it byte-for-byte.

    SHOWN TO FAIL: this is exactly what R4-94-3 (C4) fixed. Reverting
    ``rescan_classification``'s attestation line to ``bool(cached.get(...))``
    turns the ABSENT case into False, which permanently disqualifies the row
    from ``attest_scan_categories`` -- and the third case below catches it.
    """
    import json

    for stored_attested, expected in ((True, True), (False, False)):
        existing = {"data": json.dumps({
            "category": "tv", "category_conflict": True,
            "category_attested": stored_attested})}
        category, conflict, attested = scanner_route.rescan_classification(existing)
        assert category == "tv"
        assert conflict is True
        assert attested is expected

    # THE THIRD STATE. Absence is 'never checked by a conflict-aware crawl',
    # which attest_scan_categories keys off and get_scan_category fails closed
    # on. bool() collapses it into False and destroys it.
    existing = {"data": json.dumps({"category": "tv"})}
    _, _, attested = scanner_route.rescan_classification(existing)
    assert attested is None, "absent attestation must stay UNKNOWN, not False"


# ── P6: unknown is representable ────────────────────────────────────────────


def test_p6_unknown_is_not_false_and_not_clean():
    """UNKNOWN must be distinguishable from False, from clean, from movie and
    from unconflicted -- at every boundary the value crosses.

    SHOWN TO FAIL: deleting the ``category_attested is None`` branch in
    ``_media_item_to_dict`` (backend/api/routes/scanner.py:386-390) makes the
    serializer emit ``category_attested: None``, and the KEY becoming present is
    the destructive act -- attest_scan_categories skips any row where the key
    exists, so the row can never be attested again.
    """
    unknown = MediaItem(id="", title="x", year=2020, category_attested=None)
    serialized = scanner_route._media_item_to_dict(unknown)
    assert "category_attested" not in serialized, (
        "an UNKNOWN attestation must not be serialized at all; emitting the "
        "key withdraws the row from attest_scan_categories")

    for value in (True, False):
        item = MediaItem(id="", title="x", year=2020, category_attested=value)
        assert scanner_route._media_item_to_dict(item)["category_attested"] is value

    # The same three-state question one field over: a row that RECORDED
    # 'ambiguous' decided something, and is not the same as a row with no
    # verdict at all.
    assert stored_media_type({"media_type": "ambiguous"}) == "ambiguous"
    assert stored_media_type({}) == ""
    assert stored_media_type({"media_type": "movie"}) == "movie"

    # And an absent verdict must not read as 'not TV'.
    assert cached_media_type({"category": "tv"})[0] == "tv"
    assert cached_media_type({})[0] == "ambiguous"


# ── P7: conflict is evidence, not a veto bit ────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="P7 fails today: `category_conflict` is a bare bool, so the "
           "conflicting CLAIMS are not stored anywhere and cannot be "
           "recovered from a row. Remove this marker when RouteEvidence.claims "
           "carries them (design doc S1).",
)
def test_p7_a_conflict_preserves_the_claims_it_summarises():
    """A conflict must be derivable FROM the claims, not stored instead of them.

    A summary bit goes stale the moment the claims it summarises move -- and
    measured, the inverse also happens here: ``mark_scan_category_conflict``
    moves the bit and nothing re-derives the verdict, so the API blob and the
    matcher answer differently for the same row (design doc V7).
    """
    row = {"title": "Some Film 2019 1080p", "category": "4k",
           "category_conflict": True, "category_attested": True,
           "season": None, "is_tv": False,
           "media_type": "movie", "media_type_provisional": True}

    claims = row.get("route_claims") or row.get("category_claims")
    assert claims, (
        "the row records THAT two listings disagreed but not WHICH types they "
        "claimed, so 'conflict' cannot be re-derived and cannot be revised "
        "when a third listing is seen")
    assert set(claims) == {"tv", "movie"}
    assert (len(set(claims)) > 1) is bool(row["category_conflict"])


# ── P8: legacy conversion is one-way and idempotent ─────────────────────────


def test_p8_reading_a_legacy_row_never_writes_it_back():
    """Conversion is a READ. The three cache readers must be pure.

    SHOWN TO FAIL: any of them growing a ``cached['media_type'] = ...``
    write-back -- the obvious "cache the reconstruction" optimisation -- fails
    this immediately, and that write-back is precisely how a legacy row would
    acquire an authority nobody observed.
    """
    for row in _reachable_rows():
        before = dict(row)
        cached_media_type(row)
        cached_type_evidence(row)
        cached_verdict_evidence(row)
        assert row == before, "reading mutated the row: %r -> %r" % (before, row)


@pytest.mark.xfail(
    strict=True,
    reason="P8's no-authority-gain half fails today: persisting a verdict and "
           "reading it back re-admits it one authority level above the "
           "evidence that produced it (P1), so repeated read/write cycles can "
           "only gain authority, never preserve it. Remove this marker when "
           "the legacy adapter clamps legacy provenance to provisional "
           "(design doc S3, rule L3).",
)
def test_p8_legacy_conversion_never_increases_authority():
    """Reading a pre-schema row must never produce MORE authority than the row
    itself recorded, however many times it is read."""
    for row in _reachable_rows():
        recorded = row.get("media_type_provisional")
        evidence = cached_verdict_evidence(row)
        if evidence is None:
            continue
        if recorded:
            assert evidence.authority is grammar.Authority.ROUTE
        else:
            # A non-provisional verdict is only known to have had
            # TITLE-or-better behind it. Re-admitting it at DETAIL claims more
            # than the row recorded.
            assert evidence.authority is grammar.Authority.TITLE, (
                "a stored non-provisional verdict re-enters at %s, which is "
                "more authority than 'not provisional' records"
                % evidence.authority.name)
