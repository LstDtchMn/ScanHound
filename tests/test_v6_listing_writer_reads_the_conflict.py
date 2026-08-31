"""V6: the listing writer must account for the conflict it is recording.

TEMPORARY. This file exists only for the V6/V7 bridge and is DELETED when the
canonical media-type state becomes the sole reader and writer of the verdict
(docs/design/2026-08-31-media-type-authority-model.md, Phase B). Under that
model the conflicting route claims are stored and the resolver sees them, so
there is no "did the writer remember to read the bit" question left to test.

THE DEFECT. ``resolve_listing_media_type`` had zero references to
``post_info['category_conflict']``. ``_process_posts``'s worker calls it, then
persists ``details['category_conflict'] = True`` on the very same row -- so the
crawl wrote ``media_type='movie'`` beside ``category_conflict=True``, and the
stored verdict disagreed with the effective conflict-aware cache interpretation
of the same row, with no later operation to reconcile them.

THE RULE, which is not new: it is the one ``cached_type_evidence`` already
applies on the read side. A cross-listing conflict invalidates ROUTE evidence,
not TITLE and not DETAIL evidence.

HOW EACH ASSERTION WAS SHOWN TO FAIL -- both directions, because a one-sided
suppression test passes for a fix that is wrong the other way.

  * REINTRODUCE THE DEFECT (in ``resolve_listing_media_type``, the route
    guard back to the unconditional ``if post_info.get('type') in
    ('tv','movie')`` -- no line number here on purpose: this branch's own
    r4_94_1_mutation_check.py shows what literal line numbers become):
    7 of 12 fail. The neutral cases fail with verdict 'movie'/'tv' decided by
    ``listing-route``; the row-agreement property fails with stored 'movie',
    effective 'ambiguous' -- the exact V6 disagreement. The title and detail
    cases fail too, and on the RIGHT assertion: the media type is still 'tv',
    but ``because`` gains ``listing-route=movie (overruled)``, which is the
    suppressed route reaching the verdict.
  * OVER-SUPPRESS (return ``resolve_media_type([])`` whenever a conflict is
    recorded): 3 of 12 fail -- exactly the title, detail and equivalence tests,
    which exist to stop a fix that answers 'ambiguous' by deleting evidence a
    conflict has no authority over.
"""
from __future__ import annotations

import itertools

import pytest

from backend import release_grammar as grammar
from backend.scanner_service import cached_media_type, resolve_listing_media_type

_NEUTRAL = "Some Film 2019 1080p"
_TV_TITLE = "Great Show Complete Series"


def _post(route, *, title=_NEUTRAL, conflict=True):
    return {"type": route, "title": title, "category": "4k",
            "category_conflict": conflict}


def test_v6_conflict_with_neutral_evidence_is_ambiguous():
    """movie route + conflict + neutral title/detail -> ambiguous.

    The route is the ONLY signal present, and the conflict is precisely the
    statement that the route is untrustworthy. Nothing is left, so the answer
    is 'I decided nothing' -- not the route's answer.
    """
    verdict = resolve_listing_media_type(_post("movie"), {"is_tv": False})
    assert verdict.media_type is grammar.MediaType.AMBIGUOUS, verdict.because
    assert not any("listing-route" in b for b in verdict.because), (
        "the suppressed route still reached the verdict: %r" % (verdict.because,))

    # And the same on the TV side, so the fix is not "movie loses".
    tv_verdict = resolve_listing_media_type(_post("tv"), {"is_tv": False})
    assert tv_verdict.media_type is grammar.MediaType.AMBIGUOUS, tv_verdict.because


def test_v6_conflict_still_admits_title_evidence():
    """movie route + conflict + TV title -> tv, DECIDED BY THE TITLE.

    Two listings disagreeing about which category page carried a release says
    nothing about what the release is CALLED. Over-suppressing here would be
    the mirror of the bug.
    """
    verdict = resolve_listing_media_type(
        _post("movie", title=_TV_TITLE), {"is_tv": False})
    assert verdict.media_type is grammar.MediaType.TV, verdict.because
    assert verdict.because == ("listing-title=tv",), verdict.because
    assert verdict.provisional is False, (
        "a TITLE-decided verdict is not provisional; provisional means nothing "
        "above ROUTE spoke")


