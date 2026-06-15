"""Tests for backend/metadata_enricher.py — MetadataEnricher class.

Covers:
- Skips when no TMDB API key or no items to enrich
- TMDB lookup via imdb_id (find) and title search fallback
- Title construction (eng only, eng+orig, TV name fields)
- Year extraction from release_date / first_air_date
- Genres and language population from TMDB result
- TMDB vote fallback when OMDb not configured
- OMDb ratings, votes, RT score from Ratings array
- OMDb cache hit skips network call
- IMDb direct-scrape fallback when OMDb unavailable
- RT score from scraper fallback
- progress_fn and log_fn callbacks
- stop_flag_fn aborts loop
- Exceptions inside fetch_metadata don't crash enrich()
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.metadata_enricher import MetadataEnricher
from backend.scanner_service import MediaItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(
    title="Test Movie",
    year=2020,
    imdb_id=None,
    description="",
    season=None,
    rt_score=None,
    rating=0.0,
    votes=0,
    votes_source="",
    poster_path=None,
):
    return MediaItem(
        id="test-id",
        title=title,
        year=year,
        season=season,
        imdb_id=imdb_id,
        description=description,
        rt_score=rt_score,
        rating=rating,
        votes=votes,
        votes_source=votes_source,
        poster_path=poster_path,
    )


def _make_enricher(config=None, scrapers=None, omdb_cache=None):
    cfg = {"tmdb_api_key": "fake_key"}
    if config is not None:
        cfg.update(config)
    scrapers = scrapers or MagicMock()
    return MetadataEnricher(cfg, scrapers, omdb_cache or {})


def _run(enricher, items, **kwargs):
    asyncio.run(enricher.enrich(items, **kwargs))


def _tmdb_movie_result(**kwargs):
    base = {
        "overview": "A great film.",
        "poster_path": "/poster.jpg",
        "title": "Test Movie",
        "original_title": "Test Movie",
        "release_date": "2020-06-15",
        "genre_ids": [],
    }
    base.update(kwargs)
    return base


def _tmdb_tv_result(**kwargs):
    base = {
        "overview": "A great show.",
        "poster_path": "/show.jpg",
        "name": "Breaking Bad",
        "original_name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "genre_ids": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

class TestEnrichSkips:

    def test_no_tmdb_key_exits_immediately(self):
        enricher = MetadataEnricher(
            config={}, scrapers=MagicMock(), omdb_cache={}
        )
        item = _item()
        _run(enricher, [item])
        assert item.description == ""

    def test_empty_items_list_does_not_raise(self):
        enricher = _make_enricher()
        _run(enricher, [])  # must not raise

    def test_items_with_description_skipped(self):
        # Enrichment is skipped when both description AND poster_path are already present.
        enricher = _make_enricher()
        item = _item(description="Already filled in.", poster_path="/poster.jpg")
        with patch("backend.metadata_enricher.TmdbClient") as mock_cls:
            _run(enricher, [item])
            mock_cls.assert_not_called()
        assert item.description == "Already filled in."

    def test_mixed_items_only_enriches_empty_ones(self):
        mock_tmdb = MagicMock()
        mock_tmdb.search.return_value = [_tmdb_movie_result()]

        enricher = _make_enricher()
        has_desc = _item(description="Done", title="Test Movie Already")
        needs_desc = _item(description="", title="Test Movie")

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [has_desc, needs_desc])

        assert has_desc.description == "Done"
        assert needs_desc.description == "A great film."


# ---------------------------------------------------------------------------
# TMDB lookup — search path
# ---------------------------------------------------------------------------

class TestTmdbSearchPath:

    def _run_with_tmdb(self, item, result, movie=True):
        mock_tmdb = MagicMock()
        if movie:
            mock_tmdb.search.return_value = [result]
        else:
            mock_tmdb.search.return_value = [result]
        mock_tmdb.find.return_value = None
        enricher = _make_enricher()
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])
        return item

    def test_description_set(self):
        item = self._run_with_tmdb(_item(), _tmdb_movie_result(overview="Epic story."))
        assert item.description == "Epic story."

    def test_poster_path_set(self):
        item = self._run_with_tmdb(_item(), _tmdb_movie_result(poster_path="/new.jpg"))
        assert item.poster_path == "/new.jpg"

    def test_title_set_from_english_title(self):
        item = self._run_with_tmdb(
            _item(title="Inception"),
            _tmdb_movie_result(title="Inception", original_title="Inception"),
        )
        assert item.title == "Inception"

    def test_bilingual_title_combined(self):
        item = self._run_with_tmdb(
            _item(title="Parasite"),
            _tmdb_movie_result(title="Parasite", original_title="기생충"),
        )
        assert item.title == "Parasite (기생충)"

    def test_year_extracted_from_release_date(self):
        item = _item(year=0)
        self._run_with_tmdb(item, _tmdb_movie_result(release_date="2019-05-30"))
        assert item.year == 2019

    def test_year_not_overwritten_when_set(self):
        item = _item(year=2019)
        self._run_with_tmdb(item, _tmdb_movie_result(release_date="2020-01-01"))
        assert item.year == 2019

    def test_invalid_release_date_leaves_year_unchanged(self):
        item = _item(year=0)
        self._run_with_tmdb(item, _tmdb_movie_result(release_date="???"))
        assert item.year == 0

    def test_tv_uses_name_fields(self):
        mock_tmdb = MagicMock()
        mock_tmdb.search.return_value = [_tmdb_tv_result()]
        mock_tmdb.external_ids.return_value = {"imdb_id": "tt0903747"}

        enricher = _make_enricher()
        item = _item(title="Breaking Bad", season=1, year=0)
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.description == "A great show."
        assert item.year == 2008

    def test_genres_populated(self):
        item = _item()
        self._run_with_tmdb(item, _tmdb_movie_result(genre_ids=[28, 12]))
        # 28 = Action, 12 = Adventure per TMDB_GENRE_MAP
        assert "Action" in item.genres
        assert "Adventure" in item.genres

    def test_unknown_genre_id_ignored(self):
        item = _item()
        self._run_with_tmdb(item, _tmdb_movie_result(genre_ids=[99999]))
        assert item.genres == []

    def test_language_populated(self):
        item = _item()
        self._run_with_tmdb(item, _tmdb_movie_result(original_language="en"))
        assert item.language == "English"

    def test_unknown_language_uppercased(self):
        item = _item()
        self._run_with_tmdb(item, _tmdb_movie_result(original_language="xx"))
        assert item.language == "XX"

    def test_tmdb_vote_fallback_when_no_omdb(self):
        item = _item()
        self._run_with_tmdb(
            item,
            _tmdb_movie_result(vote_average=7.5, vote_count=1200),
        )
        assert item.rating == 7.5
        assert item.votes == 1200
        assert item.votes_source == "tmdb"

    def test_tmdb_votes_not_overwrite_existing_rating(self):
        item = _item(rating=8.0, votes=500000, votes_source="imdb")
        self._run_with_tmdb(
            item,
            _tmdb_movie_result(vote_average=5.0, vote_count=100),
        )
        # Existing rating stays; votes only set when 0
        assert item.rating == 8.0


# ---------------------------------------------------------------------------
# TMDB lookup — find-by-imdb_id path
# ---------------------------------------------------------------------------

class TestTmdbFindPath:

    def test_find_by_imdb_id_used_when_present(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = {
            "movie_results": [_tmdb_movie_result(overview="Found via ID")]
        }
        mock_tmdb.details.return_value = {"imdb_id": "tt9999999"}

        enricher = _make_enricher()
        item = _item(imdb_id="tt9999999")
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.description == "Found via ID"
        mock_tmdb.find.assert_called_once_with("tt9999999")

    def test_find_falls_back_to_search_when_empty(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = {"movie_results": []}
        mock_tmdb.search.return_value = [_tmdb_movie_result(overview="Search fallback")]

        enricher = _make_enricher()
        item = _item(imdb_id="tt0000001")
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.description == "Search fallback"

    def test_tv_find_uses_tv_results(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = {
            "tv_results": [_tmdb_tv_result(overview="Found TV")],
            "movie_results": [],
        }
        mock_tmdb.external_ids.return_value = {"imdb_id": "tt0903747"}

        enricher = _make_enricher()
        item = _item(imdb_id="tt0903747", season=1)
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.description == "Found TV"

    def test_imdb_id_fetched_from_details_when_missing(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = None
        mock_tmdb.search.return_value = [_tmdb_movie_result(id=777)]
        mock_tmdb.details.return_value = {"imdb_id": "tt0011223"}

        enricher = _make_enricher()
        item = _item()
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.imdb_id == "tt0011223"

    def test_external_ids_used_for_tv(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = None
        mock_tmdb.search.return_value = [_tmdb_tv_result(id=555)]
        mock_tmdb.external_ids.return_value = {"imdb_id": "tt0903747"}

        enricher = _make_enricher()
        item = _item(season=1, title="Breaking Bad")
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.imdb_id == "tt0903747"


# ---------------------------------------------------------------------------
# OMDb ratings
# ---------------------------------------------------------------------------

class TestOmdbRatings:

    def _setup(self, omdb_response, omdb_cache=None):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = {
            "movie_results": [_tmdb_movie_result()]
        }
        mock_tmdb.details.return_value = {"imdb_id": "tt1234567"}

        enricher = _make_enricher(
            config={"tmdb_api_key": "fakekey", "omdb_api_key": "omdbkey"},
            omdb_cache=omdb_cache or {},
        )
        return mock_tmdb, enricher

    def test_imdb_rating_and_votes_set(self):
        mock_tmdb, enricher = self._setup(None)
        item = _item(imdb_id="tt1234567")

        omdb_data = {
            "Response": "True",
            "imdbRating": "8.3",
            "imdbVotes": "1,500,000",
            "Ratings": [],
        }

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = omdb_data
            mock_get.return_value = mock_resp
            _run(enricher, [item])

        assert item.rating == 8.3
        assert item.votes == 1_500_000
        assert item.votes_source == "imdb"

    def test_rt_score_from_ratings_array(self):
        mock_tmdb, enricher = self._setup(None)
        item = _item(imdb_id="tt0000001")

        omdb_data = {
            "Response": "True",
            "imdbRating": "7.0",
            "imdbVotes": "500,000",
            "Ratings": [{"Source": "Rotten Tomatoes", "Value": "92%"}],
        }

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = omdb_data
            mock_get.return_value = mock_resp
            _run(enricher, [item])

        assert item.rt_score == 92

    def test_rt_score_already_set_not_overwritten(self):
        mock_tmdb, enricher = self._setup(None)
        item = _item(imdb_id="tt0000002", rt_score=80)

        omdb_data = {
            "Response": "True",
            "imdbRating": "7.0",
            "imdbVotes": "100,000",
            "Ratings": [{"Source": "Rotten Tomatoes", "Value": "55%"}],
        }

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = omdb_data
            mock_get.return_value = mock_resp
            _run(enricher, [item])

        # rt_score was already 80, should not be overwritten
        assert item.rt_score == 80

    def test_cache_hit_skips_network(self):
        cached = {
            "Response": "True",
            "imdbRating": "6.5",
            "imdbVotes": "200,000",
            "Ratings": [],
        }
        mock_tmdb, enricher = self._setup(None, omdb_cache={"tt5555555": cached})
        item = _item(imdb_id="tt5555555")

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get") as mock_get:
            _run(enricher, [item])
            mock_get.assert_not_called()

        assert item.rating == 6.5

    def test_omdb_failed_response_skips(self):
        mock_tmdb, enricher = self._setup(None)
        item = _item(imdb_id="tt7777777")

        omdb_data = {"Response": "False", "Error": "Movie not found!"}

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = omdb_data
            mock_get.return_value = mock_resp
            _run(enricher, [item])

        assert item.rating == 0.0

    def test_omdb_na_rating_not_set(self):
        mock_tmdb, enricher = self._setup(None)
        item = _item(imdb_id="tt8888888")

        omdb_data = {
            "Response": "True",
            "imdbRating": "N/A",
            "imdbVotes": "N/A",
            "Ratings": [],
        }

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = omdb_data
            mock_get.return_value = mock_resp
            _run(enricher, [item])

        assert item.rating == 0.0
        assert item.votes == 0


# ---------------------------------------------------------------------------
# Fallback scrapers
# ---------------------------------------------------------------------------

class TestFallbackScrapers:

    def _tmdb_with_imdb_result(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = {
            "movie_results": [_tmdb_movie_result()]
        }
        return mock_tmdb

    def test_imdb_scrape_fallback_when_no_omdb_key(self):
        mock_scrapers = MagicMock()
        mock_scrapers.scrape_imdb_data.return_value = {"rating": 7.8, "votes": 250000}

        enricher = MetadataEnricher(
            config={"tmdb_api_key": "fakekey"},  # no omdb key
            scrapers=mock_scrapers,
            omdb_cache={},
        )
        item = _item(imdb_id="tt9999999")

        with patch("backend.metadata_enricher.TmdbClient", return_value=self._tmdb_with_imdb_result()):
            _run(enricher, [item])

        assert item.rating == 7.8
        assert item.votes == 250000
        assert item.votes_source == "imdb"

    def test_imdb_scrape_zero_rating_not_applied(self):
        mock_scrapers = MagicMock()
        mock_scrapers.scrape_imdb_data.return_value = {"rating": 0, "votes": 0}

        enricher = MetadataEnricher(
            config={"tmdb_api_key": "fakekey"},
            scrapers=mock_scrapers,
            omdb_cache={},
        )
        item = _item(imdb_id="tt9999999")

        with patch("backend.metadata_enricher.TmdbClient", return_value=self._tmdb_with_imdb_result()):
            _run(enricher, [item])

        assert item.rating == 0.0

    def test_rt_score_from_scraper(self):
        mock_scrapers = MagicMock()
        mock_scrapers.scrape_rt_score.return_value = {"critics": 85}

        enricher = MetadataEnricher(
            config={"tmdb_api_key": "fakekey", "show_rt": True},
            scrapers=mock_scrapers,
            omdb_cache={},
        )
        mock_tmdb = MagicMock()
        mock_tmdb.search.return_value = [_tmdb_movie_result()]

        item = _item()
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        assert item.rt_score == 85

    def test_rt_score_not_fetched_when_show_rt_false(self):
        mock_scrapers = MagicMock()
        mock_scrapers.scrape_rt_score.return_value = {"critics": 99}

        enricher = MetadataEnricher(
            config={"tmdb_api_key": "fakekey", "show_rt": False},
            scrapers=mock_scrapers,
            omdb_cache={},
        )
        mock_tmdb = MagicMock()
        mock_tmdb.search.return_value = [_tmdb_movie_result()]

        item = _item()
        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])

        mock_scrapers.scrape_rt_score.assert_not_called()


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

class TestCallbacks:

    def _mock_tmdb(self):
        m = MagicMock()
        m.search.return_value = [_tmdb_movie_result()]
        return m

    def test_progress_fn_called_for_each_item(self):
        enricher = _make_enricher()
        progress_calls = []

        with patch("backend.metadata_enricher.TmdbClient", return_value=self._mock_tmdb()):
            asyncio.run(enricher.enrich(
                [_item(), _item(title="Movie 2")],
                progress_fn=lambda f, l: progress_calls.append(f),
            ))

        assert len(progress_calls) == 2
        assert progress_calls[-1] == 1.0

    def test_log_fn_start_and_end_messages(self):
        enricher = _make_enricher()
        logs = []

        with patch("backend.metadata_enricher.TmdbClient", return_value=self._mock_tmdb()):
            asyncio.run(enricher.enrich([_item()], log_fn=logs.append))

        assert any("Enriching metadata" in msg for msg in logs)
        assert any("complete" in msg for msg in logs)

    def test_stop_flag_aborts_processing(self):
        enricher = _make_enricher()
        call_count = [0]

        def stop_fn():
            call_count[0] += 1
            return call_count[0] > 1

        with patch("backend.metadata_enricher.TmdbClient", return_value=self._mock_tmdb()):
            # Five items; should abort after first future completes
            asyncio.run(enricher.enrich(
                [_item(title=f"Movie {i}") for i in range(5)],
                stop_flag_fn=stop_fn,
            ))
        # Just verify it doesn't hang or crash when stop fires


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------

class TestExceptionSafety:

    def test_tmdb_search_exception_does_not_crash(self):
        mock_tmdb = MagicMock()
        mock_tmdb.search.side_effect = Exception("network timeout")
        mock_tmdb.find.return_value = None

        enricher = _make_enricher()
        item = _item()

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])  # must not raise

        assert item.description == ""

    def test_omdb_exception_does_not_crash(self):
        mock_tmdb = MagicMock()
        mock_tmdb.find.return_value = {
            "movie_results": [_tmdb_movie_result()]
        }
        mock_tmdb.details.return_value = {}

        enricher = _make_enricher(
            config={"tmdb_api_key": "fakekey", "omdb_api_key": "omdbkey"}
        )
        item = _item(imdb_id="tt1234567")

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb), \
             patch("backend.metadata_enricher.requests.get", side_effect=Exception("omdb down")):
            _run(enricher, [item])  # must not raise

        assert item.description == "A great film."

    def test_rt_scrape_exception_does_not_crash(self):
        mock_scrapers = MagicMock()
        mock_scrapers.scrape_rt_score.side_effect = Exception("RT down")

        mock_tmdb = MagicMock()
        mock_tmdb.search.return_value = [_tmdb_movie_result()]

        enricher = MetadataEnricher(
            config={"tmdb_api_key": "fakekey", "show_rt": True},
            scrapers=mock_scrapers,
            omdb_cache={},
        )
        item = _item()

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item])  # must not raise

        assert item.rt_score is None

    def test_multiple_items_one_exception_others_proceed(self):
        call_count = [0]

        def search_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("first item fails")
            return [_tmdb_movie_result(overview="Second item ok")]

        mock_tmdb = MagicMock()
        mock_tmdb.search.side_effect = search_side_effect
        mock_tmdb.find.return_value = None

        enricher = _make_enricher()
        item1 = _item(title="Fail Movie")
        item2 = _item(title="Test Movie")

        with patch("backend.metadata_enricher.TmdbClient", return_value=mock_tmdb):
            _run(enricher, [item1, item2])

        # item2 should have been enriched successfully
        assert item2.description == "Second item ok"
