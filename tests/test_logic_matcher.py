"""Tests for backend/logic/matcher.py — PlexMatcher class.

Covers:
- clean_string() normalization
- build_plex_lookup_index() structure
- compare_and_display() matching & status outcomes
- Edge cases (empty library, missing fields)
"""

import pytest

import sys, os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.logic.matcher import PlexMatcher


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    """Minimal config dict for PlexMatcher."""
    return {
        "movie_match_threshold": 85,
        "tv_match_threshold": 90,
        "low_match_threshold": 80,
        "year_tolerance": 1,
        "pref_res": "Prefer 4K",
        "rule_1080_4k": True,
    }


@pytest.fixture
def matcher(config):
    return PlexMatcher(config)


@pytest.fixture
def sample_plex_lib():
    """A small Plex library list for index building."""
    return [
        {
            "title": "The Matrix",
            "clean_title": "the matrix",
            "original_title": "The Matrix",
            "year": 1999,
            "res": "1080p",
            "imdb_id": "tt0133093",
            "rating_key": "1001",
        },
        {
            "title": "Inception",
            "clean_title": "inception",
            "original_title": "Inception",
            "year": 2010,
            "res": "4K",
            "imdb_id": "tt1375666",
            "rating_key": "1002",
        },
        {
            "title": "Breaking Bad",
            "clean_title": "breaking bad",
            "original_title": "Breaking Bad",
            "year": 2008,
            "res": "1080p",
            "imdb_id": "tt0903747",
            "rating_key": "2001",
            "season": 1,
            "is_tv": True,
        },
        {
            "title": "Breaking Bad",
            "clean_title": "breaking bad",
            "original_title": "Breaking Bad",
            "year": 2008,
            "res": "1080p",
            "imdb_id": "tt0903747",
            "rating_key": "2002",
            "season": 2,
            "is_tv": True,
        },
    ]


@pytest.fixture
def plex_index(matcher, sample_plex_lib):
    """Pre-built index from the sample library."""
    return matcher.build_plex_lookup_index(sample_plex_lib)


# ===================================================================
# clean_string tests
# ===================================================================

class TestCleanString:

    def test_basic_lowercase(self, matcher):
        assert matcher.clean_string("Hello World") == "hello world"

    def test_accented_characters(self, matcher):
        # NFKD + ASCII encode removes accents
        result = matcher.clean_string("Amelie")
        assert result == "amelie"

    def test_accented_e(self, matcher):
        result = matcher.clean_string("\u00c9mile")
        assert result == "emile"

    def test_special_chars_removed(self, matcher):
        result = matcher.clean_string("Spider-Man: No Way Home!")
        # hyphens and colons and exclamation are non-word
        assert ":" not in result
        assert "!" not in result
        assert "-" not in result

    def test_empty_string(self, matcher):
        assert matcher.clean_string("") == ""

    def test_none_returns_empty(self, matcher):
        assert matcher.clean_string(None) == ""

    def test_numeric_string(self, matcher):
        assert matcher.clean_string("2001") == "2001"

    def test_whitespace_stripped(self, matcher):
        assert matcher.clean_string("  spaced  ") == "spaced"

    def test_underscores_kept(self, matcher):
        # \w includes underscores, so they are preserved
        result = matcher.clean_string("some_title")
        assert "_" in result


# ===================================================================
# build_plex_lookup_index tests
# ===================================================================

class TestBuildPlexLookupIndex:

    def test_keys_present(self, plex_index):
        assert "by_imdb" in plex_index
        assert "by_title" in plex_index
        assert "by_title_year" in plex_index
        assert "all_items" in plex_index

    def test_by_imdb_lookup(self, plex_index):
        assert "tt0133093" in plex_index["by_imdb"]
        items = plex_index["by_imdb"]["tt0133093"]
        assert any(i["rating_key"] == "1001" for i in items)

    def test_by_title_lookup(self, plex_index):
        assert "the matrix" in plex_index["by_title"]

    def test_by_title_year_lookup(self, plex_index):
        assert "the matrix|1999" in plex_index["by_title_year"]

    def test_all_items_length(self, plex_index, sample_plex_lib):
        assert len(plex_index["all_items"]) == len(sample_plex_lib)

    def test_empty_library(self, matcher):
        index = matcher.build_plex_lookup_index([])
        assert index["by_imdb"] == {}
        assert index["by_title"] == {}
        assert index["by_title_year"] == {}
        assert index["all_items"] == []

    def test_item_without_imdb_not_in_by_imdb(self, matcher):
        lib = [{"title": "Unknown", "year": 2020, "clean_title": "unknown"}]
        index = matcher.build_plex_lookup_index(lib)
        assert len(index["by_imdb"]) == 0

    def test_item_without_clean_title_gets_generated(self, matcher):
        lib = [{"title": "Test Movie", "year": 2020}]
        index = matcher.build_plex_lookup_index(lib)
        # clean_title should be populated from title
        assert "test movie" in index["by_title"]

    def test_multiple_items_same_imdb(self, matcher):
        """TV seasons share the same IMDb ID; they should all appear."""
        lib = [
            {"title": "Show", "clean_title": "show", "year": 2020, "imdb_id": "tt1111111", "season": 1},
            {"title": "Show", "clean_title": "show", "year": 2020, "imdb_id": "tt1111111", "season": 2},
        ]
        index = matcher.build_plex_lookup_index(lib)
        assert len(index["by_imdb"]["tt1111111"]) == 2


