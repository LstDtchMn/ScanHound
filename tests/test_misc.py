"""Tests for miscellaneous backend modules.

Covers:
- backend/sources/base.py  (SourceCapability, SourceConfig, ParsedRelease)
- backend/notification_bridge.py  (NotificationBridge)
- backend/plex_manager.py  (LibraryType, PlexLibrary, PathMapping)
- backend/tmdb_client.py  (TmdbClient init / attributes)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import sys, os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.sources.base import SourceCapability, SourceConfig, ParsedRelease
from backend.notification_bridge import NotificationBridge
from backend.plex_manager import LibraryType, PlexLibrary, PathMapping


# ===================================================================
# SourceCapability
# ===================================================================

class TestSourceCapability:

    def test_single_flag(self):
        cap = SourceCapability.MOVIES
        assert SourceCapability.MOVIES in cap

    def test_combined_flags(self):
        cap = SourceCapability.MOVIES | SourceCapability.TV_SHOWS
        assert SourceCapability.MOVIES in cap
        assert SourceCapability.TV_SHOWS in cap

    def test_combined_does_not_include_other(self):
        cap = SourceCapability.MOVIES | SourceCapability.TV_SHOWS
        assert SourceCapability.SEARCH not in cap

    def test_all_flags_combine(self):
        cap = (
            SourceCapability.MOVIES
            | SourceCapability.TV_SHOWS
            | SourceCapability.PAGINATION
            | SourceCapability.SEARCH
            | SourceCapability.RSS
            | SourceCapability.API
        )
        assert SourceCapability.RSS in cap
        assert SourceCapability.PAGINATION in cap


# ===================================================================
# SourceConfig
# ===================================================================

class TestSourceConfig:

    def test_defaults(self):
        cfg = SourceConfig(name="test", display_name="Test", base_url="https://example.com")
        assert cfg.rate_limit == 2.0
        assert cfg.requires_auth is False
        assert cfg.requires_cloudflare_bypass is False
        assert cfg.enabled is True
        assert cfg.priority == 100
        assert cfg.custom_headers == {}
        assert cfg.timeout == 30

    def test_default_capabilities(self):
        cfg = SourceConfig(name="x", display_name="X", base_url="https://x.com")
        assert SourceCapability.MOVIES in cfg.capabilities
        assert SourceCapability.TV_SHOWS in cfg.capabilities

    def test_custom_values(self):
        cfg = SourceConfig(
            name="custom",
            display_name="Custom",
            base_url="https://custom.com",
            rate_limit=5.0,
            requires_auth=True,
            timeout=60,
        )
        assert cfg.rate_limit == 5.0
        assert cfg.requires_auth is True
        assert cfg.timeout == 60


# ===================================================================
# ParsedRelease
# ===================================================================

class TestParsedRelease:

    def test_defaults(self):
        pr = ParsedRelease(title="Test", url="https://x.com", source="src")
        assert pr.year == 0
        assert pr.resolution == ""
        assert pr.is_hdr is False
        assert pr.is_dovi is False
        assert pr.is_tv is False
        assert pr.season is None
        assert pr.episode is None
        assert pr.raw_data == {}

    def test_post_init_display_title_defaults_to_title(self):
        pr = ParsedRelease(title="My Movie", url="u", source="s")
        assert pr.display_title == "My Movie"

    def test_post_init_search_key_generated(self):
        pr = ParsedRelease(title="Spider-Man: No Way Home", url="u", source="s")
        assert pr.search_key != ""
        assert "spider" in pr.search_key

    def test_post_init_explicit_display_title(self):
        pr = ParsedRelease(title="raw", url="u", source="s", display_title="Pretty Title")
        assert pr.display_title == "Pretty Title"

    def test_to_dict_keys(self):
        pr = ParsedRelease(title="Test", url="https://x.com", source="src", year=2023)
        d = pr.to_dict()
        assert d["display_title"] == "Test"
        assert d["url"] == "https://x.com"
        assert d["year"] == 2023
        assert d["source"] == "src"
        assert "res" in d  # resolution mapped to 'res'

    def test_normalize_removes_year_parens(self):
        pr = ParsedRelease(title="Movie (2023) Special", url="u", source="s")
        # search_key should not contain the year in parens
        assert "2023" not in pr.search_key


# ===================================================================
# NotificationBridge
# ===================================================================

class TestNotificationBridge:

    def test_init_attributes(self):
        nb = NotificationBridge()
        assert nb._manager is None
        assert nb._loop is None
        assert nb._thread is None

    def test_send_without_configure_does_not_crash(self):
        nb = NotificationBridge()
        # Should silently return because _manager and _loop are None
        nb.send("info", "Title", "Message")

    def test_notify_scan_complete_without_configure(self):
        nb = NotificationBridge()
        nb.notify_scan_complete(total=10, missing=2, upgrades=1)

    def test_notify_error_without_configure(self):
        nb = NotificationBridge()
        nb.notify_error("something went wrong")

    def test_shutdown_without_configure(self):
        nb = NotificationBridge()
        nb.shutdown()
        assert nb._manager is None
        assert nb._loop is None


# ===================================================================
# LibraryType
# ===================================================================

class TestLibraryType:

    def test_from_plex_type_movie(self):
        assert LibraryType.from_plex_type("movie") == LibraryType.MOVIE

    def test_from_plex_type_show(self):
        assert LibraryType.from_plex_type("show") == LibraryType.SHOW

    def test_from_plex_type_artist(self):
        assert LibraryType.from_plex_type("artist") == LibraryType.MUSIC

    def test_from_plex_type_photo(self):
        assert LibraryType.from_plex_type("photo") == LibraryType.PHOTO

    def test_from_plex_type_unknown_fallback(self):
        assert LibraryType.from_plex_type("clip") == LibraryType.OTHER

    def test_from_plex_type_case_insensitive(self):
        assert LibraryType.from_plex_type("Movie") == LibraryType.MOVIE
        assert LibraryType.from_plex_type("SHOW") == LibraryType.SHOW


# ===================================================================
# PlexLibrary serialization round-trip
# ===================================================================

class TestPlexLibrary:

    def _make_lib(self, **overrides):
        defaults = dict(
            key="1",
            title="Movies",
            type=LibraryType.MOVIE,
            scanner="Plex Movie",
            agent="tv.plex.agents.movie",
            location=["/media/movies"],
            item_count=42,
            last_scanned=datetime(2025, 1, 15, 10, 30, tzinfo=timezone.utc),
            uuid="abc-123",
        )
        defaults.update(overrides)
        return PlexLibrary(**defaults)

    def test_to_dict_keys(self):
        lib = self._make_lib()
        d = lib.to_dict()
        assert d["key"] == "1"
        assert d["title"] == "Movies"
        assert d["type"] == "movie"
        assert d["item_count"] == 42
        assert "abc-123" == d["uuid"]

    def test_from_dict_basic(self):
        data = {
            "key": "2",
            "title": "TV Shows",
            "type": "show",
            "scanner": "Plex TV",
            "agent": "tv.plex.agents.series",
            "location": ["/media/tv"],
            "item_count": 10,
            "last_scanned": None,
            "uuid": "def-456",
        }
        lib = PlexLibrary.from_dict(data)
        assert lib.key == "2"
        assert lib.type == LibraryType.SHOW
        assert lib.item_count == 10
        assert lib.last_scanned is None

    def test_round_trip(self):
        original = self._make_lib()
        d = original.to_dict()
        restored = PlexLibrary.from_dict(d)
        assert restored.key == original.key
        assert restored.title == original.title
        assert restored.type == original.type
        assert restored.scanner == original.scanner
        assert restored.location == original.location
        assert restored.item_count == original.item_count
        assert restored.uuid == original.uuid

    def test_round_trip_preserves_last_scanned(self):
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        original = self._make_lib(last_scanned=ts)
        d = original.to_dict()
        restored = PlexLibrary.from_dict(d)
        assert restored.last_scanned == ts


# ===================================================================
# PathMapping
# ===================================================================

class TestPathMapping:

    def test_translate_matching_prefix(self):
        pm = PathMapping(plex_path="/data/media", local_path="/mnt/nas")
        result = pm.translate("/data/media/movies/film.mkv")
        assert result == "/mnt/nas/movies/film.mkv"

    def test_translate_no_match(self):
        pm = PathMapping(plex_path="/data/media", local_path="/mnt/nas")
        result = pm.translate("/other/path/file.mkv")
        assert result == "/other/path/file.mkv"

    def test_translate_disabled(self):
        pm = PathMapping(plex_path="/data/media", local_path="/mnt/nas", enabled=False)
        result = pm.translate("/data/media/movies/film.mkv")
        # Disabled mapping returns path unchanged
        assert result == "/data/media/movies/film.mkv"

    def test_translate_only_first_occurrence(self):
        pm = PathMapping(plex_path="/data", local_path="/mnt")
        result = pm.translate("/data/data/file.mkv")
        # replace(... , 1) means only first match
        assert result == "/mnt/data/file.mkv"

    def test_translate_exact_path(self):
        pm = PathMapping(plex_path="/data", local_path="/mnt")
        result = pm.translate("/data")
        assert result == "/mnt"


# ===================================================================
# TmdbClient
# ===================================================================

class TestTmdbClient:

    def test_init_sets_api_key(self):
        # Patch TMDB_API_BASE so import doesn't fail
        with patch("backend.tmdb_client.TMDB_API_BASE", "https://fake.api"):
            from backend.tmdb_client import TmdbClient
            client = TmdbClient(api_key="test_key_123")
            assert client.api_key == "test_key_123"

    def test_init_sets_timeout(self):
        with patch("backend.tmdb_client.TMDB_API_BASE", "https://fake.api"):
            from backend.tmdb_client import TmdbClient
            client = TmdbClient(api_key="k", timeout=30)
            assert client.timeout == 30

    def test_init_sets_max_retries(self):
        with patch("backend.tmdb_client.TMDB_API_BASE", "https://fake.api"):
            from backend.tmdb_client import TmdbClient
            client = TmdbClient(api_key="k", max_retries=5)
            assert client.max_retries == 5

    def test_rate_limit_interval_default(self):
        with patch("backend.tmdb_client.TMDB_API_BASE", "https://fake.api"):
            from backend.tmdb_client import TmdbClient
            client = TmdbClient(api_key="k")
            assert client._rate_limit_interval == 0.20

    def test_rate_limit_interval_custom(self):
        with patch("backend.tmdb_client.TMDB_API_BASE", "https://fake.api"):
            from backend.tmdb_client import TmdbClient
            client = TmdbClient(api_key="k", rate_limit=0.5)
            assert client._rate_limit_interval == 0.5

    def test_lock_exists(self):
        with patch("backend.tmdb_client.TMDB_API_BASE", "https://fake.api"):
            from backend.tmdb_client import TmdbClient
            client = TmdbClient(api_key="k")
            assert hasattr(client, "_lock")
