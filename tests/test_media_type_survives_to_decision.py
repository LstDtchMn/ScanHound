"""The media-type verdict must survive to the decision that uses it.

External review found the authority resolver correct and its verdict
**discarded before every production decision**. This file asserts the value
arrives, end to end.

THE SHAPE OF THAT MISS, WHICH HAPPENED THREE TIMES:

    round 5   proved RSS agrees with SourceBase      -> wrong reader
    round 6   proved the shared grammar is correct   -> right grammar,
                                                        wrong consumer
    round 7   proved the resolver is correct         -> right resolver,
                                                        verdict discarded

Each round tested the component at the boundary where it was written and never
followed the value to what acts on it.

**These tests are BEHAVIOURAL, not source-inspection.** The first version of
this file grepped module source for tell-tale strings, and one of those
assertions promptly produced a false negative: it matched the explanatory
comment describing the old behaviour, reporting a gap that had already been
closed. Reading source text proves wiring at best and prose at worst, so every
assertion below runs the real code and inspects the real value.
"""

import dataclasses

import pytest

from backend import release_grammar as grammar
from backend.scanner_service import MediaItem, web_item_facts
from backend.sources.hdencode_feed_parser import parse_feed


def _feed(title, categories=()):
    """A minimal but real RSS document, parsed by the production parser."""
    cats = "".join(f"<category>{c}</category>" for c in categories)
    return f"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>{title}</title>
    <link>https://hdencode.org/some-release/</link>
    <guid>guid-1</guid>
    <pubDate>Sat, 02 Aug 2026 10:00:00 +0000</pubDate>
    {cats}
    <description>A description.</description>
  </item>
