"""Comprehensive tests for backend/plex_service.py module.

Covers:
- _clean_string helper function
- PlexService.__init__ and callback wiring
- _log / _emit_stats helpers
- connect() — direct mode, account mode, missing config, success, failure
- load_libraries() — cache path, full load, TV loading, progress, diagnostics
- _extract_movie_data — media versions, HDR/DV detection, resolution, reload
- _extract_season_data — episodes, resolution, size, DV/HDR
- _check_dovi — all detection paths (DOVIPresent, _data, profile, display, codec)
- _build_plex_index — by_imdb, by_title, all_items
- check_cache_status — valid, expired, empty, error
"""

import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from backend.plex_service import PlexService, _clean_string


# ── Helpers ──────────────────────────────────────────────────────────

def _make_service(config=None, db=None, plex_manager=None):
    """Build a PlexService with mocked dependencies."""
    cfg = config or {}
    database = db or MagicMock()
    pm = plex_manager or MagicMock()
    return PlexService(config=cfg, db=database, plex_manager=pm)


def _make_mock_movie(title="Test Movie", year=2024, rating_key=1,
                     video_resolution="1080", size=5_368_709_120,
                     guids=None, media_list=None, dovi=False,
                     color_primaries=None, language="en"):
    """Create a mock Plex movie object."""
    movie = MagicMock()
    movie.title = title
    movie.year = year
    movie.ratingKey = rating_key
    movie.originalLanguage = language

    if guids is None:
        mock_guid = MagicMock()
        mock_guid.id = "imdb://tt1234567"
        guids = [mock_guid]
    movie.guids = guids

    if media_list is not None:
        movie.media = media_list
        return movie

    # Build default single media with one part
    stream = MagicMock()
    stream.DOVIPresent = "true" if dovi else None
    stream.colorPrimaries = color_primaries or ""
    stream.DOViProfile = None
    stream.doviProfile = None
    stream._data = {}
    stream.displayTitle = ""
    stream.title = ""
    stream.profile = ""
    stream.codec = "hevc"

    if not dovi:
        del stream.DOVIPresent
        del stream.DOViProfile
        del stream.doviProfile

    part = MagicMock()
    part.size = size
    part.videoStreams.return_value = [stream]

    media = MagicMock()
    media.videoResolution = video_resolution
    media.parts = [part]

    movie.media = [media]
    return movie


def _make_mock_show(title="Test Show", year=2022, rating_key=100,
                    guids=None, seasons=None, language="en"):
    """Create a mock Plex show object."""
    show = MagicMock()
    show.title = title
    show.year = year
    show.ratingKey = rating_key
    show.originalLanguage = language

    if guids is None:
        mock_guid = MagicMock()
        mock_guid.id = "imdb://tt9876543"
        guids = [mock_guid]
    show.guids = guids

    if seasons is not None:
        show.seasons.return_value = seasons
    return show


def _make_mock_season(index=1, rating_key=200, episodes=None):
    """Create a mock Plex season."""
    season = MagicMock()
    season.index = index
    season.ratingKey = rating_key

    if episodes is None:
        ep = MagicMock()
        stream = MagicMock()
        stream.DOVIPresent = None
        stream.colorPrimaries = ""
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        # Remove dovi attrs
        del stream.DOVIPresent
        if hasattr(stream, 'DOViProfile'):
            del stream.DOViProfile
        if hasattr(stream, 'doviProfile'):
            del stream.doviProfile

        part = MagicMock()
        part.size = 2_147_483_648  # 2 GB
        part.videoStreams.return_value = [stream]

        media = MagicMock()
        media.videoResolution = "1080"
        media.parts = [part]

        ep.media = [media]
        episodes = [ep]

    season.episodes.return_value = episodes
    return season


# ======================================================================
# _clean_string Tests
# ======================================================================

class TestCleanString:
    def test_basic(self):
        assert _clean_string("The Matrix") == "the matrix"

    def test_strips_year_in_parens(self):
        assert _clean_string("Inception (2010)") == "inception"

    def test_strips_bare_year(self):
        assert _clean_string("Blade Runner 2049") == "blade runner"

    def test_removes_special_chars(self):
        assert _clean_string("Spider-Man: No Way Home") == "spiderman no way home"

    def test_collapses_whitespace(self):
        assert _clean_string("The   Lord   of   Rings") == "the lord of rings"

    def test_empty_string(self):
        assert _clean_string("") == ""

    def test_none_input(self):
        assert _clean_string(None) == ""

    def test_only_special_chars(self):
        assert _clean_string("!!!---") == ""

    def test_19xx_year(self):
        assert _clean_string("Alien 1979") == "alien"