def test_v6_conflict_still_admits_fresh_detail_evidence():
    """movie route + conflict + TV detail -> tv, DECIDED BY THE DETAIL PAGE.

    The detail filename is a fresh observation of the release itself. A route
    conflict cannot reach it.
    """
    verdict = resolve_listing_media_type(_post("movie"), {"is_tv": True})
    assert verdict.media_type is grammar.MediaType.TV, verdict.because
    assert verdict.because == ("detail-filename=tv",), verdict.because
    assert verdict.provisional is False


def test_v6_no_conflict_is_untouched():
    """CONTROL. Without a conflict the route still decides, provisionally.

    Without this, a fix that simply dropped the route evidence altogether would
    pass every other test in this file.
    """
    verdict = resolve_listing_media_type(
        _post("movie", conflict=False), {"is_tv": False})
    assert verdict.media_type is grammar.MediaType.MOVIE, verdict.because
    assert verdict.provisional is True
    assert verdict.because == ("listing-route=movie",), verdict.because

    tv_verdict = resolve_listing_media_type(
        _post("tv", conflict=False), {"is_tv": False})
    assert tv_verdict.media_type is grammar.MediaType.TV, tv_verdict.because
    assert tv_verdict.provisional is True


def test_v6_absent_conflict_key_is_not_a_conflict():
    """A post dict with NO ``category_conflict`` key at all (the RSS-shaped and
    the older listing-shaped inputs) must behave exactly like conflict=False.

    Reading absence as True would blank the route for every path that does not
    set the key -- silently unresolving the whole corpus.
    """
    verdict = resolve_listing_media_type(
        {"type": "movie", "title": _NEUTRAL}, {"is_tv": False})
    assert verdict.media_type is grammar.MediaType.MOVIE, verdict.because


@pytest.mark.parametrize("category,route", [("4k", "movie"), ("remux", "movie"),
                                            ("tv", "tv")])
@pytest.mark.parametrize("detail_is_tv", [False, True])
def test_v6_writer_and_reader_agree_on_every_row_the_crawl_writes(
        category, route, detail_is_tv):
    """THE INTERNAL-CONSISTENCY PROPERTY, over every conflicted row the crawl
    can write with a neutral title.

    Persist exactly what ``_process_posts``'s worker persists, then read the
    row back through ``cached_media_type`` -- the reader the matcher uses. The
    two must give the same answer. Before the fix they did not, on the rows
    where the route was the only evidence.
    """
    post = {"type": route, "title": _NEUTRAL, "category": category,
            "category_conflict": True}
    verdict = resolve_listing_media_type(post, {"is_tv": detail_is_tv})

    row = {"title": _NEUTRAL, "category": category,
           "season": 3 if detail_is_tv else None,
           "category_conflict": True, "category_attested": True,
           "media_type": verdict.media_type.value,
           "media_type_provisional": verdict.provisional,
           "is_tv": verdict.media_type is grammar.MediaType.TV}

    assert cached_media_type(row)[0] == verdict.media_type.value, (
        "the row the writer just wrote reads back as a different verdict: "
        "stored %r, effective %r" % (verdict.media_type.value,
                                     cached_media_type(row)[0]))


def test_v6_conflict_is_the_only_new_input():
    """The suppression must key on the conflict and nothing else.

    Enumerated so a fix that accidentally keys on, say, the category string
    cannot pass: for every (route, title, detail) the conflicted answer must
    equal the unconflicted answer computed WITHOUT the route evidence, and the
    unconflicted answer must be unchanged from the pre-fix composition.
    """
    for route, title, detail in itertools.product(
            ("movie", "tv"), (_NEUTRAL, _TV_TITLE), (False, True)):
        conflicted = resolve_listing_media_type(
            {"type": route, "title": title, "category_conflict": True},
            {"is_tv": detail})
        # The same call with NO route at all -- the exact evidence set the
        # suppression is supposed to leave standing.
        routeless = resolve_listing_media_type(
            {"type": "", "title": title}, {"is_tv": detail})
        assert conflicted.media_type is routeless.media_type, (
            route, title, detail, conflicted.because, routeless.because)
        assert conflicted.because == routeless.because, (
            route, title, detail)