</channel></rss>""".encode("utf-8")


def _entry(title, categories=()):
    return parse_feed(_feed(title, categories), "movies_1080p").entries[0]


# ───────────────── the resolver itself (guard against wrong fix) ────────────

def test_the_resolver_returns_ambiguous_for_unknown_plus_silent():
    """Not the defect — here so a future reader does not 'fix' the resolver
    when the loss was always in its callers."""
    verdict = grammar.resolve_media_type(
        [None, grammar.title_type_evidence("The Batman 2022 1080p")])
    assert verdict.media_type is grammar.MediaType.AMBIGUOUS


# ───────────────────────── RSS: the storage boundary ────────────────────────

class TestRssStoresTheVerdict:
    def test_ambiguous_is_stored_as_ambiguous_not_movie(self):
        """THE REGRESSION. An unknown feed category plus a silent title
        resolves AMBIGUOUS; it used to be persisted as a confident movie."""
        entry = _entry("The Batman 2022 1080p BluRay 14.7 GB",
                       categories=("Uncategorised",))
        assert entry.media_type == "ambiguous"

    def test_a_tv_title_is_still_tv(self):
        entry = _entry("Great Show Complete Series 1080p WEB-DL 40.0 GB")
        assert entry.media_type == "tv"

    def test_a_trusted_category_resolves_a_silent_title(self):
        """The legitimate additive case survives the fix."""
        entry = _entry("The Batman 2022 1080p BluRay 14.7 GB",
                       categories=("TV Shows",))
        assert entry.media_type == "tv"

    def test_provisional_is_carried(self):
        """A category-only verdict must stay distinguishable from one backed by
        a title, or nothing downstream can decline to act on weak evidence."""
        weak = _entry("The Batman 2022 1080p BluRay 14.7 GB",
                      categories=("TV Shows",))
        strong = _entry("Great Show Complete Series 1080p WEB-DL 40.0 GB")
        assert weak.media_type_provisional is True
        assert strong.media_type_provisional is False

    def test_provenance_is_carried(self):
        entry = _entry("Great Show Complete Series 1080p WEB-DL 40.0 GB")
        assert entry.media_type_because
        assert any("feed-title" in reason for reason in entry.media_type_because)

    def test_an_unknown_category_contributes_nothing_rather_than_guessing(self):
        """'TVrip' contains 'tv'. The old substring match called it TV."""
        entry = _entry("The Batman 2022 1080p BluRay 14.7 GB",
                       categories=("TVrip",))
        assert entry.media_type == "ambiguous"


# ─────────────────── listing: the object and decision boundary ──────────────

class TestMediaItemCarriesTheVerdict:
    def test_media_item_has_a_media_type_field(self):
        names = {f.name for f in dataclasses.fields(MediaItem)}
        assert "media_type" in names
        assert "media_type_provisional" in names

    def test_it_defaults_to_ambiguous_not_movie(self):
        """An item built without an explicit verdict must never be mistaken
        for a film."""
        item = MediaItem(id="1", title="X", year=2024)
        assert item.media_type == "ambiguous"
        assert item.media_type_provisional is True

    @pytest.mark.parametrize("media_type,expected_is_tv", [
        ("tv", True),
        ("movie", False),
        ("ambiguous", False),
    ])
    def test_plex_matching_uses_the_carried_verdict(self, media_type,
                                                    expected_is_tv):
        """THE REGRESSION, asserted through the PRODUCTION function.

        An earlier version of this test wrote `(item.media_type == "tv")` —
        a restatement of the rule rather than a call to it — and a mutation
        reverting production to `season is not None` passed it. It now calls
        web_item_facts(), the code Plex matching actually uses.

        Every case has season=None, so the OLD rule answers False for all
        three, including the TV one. That is the defect: a season pack, a
        complete series and a mini-series are all TV and none carries a
        numeric season."""
        item = MediaItem(id="1", title="Great Show", year=2024,
                         season=None, media_type=media_type)
        assert web_item_facts(item)['is_tv'] is expected_is_tv

    def test_the_seam_carries_the_type_and_its_confidence(self):
        item = MediaItem(id="1", title="X", year=2024, media_type="ambiguous",
                         media_type_provisional=True)
        facts = web_item_facts(item)
        assert facts['media_type'] == "ambiguous"
        assert facts['media_type_provisional'] is True

    def test_ambiguous_does_not_select_the_tv_library_but_is_not_movie(self):
        """AMBIGUOUS answers False to 'is this TV?' — it must not pick the TV
        library — yet it stays distinct from 'movie' so the library query can
        refuse to guess."""
        facts = web_item_facts(
            MediaItem(id="1", title="X", year=2024, media_type="ambiguous"))
        assert facts['is_tv'] is False
        assert facts['media_type'] != "movie"


# ──────────────────── the library query refuses to guess ────────────────────

class TestUnresolvedTypeMatchesNothing:
    def test_ambiguous_does_not_fall_through_to_the_movies_library(self, tmp_path):
        """The `else` branch used to be `content_type = 'Movies'`, so an
        unresolved type silently became a movie query — undoing the tri-state
        one layer below where it was produced."""
        from backend.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "t.db"))
        context = db.get_hdencode_candidate_context(
            canonical_url="https://hdencode.org/x/",
            clean_title="Great Show", media_type="ambiguous",
            years=(2024,), season=None, imdb_id=None, tmdb_id=None)
        assert context["plex_matches"] == []
        assert context.get("media_type_unresolved") is True

    def test_a_resolved_type_still_queries_normally(self, tmp_path):
        """Guard against over-correcting into refusing everything."""
        from backend.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "t.db"))
        context = db.get_hdencode_candidate_context(
            canonical_url="https://hdencode.org/x/",
            clean_title="Great Show", media_type="movie",
            years=(2024,), season=None, imdb_id=None, tmdb_id=None)
        assert context.get("media_type_unresolved") is not True


def test_THE_REGRESSION_complete_series_has_no_numeric_season():
    """Why the old reconstruction was wrong, independent of the fix's shape."""
    verdict = grammar.resolve_media_type(
        [grammar.title_type_evidence("Great Show Complete Series 1080p")])
    assert verdict.media_type is grammar.MediaType.TV
    assert grammar.parse_season_episode(
        "Great Show Complete Series 1080p").season is None