# ======================================================================
# __init__ / Callbacks
# ======================================================================

class TestPlexServiceInit:
    def test_defaults(self):
        svc = _make_service()
        assert svc.plex_movies == []
        assert svc.plex_tv == []
        assert svc.plex_index == {"by_imdb": {}, "by_title": {}, "all_items": []}
        assert svc.stats == {"plex_1080": 0, "plex_4k": 0, "tv_seasons": 0, "new_items": 0}
        assert svc._plex_loading is False
        assert svc._log_fn is None
        assert svc._stats_callback is None

    def test_set_log_callback(self):
        svc = _make_service()
        fn = MagicMock()
        svc.set_log_callback(fn)
        assert svc._log_fn is fn

    def test_set_stats_callback(self):
        svc = _make_service()
        fn = MagicMock()
        svc.set_stats_callback(fn)
        assert svc._stats_callback is fn


class TestLog:
    def test_log_info(self):
        svc = _make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc._log("hello", "info")
        cb.assert_called_once_with("hello", "info")

    def test_log_success_level(self):
        svc = _make_service()
        # Should not raise
        svc._log("ok", "success")

    def test_log_callback_exception_suppressed(self):
        svc = _make_service()
        svc.set_log_callback(MagicMock(side_effect=RuntimeError("boom")))
        svc._log("msg")  # should not raise

    def test_log_no_callback(self):
        svc = _make_service()
        svc._log("msg", "warning")  # should not raise


class TestEmitStats:
    def test_emits_stats(self):
        svc = _make_service()
        cb = MagicMock()
        svc.set_stats_callback(cb)
        svc.stats = {"plex_1080": 10, "plex_4k": 5, "tv_seasons": 20, "new_items": 0}
        svc._emit_stats()
        cb.assert_called_once_with({"plex_1080": 10, "plex_4k": 5, "tv_seasons": 20, "new_items": 0})

    def test_no_callback_noop(self):
        svc = _make_service()
        svc._emit_stats()  # should not raise

    def test_callback_exception_suppressed(self):
        svc = _make_service()
        svc.set_stats_callback(MagicMock(side_effect=RuntimeError("fail")))
        svc._emit_stats()  # should not raise


# ======================================================================
# connect
# ======================================================================

class TestConnect:
    def test_direct_mode_success(self):
        pm = MagicMock()
        pm.connect.return_value = (True, "Connected")
        pm.get_server_info.return_value = {"machine_id": "abc123"}
        pm._server_name = "MyServer"

        config = {
            "plex_connection_mode": "direct",
            "plex_url": "http://plex:32400",
            "plex_token": "mytoken",
            "plex_username": "",
            "plex_password": "",
            "plex_server_name": "",
        }
        svc = _make_service(config=config, plex_manager=pm)
        success, msg = svc.connect()

        assert success is True
        assert msg == "Connected"
        assert svc.config["plex_server_id"] == "abc123"
        pm.configure.assert_called_once()

    def test_direct_mode_missing_url(self):
        config = {
            "plex_connection_mode": "direct",
            "plex_url": "",
            "plex_token": "",
            "plex_username": "",
            "plex_password": "",
            "plex_server_name": "",
        }
        svc = _make_service(config=config)
        success, msg = svc.connect()
        assert success is False
        assert "not configured" in msg

    def test_account_mode_success(self):
        pm = MagicMock()
        pm.connect.return_value = (True, "Connected via account")
        pm.get_server_info.return_value = {"machine_id": "xyz789"}
        pm._server_name = "AccountServer"

        config = {
            "plex_connection_mode": "account",
            "plex_url": "",
            "plex_token": "",
            "plex_username": "user",
            "plex_password": "pass",
            "plex_server_name": "",
        }
        svc = _make_service(config=config, plex_manager=pm)
        success, msg = svc.connect()

        assert success is True
        assert svc.config["plex_server_id"] == "xyz789"
        assert svc.config["plex_server_name"] == "AccountServer"

    def test_account_mode_missing_credentials(self):
        config = {
            "plex_connection_mode": "account",
            "plex_url": "",
            "plex_token": "",
            "plex_username": "",
            "plex_password": "",
            "plex_server_name": "",
        }
        svc = _make_service(config=config)
        success, msg = svc.connect()
        assert success is False
        assert "username/password" in msg

    def test_connection_failure(self):
        pm = MagicMock()
        pm.connect.return_value = (False, "Connection refused")

        config = {
            "plex_connection_mode": "direct",
            "plex_url": "http://plex:32400",
            "plex_token": "token",
            "plex_username": "",
            "plex_password": "",
            "plex_server_name": "",
        }
        svc = _make_service(config=config, plex_manager=pm)
        success, msg = svc.connect()
        assert success is False
        assert "Connection refused" in msg

    def test_server_info_none_gives_empty_id(self):
        pm = MagicMock()
        pm.connect.return_value = (True, "Connected")
        pm.get_server_info.return_value = None
        pm._server_name = ""

        config = {
            "plex_connection_mode": "direct",
            "plex_url": "http://plex:32400",
            "plex_token": "token",
            "plex_username": "",
            "plex_password": "",
            "plex_server_name": "",
        }
        svc = _make_service(config=config, plex_manager=pm)
        success, _ = svc.connect()
        assert success is True
        assert svc.config["plex_server_id"] == ""


