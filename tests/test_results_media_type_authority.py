"""Round-11 Finding 5: results.py consumers must consume the CARRIED
media-type verdict, never reconstruct it from season presence -- full result
dictionaries, not helper proxies."""
from backend.api.routes.results import _effective_category


def _item(**over):
    base = {"url": "https://hdencode.org/x/", "title": "Show Complete Series",
            "year": 2026, "season": None, "category": None}
    base.update(over)
    return base


class TestEffectiveCategory:
    def test_authoritative_tv_without_season_or_category_is_tv(self):
        assert _effective_category(_item(media_type="tv")) == "tv"

    def test_authoritative_movie_with_stray_season_stays_movie(self):
        # The carried verdict outranks a stray/legacy season value.
        assert _effective_category(_item(media_type="movie", season=3)) == "4k"

    def test_explicit_category_wins_over_everything(self):
        assert _effective_category(
            _item(media_type="tv", category="remux")) == "remux"

    def test_ambiguous_falls_back_to_the_legacy_heuristic(self):
        # DECLARED LIMITATION: the facet space is binary (tv/4k), so an
        # ambiguous verdict keeps the display-only season fallback.
        assert _effective_category(_item(media_type="ambiguous", season=2)) == "tv"
        assert _effective_category(_item(media_type="ambiguous")) == "4k"


class TestBookmarkIdentity:
    def _key(self, item):
        from backend.api.routes import results as mod
        return mod._bookmark_key_for_item(item)

    def test_tokenless_tv_bookmark_keys_as_tv(self):
        key = self._key(_item(media_type="tv"))
        assert key[-1] == "tv"

    def test_movie_with_stray_season_keys_as_movie(self):
        key = self._key(_item(media_type="movie", season=3))
        assert key[-1] == "movie"

    def test_imdb_id_short_circuits_type_entirely(self):
        key = self._key(_item(media_type="tv", imdb_id="tt123"))
        assert key == ("imdb", "tt123")


class TestBookmarkAmbiguityPreserved:
    """Round-12 F5 remainder: bookmark identity is PERSISTENT, so an
    unresolved/ambiguous media type must keep its uncertainty in the key --
    never inferred from season, never able to collide with a confident tv or
    movie bookmark."""

    def _key(self, item):
        from backend.api.routes import results as mod
        return mod._bookmark_key_for_item(item)

    def test_ambiguous_type_cannot_collide_with_tv_or_movie(self):
        base = _item(media_type="ambiguous", season=2)
        key = self._key(base)
        assert key != self._key(_item(media_type="tv", season=2))
        assert key != self._key(_item(media_type="movie", season=2))
        assert "ambiguous" in str(key)

    def test_absent_type_is_also_not_inferred_from_season(self):
        key_with = self._key(_item(media_type=None, season=3))
        key_without = self._key(_item(media_type=None))
        assert "tv" not in str(key_with)
        assert "movie" not in str(key_without)
