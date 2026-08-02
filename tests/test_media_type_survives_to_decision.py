"""The media-type verdict must survive to the decision that uses it.

Written 2026-08-02 after external review found that the authority resolver is
correct and its verdict is **discarded before every production decision**.

THE SHAPE OF THIS MISS, WHICH HAS NOW HAPPENED THREE TIMES:

    round 5   proved RSS agrees with SourceBase          -> wrong reader
    round 6   proved the shared grammar is correct       -> right grammar,
                                                            wrong consumer
    round 7   proved the resolver is correct             -> right resolver,
                                                            verdict discarded

Each round tested the component at the boundary where it was written, and
never followed the value to the place that acts on it. A resolver whose output
nothing reads is as good as no resolver.

TWO INDEPENDENT LOSSES, both reproduced:

1. **RSS collapses AMBIGUOUS to "movie".** `_parse_item` stores
   `"tv" if verdict is TV else "movie"`, dropping `provisional` and `because`.
   An unknown feed category plus a silent title resolves to AMBIGUOUS and is
   persisted as a *movie* — the precise opposite of the fail-closed behaviour
   the resolver was built to provide, and written by the same hand that wrote
   the fail-closed test.

2. **The listing verdict never reaches Plex matching.** `MediaItem` carries no
   media-type field. `_create_media_item` reads `result['details']` and
   `result['url']` but not `result['is_tv']`, and matching later reconstructs
   `is_tv` as `item.season is not None`. `Great Show Complete Series` resolves
   to TV, has no numeric season, and is therefore matched as a movie.

Every test here is ``xfail(strict=True)`` against the desired end state: they
fail today and turn the suite RED when the verdict is carried through, which is
the signal to delete the markers.
"""

import dataclasses
import inspect

import pytest

from backend import release_grammar as grammar
from backend.scanner_service import MediaItem


# ───────────────────────── loss 1: RSS collapses AMBIGUOUS ──────────────────

def test_the_resolver_itself_still_returns_ambiguous():
    """Not an xfail — the resolver is fine. It is the caller that loses it,
    and this is here so a future reader does not 'fix' the wrong component."""
    verdict = grammar.resolve_media_type([
        None,  # unknown feed category contributes nothing
        grammar.title_type_evidence("The Batman 2022 1080p", source="feed-title"),
    ])
    assert verdict.media_type is grammar.MediaType.AMBIGUOUS


@pytest.mark.xfail(strict=True, reason=(
    "_parse_item stores `'tv' if verdict is TV else 'movie'`, so AMBIGUOUS is "
    "persisted as a movie. Unknown category + silent title therefore becomes a "
    "confident wrong answer at the storage boundary."))
def test_rss_does_not_store_ambiguous_as_movie():
    source = inspect.getsource(
        __import__("backend.sources.hdencode_feed_parser", fromlist=["_"]))
    assert 'else "movie"' not in source, (
        "the RSS media_type assignment still collapses every non-TV verdict, "
        "AMBIGUOUS included, to 'movie'")


@pytest.mark.xfail(strict=True, reason=(
    "provisional and because are computed and dropped. A ROUTE-only verdict is "
    "indistinguishable downstream from one backed by a confirmed identity, so "
    "nothing can refuse to act on weak evidence."))
def test_rss_preserves_provisional_and_provenance():
    source = inspect.getsource(
        __import__("backend.sources.hdencode_feed_parser", fromlist=["_"]))
    assert "provisional" in source and "because" in source


# ────────────────── loss 2: the listing verdict never arrives ───────────────

@pytest.mark.xfail(strict=True, reason=(
    "MediaItem has no media-type field, so the resolver's verdict cannot be "
    "carried on the object that represents a scan result."))
def test_media_item_carries_a_media_type():
    names = {f.name for f in dataclasses.fields(MediaItem)}
    assert names & {"media_type", "is_tv", "media_type_verdict"}


@pytest.mark.xfail(strict=True, reason=(
    "_create_media_item reads result['details'] and result['url'] but never "
    "result['is_tv'], so the verdict computed in process_post is dropped on "
    "the floor at the object boundary."))
def test_create_media_item_consumes_the_resolved_verdict():
    source = inspect.getsource(
        __import__("backend.scanner_service", fromlist=["_"]))
    start = source.index("def _create_media_item")
    body = source[start:start + 4000]
    assert "is_tv" in body or "media_type" in body


@pytest.mark.xfail(strict=True, reason=(
    "Plex matching rebuilds `is_tv` as `item.season is not None`. "
    "'Great Show Complete Series' resolves to TV, carries no numeric season, "
    "and is therefore matched as a MOVIE — divergence (f), alive after the "
    "resolver said TV."))
def test_plex_matching_does_not_reconstruct_media_type_from_season():
    source = inspect.getsource(
        __import__("backend.scanner_service", fromlist=["_"]))
    assert "'is_tv': item.season is not None" not in source


def test_THE_REGRESSION_complete_series_has_no_numeric_season():
    """Not an xfail — this documents WHY the reconstruction is wrong, and it is
    true regardless of how the fix is shaped.

    The reconstruction assumes season-presence and TV-ness are the same fact.
    They are not: a season pack, a complete series and a mini-series are all TV
    and all lack an SxxExx token."""
    verdict = grammar.resolve_media_type([
        grammar.title_type_evidence("Great Show Complete Series 1080p",
                                    source="listing-title")])
    assert verdict.media_type is grammar.MediaType.TV
    assert grammar.parse_season_episode("Great Show Complete Series 1080p").season is None