# ===================================================================
# compare_and_display tests
# ===================================================================

class TestCompareAndDisplay:

    # --- Movie matching -------------------------------------------------

    def test_movie_found_by_imdb(self, matcher, plex_index):
        web = {
            "display_title": "The Matrix",
            "search_key": "the matrix",
            "year": 1999,
            "imdb_id": "tt0133093",
            "is_tv": False,
            "res": "1080p",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert status == "In Library"
        assert plex_id == "1001"

    def test_movie_missing(self, matcher, plex_index):
        web = {
            "display_title": "Nonexistent Movie",
            "search_key": "nonexistent movie",
            "year": 2099,
            "imdb_id": None,
            "is_tv": False,
            "res": "1080p",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert status == "MISSING"
        assert plex_id is None
        assert match_item is None

    def test_movie_upgrade_4k(self, matcher, plex_index):
        """Web has 4K for a movie that is 1080p in Plex => UPGRADE."""
        web = {
            "display_title": "The Matrix",
            "search_key": "the matrix",
            "year": 1999,
            "imdb_id": "tt0133093",
            "is_tv": False,
            "res": "4K",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert "UPGRADE" in status

    def test_movie_no_upgrade_when_already_4k(self, matcher, plex_index):
        """Inception is already 4K in Plex; web 4K should be 'In Library'."""
        web = {
            "display_title": "Inception",
            "search_key": "inception",
            "year": 2010,
            "imdb_id": "tt1375666",
            "is_tv": False,
            "res": "4K",
        }
        status, color, info, plex_id, _ = matcher.compare_and_display(web, plex_index)
        assert status == "In Library"

    def test_movie_found_by_fuzzy_title(self, matcher, plex_index):
        """Match by title+year when no IMDb ID is present."""
        web = {
            "display_title": "The Matrix",
            "search_key": "the matrix",
            "year": 1999,
            "imdb_id": None,
            "is_tv": False,
            "res": "1080p",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert status == "In Library"

    # --- TV matching ----------------------------------------------------

    def test_tv_season_found_by_imdb(self, matcher, plex_index):
        web = {
            "display_title": "Breaking Bad",
            "search_key": "breaking bad",
            "year": 0,
            "imdb_id": "tt0903747",
            "is_tv": True,
            "season": 1,
            "res": "1080p",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert status == "In Library"
        assert plex_id == "2001"

    def test_tv_season_missing(self, matcher, plex_index):
        web = {
            "display_title": "Breaking Bad",
            "search_key": "breaking bad",
            "year": 0,
            "imdb_id": "tt0903747",
            "is_tv": True,
            "season": 5,  # Season 5 not in library
            "res": "1080p",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert status == "MISSING"

    def test_tv_upgrade_4k(self, matcher, plex_index):
        """Plex has Breaking Bad S01 at 1080p; web has 4K."""
        web = {
            "display_title": "Breaking Bad",
            "search_key": "breaking bad",
            "year": 0,
            "imdb_id": "tt0903747",
            "is_tv": True,
            "season": 1,
            "res": "4K",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, plex_index)
        assert "UPGRADE" in status

    # --- Empty library --------------------------------------------------

    def test_empty_library_returns_missing(self, matcher):
        empty_index = matcher.build_plex_lookup_index([])
        web = {
            "display_title": "Any Movie",
            "search_key": "any movie",
            "year": 2020,
            "imdb_id": "tt0000001",
            "is_tv": False,
            "res": "1080p",
        }
        status, color, info, plex_id, match_item = matcher.compare_and_display(web, empty_index)
        assert status == "MISSING"

    # --- Color codes ----------------------------------------------------

    def test_missing_color_is_red(self, matcher, plex_index):
        web = {
            "display_title": "Ghost",
            "search_key": "ghost",
            "year": 3000,
            "imdb_id": None,
            "is_tv": False,
            "res": "1080p",
        }
        _, color, _, _, _ = matcher.compare_and_display(web, plex_index)
        assert color == "#ff4d4d"

    def test_in_library_color_is_green(self, matcher, plex_index):
        web = {
            "display_title": "Inception",
            "search_key": "inception",
            "year": 2010,
            "imdb_id": "tt1375666",
            "is_tv": False,
            "res": "1080p",
        }
        _, color, _, _, _ = matcher.compare_and_display(web, plex_index)
        assert color == "#2ecc71"

    def test_upgrade_color_is_orange(self, matcher, plex_index):
        web = {
            "display_title": "The Matrix",
            "search_key": "the matrix",
            "year": 1999,
            "imdb_id": "tt0133093",
            "is_tv": False,
            "res": "4K",
        }
        _, color, _, _, _ = matcher.compare_and_display(web, plex_index)
        assert color == "#f39c12"