# ======================================================================
# load_libraries
# ======================================================================

class TestLoadLibraries:
    def test_not_connected_returns_immediately(self):
        pm = MagicMock()
        pm.is_connected = False
        svc = _make_service(plex_manager=pm)
        svc.load_libraries()
        # No library data should be loaded
        assert svc.plex_movies == []

    def test_already_loading_skips(self):
        pm = MagicMock()
        pm.is_connected = True
        svc = _make_service(plex_manager=pm)
        svc._plex_loading = True
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.load_libraries(wait_if_loading=False)
        # Should log that it's skipping
        assert any("already in progress" in str(c) for c in cb.call_args_list)

    def test_cache_path_loads_from_cache(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()
        cached_movies = [
            {"clean_title": "test", "res": "4K", "imdb_id": "tt123", "rating_key": "1"},
            {"clean_title": "test2", "res": "1080p", "imdb_id": None, "rating_key": "2"},
        ]
        cached_tv = [
            {"clean_title": "show", "season": 1, "imdb_id": "tt456", "rating_key": "3"},
        ]
        db.load_plex_cache.side_effect = lambda mode: cached_movies if mode == "Movies" else cached_tv

        svc = _make_service(config={}, db=db, plex_manager=pm)
        cb = MagicMock()
        svc.set_stats_callback(cb)

        svc.load_libraries(use_cache=True)

        assert svc.plex_movies == cached_movies
        assert svc.plex_tv == cached_tv
        assert svc.stats["plex_4k"] == 1
        assert svc.stats["plex_1080"] == 1
        assert svc.stats["tv_seasons"] == 1
        cb.assert_called()

    def test_cache_empty_falls_back_to_full_load(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()
        db.load_plex_cache.return_value = []

        # Mock library sections
        mock_lib = MagicMock()
        movie = _make_mock_movie()
        mock_lib.all.return_value = [movie]
        pm.get_library_section.return_value = mock_lib

        config = {
            "movie_libs": ["Movies"],
            "tv_libs": [],
        }
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries(use_cache=True)

        # Should have loaded from the library
        assert len(svc.plex_movies) > 0
        db.save_plex_cache.assert_called()

    def test_full_load_movies(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        movie_1080 = _make_mock_movie(title="Movie A", rating_key=1, video_resolution="1080")
        movie_4k = _make_mock_movie(title="Movie B", rating_key=2, video_resolution="4k")

        mock_lib = MagicMock()
        mock_lib.all.return_value = [movie_1080, movie_4k]
        pm.get_library_section.return_value = mock_lib

        config = {
            "movie_libs": ["Movies"],
            "tv_libs": [],
        }
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        assert len(svc.plex_movies) == 2
        assert svc.stats["plex_1080"] == 1
        assert svc.stats["plex_4k"] == 1

    def test_full_load_tv_shows(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        season1 = _make_mock_season(index=1, rating_key=201)
        season2 = _make_mock_season(index=2, rating_key=202)
        show = _make_mock_show(title="Breaking Bad", seasons=[season1, season2])

        mock_lib = MagicMock()
        mock_lib.all.return_value = [show]
        mock_lib.type = "show"
        pm.get_library_section.return_value = mock_lib

        config = {
            "movie_libs": [],
            "tv_libs": ["TV Shows"],
        }
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        assert len(svc.plex_tv) == 2
        assert svc.stats["tv_seasons"] == 2

    def test_tv_specials_skipped(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        specials = _make_mock_season(index=0, rating_key=200)
        show = _make_mock_show(seasons=[specials])

        mock_lib = MagicMock()
        mock_lib.all.return_value = [show]
        mock_lib.type = "show"
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": [], "tv_libs": ["TV"]}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        assert svc.stats["tv_seasons"] == 0

    def test_library_not_found(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()
        pm.get_library_section.return_value = None

        config = {"movie_libs": ["Nonexistent"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.load_libraries()

        assert svc.plex_movies == []
        assert any("not found" in str(c) for c in cb.call_args_list)

    def test_movie_library_exception(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()
        pm.get_library_section.side_effect = RuntimeError("lib error")

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()  # should not raise

    def test_tv_show_exception_logged(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        show = MagicMock()
        show.title = "Bad Show"
        show.seasons.side_effect = RuntimeError("seasons fail")

        mock_lib = MagicMock()
        mock_lib.all.return_value = [show]
        mock_lib.type = "show"
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": [], "tv_libs": ["TV"]}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.load_libraries()

        # Should log diagnostics about error
        assert any("errored" in str(c) or "error" in str(c).lower() for c in cb.call_args_list)

    def test_tv_no_seasons_show(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        show = _make_mock_show(seasons=[])

        mock_lib = MagicMock()
        mock_lib.all.return_value = [show]
        mock_lib.type = "show"
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": [], "tv_libs": ["TV"]}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()
        assert svc.stats["tv_seasons"] == 0

    def test_progress_callback_called(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        mock_lib = MagicMock()
        mock_lib.all.return_value = []
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        progress = MagicMock()
        svc.load_libraries(progress_callback=progress)
        progress.assert_called()

    def test_duplicate_movie_skipped(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        movie = _make_mock_movie(title="Same Movie", rating_key=42)

        mock_lib = MagicMock()
        mock_lib.all.return_value = [movie, movie]  # same ratingKey twice
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        assert len(svc.plex_movies) == 1

    def test_saves_to_cache(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        mock_lib = MagicMock()
        mock_lib.all.return_value = [_make_mock_movie()]
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        # Movies has items → saved. TV is empty → skipped, so an empty load
        # can't clobber a valid TV cache via full_replace.
        saved_modes = [c.args[1] for c in db.save_plex_cache.call_args_list]
        assert saved_modes == ["Movies"]

    def test_partial_movie_library_load_does_not_full_replace_cache(self):
        # A connection drop mid-library (e.g. the 2nd of 3 movies raises
        # while iterating, as would happen if Plex drops the connection
        # partway through) must NOT be treated as a complete load. The
        # per-library except already swallows the error and moves on, but
        # the resulting partial _movies list must not be persisted with
        # full_replace=True — that would wipe a good existing cache with an
        # incomplete one. Existing cache should be left untouched instead.
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        good_movie = _make_mock_movie(title="Movie A", rating_key=1)

        class ExplodingMovie:
            """Raises when Plex connection drops mid-iteration."""
            @property
            def ratingKey(self):
                raise ConnectionError("Plex connection lost")

        mock_lib = MagicMock()
        mock_lib.all.return_value = [good_movie, ExplodingMovie()]
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        # The partial set (1 of 2 movies) must not be full-replace saved.
        movie_saves = [c for c in db.save_plex_cache.call_args_list if c.args[1] == "Movies"]
        assert not any(c.kwargs.get("full_replace") or (len(c.args) > 3 and c.args[3])
                       for c in movie_saves), (
            "Partial library load must not full_replace the cache"
        )

    def test_complete_movie_library_load_still_full_replaces_cache(self):
        # Sanity/regression companion: an uninterrupted, complete load must
        # still behave exactly as before — full_replace=True so stale rows
        # get pruned.
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        mock_lib = MagicMock()
        mock_lib.all.return_value = [_make_mock_movie()]
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        movie_saves = [c for c in db.save_plex_cache.call_args_list if c.args[1] == "Movies"]
        assert len(movie_saves) == 1
        assert movie_saves[0].kwargs.get("full_replace") is True

    def test_non_show_library_type_logs_error(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        mock_lib = MagicMock()
        mock_lib.all.return_value = []
        mock_lib.type = "movie"  # wrong type for TV library
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": [], "tv_libs": ["Wrong Type"]}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.load_libraries()

        assert any("not 'show'" in str(c) for c in cb.call_args_list)

    def test_tv_zero_seasons_logs_warning(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        mock_lib = MagicMock()
        mock_lib.all.return_value = []
        mock_lib.type = "show"
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": [], "tv_libs": ["TV Shows"]}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.load_libraries()

        assert any("No TV seasons loaded" in str(c) for c in cb.call_args_list)

    def test_loading_sets_and_clears_flag(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()

        mock_lib = MagicMock()
        mock_lib.all.return_value = []
        pm.get_library_section.return_value = mock_lib

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()

        # After completion, loading flag should be cleared
        assert svc._plex_loading is False

    def test_load_exception_clears_loading_flag(self):
        pm = MagicMock()
        pm.is_connected = True
        db = MagicMock()
        # Force an error during movie load processing
        pm.get_library_section.side_effect = RuntimeError("boom")

        config = {"movie_libs": ["Movies"], "tv_libs": []}
        svc = _make_service(config=config, db=db, plex_manager=pm)
        svc.load_libraries()  # should not raise
        assert svc._plex_loading is False


# ======================================================================
# _extract_movie_data
# ======================================================================

class TestExtractMovieData:
    def test_basic_1080p_movie(self):
        svc = _make_service()
        movie = _make_mock_movie(title="Inception", year=2010,
                                 rating_key=1, video_resolution="1080",
                                 size=10_737_418_240)  # 10 GB
        result = svc._extract_movie_data(movie)

        assert result is not None
        assert len(result) == 1
        data = result[0]
        assert data["original_title"] == "Inception"
        assert data["year"] == 2010
        assert data["res"] == "1080p"
        assert data["size"] == 10.0  # 10 GB
        assert data["imdb_id"] == "tt1234567"
        assert data["rating_key"] == 1

    def test_4k_movie(self):
        svc = _make_service()
        movie = _make_mock_movie(video_resolution="4k")
        result = svc._extract_movie_data(movie)
        assert result[0]["res"] == "4K"

    def test_2160_resolution(self):
        svc = _make_service()
        movie = _make_mock_movie(video_resolution="2160")
        result = svc._extract_movie_data(movie)
        assert result[0]["res"] == "4K"

    def test_720p_resolution(self):
        svc = _make_service()
        movie = _make_mock_movie(video_resolution="720")
        result = svc._extract_movie_data(movie)
        assert result[0]["res"] == "720p"

    def test_unknown_resolution(self):
        svc = _make_service()
        movie = _make_mock_movie(video_resolution=None)
        result = svc._extract_movie_data(movie)
        assert result[0]["res"] == "?"

    def test_no_media_returns_none(self):
        svc = _make_service()
        movie = MagicMock()
        movie.media = []
        result = svc._extract_movie_data(movie)
        assert result is None

    def test_media_is_none_returns_none(self):
        svc = _make_service()
        movie = MagicMock()
        movie.media = None
        result = svc._extract_movie_data(movie)
        assert result is None

    def test_multiple_media_versions(self):
        svc = _make_service()

        stream1 = MagicMock()
        stream1.colorPrimaries = ""
        stream1._data = {}
        stream1.displayTitle = ""
        stream1.title = ""
        stream1.profile = ""
        stream1.codec = "hevc"
        # Remove dovi attrs so _check_dovi returns False
        for attr in ('DOVIPresent', 'DOViProfile', 'doviProfile'):
            if hasattr(stream1, attr):
                delattr(stream1, attr)

        part1 = MagicMock()
        part1.size = 5_368_709_120
        part1.videoStreams.return_value = [stream1]
        media1 = MagicMock()
        media1.videoResolution = "1080"
        media1.parts = [part1]

        stream2 = MagicMock()
        stream2.colorPrimaries = ""
        stream2._data = {}
        stream2.displayTitle = ""
        stream2.title = ""
        stream2.profile = ""
        stream2.codec = "hevc"
        for attr in ('DOVIPresent', 'DOViProfile', 'doviProfile'):
            if hasattr(stream2, attr):
                delattr(stream2, attr)

        part2 = MagicMock()
        part2.size = 53_687_091_200
        part2.videoStreams.return_value = [stream2]
        media2 = MagicMock()
        media2.videoResolution = "4k"
        media2.parts = [part2]

        movie = _make_mock_movie(media_list=[media1, media2])
        result = svc._extract_movie_data(movie)

        assert len(result) == 2
        assert result[0]["res"] == "1080p"
        assert result[1]["res"] == "4K"

    def test_dovi_detected(self):
        svc = _make_service()
        movie = _make_mock_movie(dovi=True)
        result = svc._extract_movie_data(movie)
        assert result[0]["dovi"] is True

    def test_hdr_detected_via_color_primaries(self):
        svc = _make_service()
        movie = _make_mock_movie(color_primaries="BT2020")
        result = svc._extract_movie_data(movie)
        assert result[0]["hdr"] is True

    def test_no_guids_imdb_none(self):
        svc = _make_service()
        movie = _make_mock_movie(guids=[])
        result = svc._extract_movie_data(movie)
        assert result[0]["imdb_id"] is None

    def test_non_imdb_guids_ignored(self):
        svc = _make_service()
        guid = MagicMock()
        guid.id = "tmdb://12345"
        movie = _make_mock_movie(guids=[guid])
        result = svc._extract_movie_data(movie)
        assert result[0]["imdb_id"] is None

    def test_media_with_no_parts_skipped(self):
        svc = _make_service()
        media = MagicMock()
        media.videoResolution = "1080"
        media.parts = []

        movie = _make_mock_movie(media_list=[media])
        result = svc._extract_movie_data(movie)
        # Media with no parts is skipped entirely (nothing to serve/label) —
        # no rows are produced, so the extractor returns None.
        assert result is None

    def test_exception_returns_none(self):
        svc = _make_service()
        movie = MagicMock()
        movie.title = "Error Movie"
        movie.media = MagicMock(side_effect=RuntimeError("fail"))
        result = svc._extract_movie_data(movie)
        assert result is None

    def test_reload_for_4k_with_empty_streams(self):
        """If 4K media has no video streams, movie should be reloaded."""
        svc = _make_service()

        stream = MagicMock()
        stream.colorPrimaries = ""
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        for attr in ('DOVIPresent', 'DOViProfile', 'doviProfile'):
            if hasattr(stream, attr):
                delattr(stream, attr)

        part = MagicMock()
        part.size = 53_687_091_200
        # First call to videoStreams returns empty (triggering reload), second returns streams
        part.videoStreams.side_effect = [[], [stream]]

        media = MagicMock()
        media.videoResolution = "4k"
        media.parts = [part]

        movie = _make_mock_movie(media_list=[media])
        result = svc._extract_movie_data(movie)
        movie.reload.assert_called_once()

    def test_movie_year_none_defaults_to_zero(self):
        svc = _make_service()
        movie = _make_mock_movie(year=None)
        result = svc._extract_movie_data(movie)
        assert result[0]["year"] == 0


# ======================================================================
# _extract_season_data
# ======================================================================

class TestExtractSeasonData:
    def test_basic_season(self):
        svc = _make_service()
        show = _make_mock_show()
        season = _make_mock_season(index=1, rating_key=201)
        result = svc._extract_season_data(show, season)

        assert result is not None
        assert result["clean_title"] == _clean_string("Test Show")
        assert result["original_title"] == "Test Show"
        assert result["season"] == 1
        assert result["episode_count"] == 1
        assert result["res"] == "1080p"

    def test_no_episodes_returns_none(self):
        svc = _make_service()
        show = _make_mock_show()
        season = _make_mock_season()
        season.episodes.return_value = []
        result = svc._extract_season_data(show, season)
        assert result is None

    def test_4k_episodes(self):
        svc = _make_service()
        ep = MagicMock()
        stream = MagicMock()
        stream.colorPrimaries = ""
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        for attr in ('DOVIPresent', 'DOViProfile', 'doviProfile'):
            if hasattr(stream, attr):
                delattr(stream, attr)

        part = MagicMock()
        part.size = 10_737_418_240
        part.videoStreams.return_value = [stream]
        media = MagicMock()
        media.videoResolution = "4k"
        media.parts = [part]
        ep.media = [media]

        show = _make_mock_show()
        season = _make_mock_season(episodes=[ep])
        result = svc._extract_season_data(show, season)
        assert result["res"] == "4K"

    def test_total_size_accumulated(self):
        svc = _make_service()

        episodes = []
        for _ in range(5):
            ep = MagicMock()
            stream = MagicMock()
            stream.colorPrimaries = ""
            stream._data = {}
            stream.displayTitle = ""
            stream.title = ""
            stream.profile = ""
            stream.codec = "hevc"
            for attr in ('DOVIPresent', 'DOViProfile', 'doviProfile'):
                if hasattr(stream, attr):
                    delattr(stream, attr)
            part = MagicMock()
            part.size = 2_147_483_648  # 2 GB each
            part.videoStreams.return_value = [stream]
            media = MagicMock()
            media.videoResolution = "1080"
            media.parts = [part]
            ep.media = [media]
            episodes.append(ep)

        show = _make_mock_show()
        season = _make_mock_season(episodes=episodes)
        result = svc._extract_season_data(show, season)
        assert result["episode_count"] == 5
        assert result["size"] == 10.0  # 5 * 2 GB

    def test_imdb_id_extraction(self):
        svc = _make_service()
        show = _make_mock_show()
        season = _make_mock_season()
        result = svc._extract_season_data(show, season)
        assert result["imdb_id"] == "tt9876543"

    def test_no_imdb_guid(self):
        svc = _make_service()
        guid = MagicMock()
        guid.id = "tmdb://999"
        show = _make_mock_show(guids=[guid])
        season = _make_mock_season()
        result = svc._extract_season_data(show, season)
        assert result["imdb_id"] is None

    def test_exception_returns_none(self):
        svc = _make_service()
        show = _make_mock_show()
        season = MagicMock()
        season.index = 1
        season.episodes.side_effect = RuntimeError("fail")
        result = svc._extract_season_data(show, season)
        assert result is None

    def test_episode_no_media(self):
        svc = _make_service()
        ep = MagicMock()
        ep.media = []

        show = _make_mock_show()
        season = _make_mock_season(episodes=[ep])
        result = svc._extract_season_data(show, season)
        assert result is not None
        assert result["res"] == "?"
        assert result["size"] == 0.0


# ======================================================================
# _check_dovi
# ======================================================================

class TestCheckDovi:
    def test_dovi_present_true(self):
        stream = MagicMock(spec=[])
        stream.DOVIPresent = "true"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_present_one(self):
        stream = MagicMock(spec=[])
        stream.DOVIPresent = "1"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_present_false(self):
        stream = MagicMock(spec=[])
        stream.DOVIPresent = "false"
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is False

    def test_dovi_via_data_dict(self):
        stream = MagicMock(spec=[])
        stream._data = {"DOVIPresent": "True"}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_data_dict_dovipresent_lowercase(self):
        stream = MagicMock(spec=[])
        stream._data = {"dovipresent": "1"}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_dovi_profile_attr(self):
        stream = MagicMock(spec=[])
        stream.DOViProfile = "dvhe.05"
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_doviProfile_lowercase_attr(self):
        stream = MagicMock(spec=[])
        stream.doviProfile = "dvhe.08"
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_data_keys_doviprofile(self):
        stream = MagicMock(spec=[])
        stream._data = {"doviprofile": "dvhe.05"}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_data_keys_dovilevel(self):
        stream = MagicMock(spec=[])
        stream._data = {"dovilevel": "5"}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_data_keys_doviblpresent(self):
        stream = MagicMock(spec=[])
        stream._data = {"doviblpresent": "true"}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_data_keys_dovielpresent(self):
        stream = MagicMock(spec=[])
        stream._data = {"dovielpresent": "true"}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_display_title(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = "HEVC DoVi"
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_dolby_vision_display_title(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = "4K (HEVC Dolby Vision)"
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_title_dv_word(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = ""
        stream.title = "DV HDR10"
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_profile(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = "dv_hevc"
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_dolby_vision_profile(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = "dolby vision"
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is True

    def test_dovi_via_codec(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "dvhe"
        assert PlexService._check_dovi(stream) is True

    def test_no_dovi_detected(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = "4K HEVC HDR10"
        stream.title = ""
        stream.profile = "main 10"
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is False

    def test_data_is_not_dict(self):
        """When _data is not a dict, should skip that check gracefully."""
        stream = MagicMock(spec=[])
        stream._data = "not a dict"
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = ""
        stream.codec = "hevc"
        assert PlexService._check_dovi(stream) is False

    def test_none_profile_and_codec(self):
        stream = MagicMock(spec=[])
        stream._data = {}
        stream.displayTitle = ""
        stream.title = ""
        stream.profile = None
        stream.codec = None
        assert PlexService._check_dovi(stream) is False


# ======================================================================
# _build_plex_index
# ======================================================================

class TestBuildPlexIndex:
    def test_index_by_imdb(self):
        svc = _make_service()
        svc.plex_movies = [
            {"clean_title": "inception", "imdb_id": "tt1375666", "res": "4K"},
        ]
        svc.plex_tv = []
        svc._build_plex_index()

        assert "tt1375666" in svc.plex_index["by_imdb"]
        assert len(svc.plex_index["by_imdb"]["tt1375666"]) == 1

    def test_index_by_title(self):
        svc = _make_service()
        svc.plex_movies = [
            {"clean_title": "inception", "imdb_id": None, "res": "1080p"},
        ]
        svc.plex_tv = []
        svc._build_plex_index()

        assert "inception" in svc.plex_index["by_title"]

    def test_index_all_items(self):
        svc = _make_service()
        svc.plex_movies = [{"clean_title": "m1", "imdb_id": None}]
        svc.plex_tv = [{"clean_title": "t1", "imdb_id": None}]
        svc._build_plex_index()

        assert len(svc.plex_index["all_items"]) == 2

    def test_empty_index(self):
        svc = _make_service()
        svc.plex_movies = []
        svc.plex_tv = []
        svc._build_plex_index()

        assert svc.plex_index["by_imdb"] == {}
        assert svc.plex_index["by_title"] == {}
        assert svc.plex_index["all_items"] == []

    def test_no_imdb_not_indexed_by_imdb(self):
        svc = _make_service()
        svc.plex_movies = [{"clean_title": "test", "imdb_id": None}]
        svc.plex_tv = []
        svc._build_plex_index()

        assert len(svc.plex_index["by_imdb"]) == 0

    def test_no_title_not_indexed_by_title(self):
        svc = _make_service()
        svc.plex_movies = [{"clean_title": "", "imdb_id": "tt123"}]
        svc.plex_tv = []
        svc._build_plex_index()

        assert len(svc.plex_index["by_title"]) == 0

    def test_multiple_items_same_imdb(self):
        svc = _make_service()
        svc.plex_movies = [
            {"clean_title": "movie", "imdb_id": "tt111", "res": "1080p"},
            {"clean_title": "movie", "imdb_id": "tt111", "res": "4K"},
        ]
        svc.plex_tv = []
        svc._build_plex_index()

        assert len(svc.plex_index["by_imdb"]["tt111"]) == 2


# ======================================================================
# check_cache_status
# ======================================================================

class TestCheckCacheStatus:
    def test_empty_cache(self):
        db = MagicMock()
        db.get_plex_cache_max_timestamp.return_value = {}
        svc = _make_service(db=db)
        valid, msg = svc.check_cache_status()
        assert valid is False
        assert "not found" in msg.lower() or "not found" in msg

    def test_valid_cache(self):
        db = MagicMock()
        now = time.time()
        db.get_plex_cache_max_timestamp.return_value = {
            "Movies": now, "TV Shows": now,
        }
        pm = MagicMock()
        pm.is_connected = False  # Skip recently-added check
        svc = _make_service(config={"cache_duration": 4}, db=db, plex_manager=pm)
        valid, msg = svc.check_cache_status()
        assert valid is True
        assert msg == ""

    def test_expired_cache(self):
        db = MagicMock()
        old_time = time.time() - 20 * 3600  # 20 hours ago
        db.get_plex_cache_max_timestamp.return_value = {
            "Movies": old_time, "TV Shows": old_time,
        }
        svc = _make_service(config={"cache_duration": 4}, db=db)
        valid, msg = svc.check_cache_status()
        assert valid is False
        assert "expired" in msg.lower()

    def test_cache_no_timestamp(self):
        """No timestamps → cache not found."""
        db = MagicMock()
        db.get_plex_cache_max_timestamp.return_value = {}
        svc = _make_service(config={"cache_duration": 4}, db=db)
        valid, msg = svc.check_cache_status()
        assert valid is False

    def test_cache_check_exception(self):
        db = MagicMock()
        db.get_plex_cache_max_timestamp.side_effect = RuntimeError("db error")
        svc = _make_service(db=db)
        valid, msg = svc.check_cache_status()
        assert valid is False
        assert "failed" in msg.lower()

    def test_movies_valid_tv_expired(self):
        """If movies are valid but TV is expired, cache is invalid."""
        db = MagicMock()
        now = time.time()
        old_time = time.time() - 20 * 3600

        db.get_plex_cache_max_timestamp.return_value = {
            "Movies": now,
            "TV Shows": old_time,
        }
        svc = _make_service(config={"cache_duration": 4}, db=db)
        valid, msg = svc.check_cache_status()
        assert valid is False

    def test_movies_empty_tv_has_data(self):
        """If movies cache is empty but TV has data, cache is still valid."""
        db = MagicMock()
        now = time.time()

        # Only TV Shows has a timestamp — Movies is absent (single-type setup)
        db.get_plex_cache_max_timestamp.return_value = {"TV Shows": now}
        pm = MagicMock()
        pm.is_connected = False  # Skip recently-added check
        svc = _make_service(config={"cache_duration": 4}, db=db, plex_manager=pm)
        valid, msg = svc.check_cache_status()
        assert valid is True


# ======================================================================
# wait_if_loading behavior
# ======================================================================

class TestWaitIfLoading:
    def test_already_loading_with_wait(self):
        """When already loading and wait_if_loading=True, should wait for lock."""
        pm = MagicMock()
        pm.is_connected = True
        svc = _make_service(plex_manager=pm)
        svc._plex_loading = True
        cb = MagicMock()
        svc.set_log_callback(cb)

        svc.load_libraries(wait_if_loading=True)
        assert any("Waiting" in str(c) for c in cb.call_args_list)
