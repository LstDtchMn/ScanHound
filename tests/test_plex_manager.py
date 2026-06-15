"""Comprehensive tests for backend/plex_manager.py module.

Covers:
- LibraryType enum and from_plex_type
- PlexLibrary dataclass: to_dict, from_dict, from_plex_section
- PathMapping: translate, enabled/disabled
- PlexManager:
  - Properties: is_configured, is_connected
  - configure, connect (direct and account modes)
  - discover_servers
  - disconnect
  - Callbacks: add_callback, _notify
  - refresh_libraries, get_libraries, get_library
  - get_movie_libraries, get_tv_libraries
  - validate_library_names
  - get_library_section
  - Path mappings: add, remove, clear, translate, get
  - Serialization: save_to_dict, load_from_dict
  - get_server_info
  - scan_library
  - get_recently_added
- migrate_library_config
- Global manager: get_plex_manager, configure_plex
"""

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.plex_manager import (
    LibraryType, PlexLibrary, PathMapping, PlexManager,
    migrate_library_config, get_plex_manager, configure_plex,
)


# ======================================================================
# 1. LibraryType Enum
# ======================================================================

class TestLibraryType:
    """Tests for the LibraryType enum."""

    def test_movie_value(self):
        assert LibraryType.MOVIE.value == "movie"

    def test_show_value(self):
        assert LibraryType.SHOW.value == "show"

    def test_music_value(self):
        assert LibraryType.MUSIC.value == "artist"

    def test_photo_value(self):
        assert LibraryType.PHOTO.value == "photo"

    def test_other_value(self):
        assert LibraryType.OTHER.value == "other"

    def test_from_plex_type_movie(self):
        assert LibraryType.from_plex_type("movie") == LibraryType.MOVIE

    def test_from_plex_type_show(self):
        assert LibraryType.from_plex_type("show") == LibraryType.SHOW

    def test_from_plex_type_artist(self):
        assert LibraryType.from_plex_type("artist") == LibraryType.MUSIC

    def test_from_plex_type_photo(self):
        assert LibraryType.from_plex_type("photo") == LibraryType.PHOTO

    def test_from_plex_type_unknown(self):
        assert LibraryType.from_plex_type("clip") == LibraryType.OTHER

    def test_from_plex_type_case_insensitive(self):
        assert LibraryType.from_plex_type("MOVIE") == LibraryType.MOVIE
        assert LibraryType.from_plex_type("Show") == LibraryType.SHOW

    def test_from_plex_type_empty_string(self):
        assert LibraryType.from_plex_type("") == LibraryType.OTHER


# ======================================================================
# 2. PlexLibrary Dataclass
# ======================================================================

class TestPlexLibrary:
    """Tests for PlexLibrary dataclass."""

    def test_default_fields(self):
        lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        assert lib.key == "1"
        assert lib.title == "Movies"
        assert lib.type == LibraryType.MOVIE
        assert lib.scanner == ""
        assert lib.agent == ""
        assert lib.location == []
        assert lib.item_count == 0
        assert lib.last_scanned is None
        assert lib.uuid == ""

    def test_custom_fields(self):
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        lib = PlexLibrary(
            key="2", title="TV Shows", type=LibraryType.SHOW,
            scanner="Plex TV", agent="tv.plex.agents.series",
            location=["/media/tv"], item_count=100,
            last_scanned=ts, uuid="abc-123"
        )
        assert lib.title == "TV Shows"
        assert lib.scanner == "Plex TV"
        assert lib.location == ["/media/tv"]
        assert lib.item_count == 100
        assert lib.last_scanned == ts
        assert lib.uuid == "abc-123"

    def test_to_dict(self):
        ts = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        lib = PlexLibrary(
            key="1", title="Movies", type=LibraryType.MOVIE,
            scanner="Plex Movie", agent="tv.plex.agents.movie",
            location=["/movies"], item_count=50,
            last_scanned=ts, uuid="uuid-1"
        )
        d = lib.to_dict()
        assert d["key"] == "1"
        assert d["title"] == "Movies"
        assert d["type"] == "movie"
        assert d["scanner"] == "Plex Movie"
        assert d["agent"] == "tv.plex.agents.movie"
        assert d["location"] == ["/movies"]
        assert d["item_count"] == 50
        assert d["last_scanned"] == ts.isoformat()
        assert d["uuid"] == "uuid-1"

    def test_to_dict_no_last_scanned(self):
        lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        d = lib.to_dict()
        assert d["last_scanned"] is None

    def test_from_dict(self):
        data = {
            "key": "3",
            "title": "Music",
            "type": "artist",
            "scanner": "Plex Music",
            "agent": "tv.plex.agents.music",
            "location": ["/music"],
            "item_count": 200,
            "last_scanned": "2024-01-15T10:00:00+00:00",
            "uuid": "uuid-3"
        }
        lib = PlexLibrary.from_dict(data)
        assert lib.key == "3"
        assert lib.title == "Music"
        assert lib.type == LibraryType.MUSIC
        assert lib.scanner == "Plex Music"
        assert lib.location == ["/music"]
        assert lib.item_count == 200
        assert lib.last_scanned is not None
        assert lib.uuid == "uuid-3"

    def test_from_dict_minimal(self):
        data = {"key": "1", "title": "Test", "type": "movie"}
        lib = PlexLibrary.from_dict(data)
        assert lib.key == "1"
        assert lib.title == "Test"
        assert lib.scanner == ""
        assert lib.location == []
        assert lib.last_scanned is None

    def test_from_dict_empty(self):
        lib = PlexLibrary.from_dict({})
        assert lib.key == ""
        assert lib.title == ""
        assert lib.type == LibraryType.OTHER

    def test_from_dict_no_last_scanned(self):
        data = {"key": "1", "title": "Test", "type": "movie", "last_scanned": None}
        lib = PlexLibrary.from_dict(data)
        assert lib.last_scanned is None

    def test_from_plex_section(self):
        """Test creating PlexLibrary from a mock PlexAPI section."""
        section = MagicMock()
        section.key = 1
        section.title = "Movies"
        section.type = "movie"
        section.scanner = "Plex Movie"
        section.agent = "tv.plex.agents.movie"
        section.uuid = "section-uuid"
        section.totalSize = 150

        loc1 = MagicMock()
        loc1.path = "/media/movies"
        section.locations = [loc1]
        section.scannedAt = datetime(2024, 1, 1, tzinfo=timezone.utc)

        lib = PlexLibrary.from_plex_section(section)
        assert lib.key == "1"
        assert lib.title == "Movies"
        assert lib.type == LibraryType.MOVIE
        assert lib.scanner == "Plex Movie"
        assert lib.agent == "tv.plex.agents.movie"
        assert lib.location == ["/media/movies"]
        assert lib.item_count == 150
        assert lib.last_scanned == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert lib.uuid == "section-uuid"

    def test_from_plex_section_no_locations(self):
        """Section with no locations attribute should handle gracefully."""
        section = MagicMock()
        section.key = 2
        section.title = "TV"
        section.type = "show"
        section.locations = MagicMock(side_effect=Exception("no locations"))
        section.scannedAt = None
        section.totalSize = 0
        section.scanner = ""
        section.agent = ""
        section.uuid = ""

        # locations property raises an error
        type(section).locations = PropertyMock(side_effect=Exception("no access"))

        lib = PlexLibrary.from_plex_section(section)
        assert lib.location == []  # graceful fallback

    def test_from_plex_section_no_scanned_at(self):
        """Section with scannedAt = None."""
        section = MagicMock()
        section.key = 3
        section.title = "Photos"
        section.type = "photo"
        section.scannedAt = None
        section.totalSize = 10
        section.scanner = ""
        section.agent = ""
        section.uuid = ""
        loc = MagicMock()
        loc.path = "/photos"
        section.locations = [loc]

        lib = PlexLibrary.from_plex_section(section)
        assert lib.last_scanned is None

    def test_from_plex_section_no_totalSize(self):
        """Section without totalSize attribute."""
        section = MagicMock(spec=[])
        section.key = 1
        section.title = "Test"
        section.type = "movie"
        section.scannedAt = None
        section.locations = []
        # No totalSize attribute
        delattr(section, 'totalSize') if hasattr(section, 'totalSize') else None

        lib = PlexLibrary.from_plex_section(section)
        assert lib.item_count == 0

    def test_roundtrip_to_dict_from_dict(self):
        ts = datetime(2024, 3, 15, 8, 0, 0, tzinfo=timezone.utc)
        original = PlexLibrary(
            key="5", title="4K Movies", type=LibraryType.MOVIE,
            scanner="Plex Movie", agent="agent",
            location=["/movies/4k", "/movies/backup"],
            item_count=75, last_scanned=ts, uuid="roundtrip-uuid"
        )
        d = original.to_dict()
        restored = PlexLibrary.from_dict(d)
        assert restored.key == original.key
        assert restored.title == original.title
        assert restored.type == original.type
        assert restored.location == original.location
        assert restored.item_count == original.item_count
        assert restored.uuid == original.uuid


# ======================================================================
# 3. PathMapping
# ======================================================================

class TestPathMapping:
    """Tests for the PathMapping dataclass."""

    def test_default_enabled(self):
        m = PathMapping(plex_path="/plex", local_path="/local")
        assert m.enabled is True

    def test_translate_matching_prefix(self):
        m = PathMapping(plex_path="/plex/movies", local_path="/local/movies")
        result = m.translate("/plex/movies/file.mkv")
        assert result == "/local/movies/file.mkv"

    def test_translate_non_matching_prefix(self):
        m = PathMapping(plex_path="/plex/movies", local_path="/local/movies")
        result = m.translate("/other/path/file.mkv")
        assert result == "/other/path/file.mkv"

    def test_translate_disabled(self):
        m = PathMapping(plex_path="/plex", local_path="/local", enabled=False)
        result = m.translate("/plex/file.mkv")
        assert result == "/plex/file.mkv"  # unchanged when disabled

    def test_translate_exact_match(self):
        m = PathMapping(plex_path="/plex", local_path="/local")
        result = m.translate("/plex")
        assert result == "/local"

    def test_translate_only_first_occurrence(self):
        m = PathMapping(plex_path="/plex", local_path="/local")
        result = m.translate("/plex/sub/plex/file.mkv")
        assert result == "/local/sub/plex/file.mkv"

    def test_translate_empty_path(self):
        m = PathMapping(plex_path="/plex", local_path="/local")
        result = m.translate("")
        assert result == ""


# ======================================================================
# 4. PlexManager - Properties
# ======================================================================

class TestPlexManagerProperties:
    """Tests for PlexManager properties."""

    def test_is_configured_direct_with_url_and_token(self):
        mgr = PlexManager(url="http://plex:32400", token="token123")
        assert mgr.is_configured is True

    def test_is_configured_direct_missing_url(self):
        mgr = PlexManager(url="", token="token123")
        assert mgr.is_configured is False

    def test_is_configured_direct_missing_token(self):
        mgr = PlexManager(url="http://plex:32400", token="")
        assert mgr.is_configured is False

    def test_is_configured_direct_both_empty(self):
        mgr = PlexManager()
        assert mgr.is_configured is False

    def test_is_configured_account_mode(self):
        mgr = PlexManager()
        mgr._connection_mode = "account"
        mgr._username = "user"
        mgr._password = "pass"
        assert mgr.is_configured is True

    def test_is_configured_account_missing_username(self):
        mgr = PlexManager()
        mgr._connection_mode = "account"
        mgr._username = ""
        mgr._password = "pass"
        assert mgr.is_configured is False

    def test_is_configured_account_missing_password(self):
        mgr = PlexManager()
        mgr._connection_mode = "account"
        mgr._username = "user"
        mgr._password = ""
        assert mgr.is_configured is False

    def test_is_connected_false_initially(self):
        mgr = PlexManager()
        assert mgr.is_connected is False

    def test_is_connected_true_with_server(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        assert mgr.is_connected is True

    def test_url_trailing_slash_stripped(self):
        mgr = PlexManager(url="http://plex:32400/")
        assert mgr._url == "http://plex:32400"

    def test_url_empty(self):
        mgr = PlexManager(url="")
        assert mgr._url == ""


# ======================================================================
# 5. PlexManager - configure
# ======================================================================

class TestPlexManagerConfigure:
    """Tests for PlexManager.configure method."""

    def test_configure_direct(self):
        mgr = PlexManager()
        mgr.configure("http://plex:32400", "token123")
        assert mgr._url == "http://plex:32400"
        assert mgr._token == "token123"
        assert mgr._connection_mode == "direct"
        assert mgr._server is None

    def test_configure_account(self):
        mgr = PlexManager()
        mgr.configure("", "", connection_mode="account",
                       username="user", password="pass", server_name="MyServer")
        assert mgr._connection_mode == "account"
        assert mgr._username == "user"
        assert mgr._password == "pass"
        assert mgr._server_name == "MyServer"

    def test_configure_clears_state(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr._account = MagicMock()
        mgr._libraries = {"Test": MagicMock()}
        mgr._cache_timestamp = datetime.now(timezone.utc)

        mgr.configure("http://new:32400", "newtoken")
        assert mgr._server is None
        assert mgr._account is None
        assert mgr._libraries == {}
        assert mgr._cache_timestamp is None

    def test_configure_strips_trailing_slash(self):
        mgr = PlexManager()
        mgr.configure("http://plex:32400///", "token")
        assert mgr._url == "http://plex:32400"


# ======================================================================
# 6. PlexManager - connect (direct)
# ======================================================================

class TestConnectDirect:
    """Tests for direct connection to Plex server."""

    def test_connect_direct_success(self):
        mgr = PlexManager(url="http://plex:32400", token="token")
        mock_server = MagicMock()
        mock_server.friendlyName = "TestServer"

        mock_plexapi_server = MagicMock()
        mock_plexapi_server.PlexServer = MagicMock(return_value=mock_server)
        mock_plexapi_exceptions = MagicMock()

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.server': mock_plexapi_server,
            'plexapi.exceptions': mock_plexapi_exceptions,
        }):
            success, msg = mgr._connect_direct()
            assert success is True
            assert "TestServer" in msg
            assert mgr._server is mock_server

    def test_connect_direct_no_url(self):
        mgr = PlexManager(url="", token="token")
        success, msg = mgr._connect_direct()
        assert success is False
        assert "URL and token required" in msg

    def test_connect_direct_no_token(self):
        mgr = PlexManager(url="http://plex:32400", token="")
        success, msg = mgr._connect_direct()
        assert success is False
        assert "URL and token required" in msg

    def test_connect_direct_no_url_no_token(self):
        mgr = PlexManager()
        success, msg = mgr._connect_direct()
        assert success is False

    def test_connect_direct_success_with_callback(self):
        """Direct connect should notify 'connected' callback."""
        mgr = PlexManager(url="http://plex:32400", token="token")
        mock_server = MagicMock()
        mock_server.friendlyName = "TestServer"
        cb = MagicMock()
        mgr.add_callback(cb)

        mock_plexapi_server = MagicMock()
        mock_plexapi_server.PlexServer = MagicMock(return_value=mock_server)
        mock_plexapi_exceptions = MagicMock()

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.server': mock_plexapi_server,
            'plexapi.exceptions': mock_plexapi_exceptions,
        }):
            success, msg = mgr._connect_direct()
            assert success is True
            cb.assert_called_once_with('connected', 'TestServer')

    def test_connect_direct_unauthorized(self):
        mgr = PlexManager(url="http://plex:32400", token="badtoken")

        mock_exceptions = MagicMock()
        unauthorized_exc = type('Unauthorized', (Exception,), {})
        not_found_exc = type('NotFound', (Exception,), {})
        mock_exceptions.Unauthorized = unauthorized_exc
        mock_exceptions.NotFound = not_found_exc

        mock_server_mod = MagicMock()
        mock_server_mod.PlexServer = MagicMock(side_effect=unauthorized_exc())

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.server': mock_server_mod,
            'plexapi.exceptions': mock_exceptions,
        }):
            success, msg = mgr._connect_direct()
            assert success is False
            assert "Invalid Plex token" in msg
            assert mgr._server is None

    def test_connect_direct_not_found(self):
        mgr = PlexManager(url="http://plex:32400", token="token")

        mock_exceptions = MagicMock()
        unauthorized_exc = type('Unauthorized', (Exception,), {})
        not_found_exc = type('NotFound', (Exception,), {})
        mock_exceptions.Unauthorized = unauthorized_exc
        mock_exceptions.NotFound = not_found_exc

        mock_server_mod = MagicMock()
        mock_server_mod.PlexServer = MagicMock(side_effect=not_found_exc())

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.server': mock_server_mod,
            'plexapi.exceptions': mock_exceptions,
        }):
            success, msg = mgr._connect_direct()
            assert success is False
            assert "not found" in msg
            assert mgr._server is None

    def test_connect_direct_generic_exception(self):
        mgr = PlexManager(url="http://plex:32400", token="token")

        mock_exceptions = MagicMock()
        unauthorized_exc = type('Unauthorized', (Exception,), {})
        not_found_exc = type('NotFound', (Exception,), {})
        mock_exceptions.Unauthorized = unauthorized_exc
        mock_exceptions.NotFound = not_found_exc

        mock_server_mod = MagicMock()
        mock_server_mod.PlexServer = MagicMock(side_effect=ConnectionError("Connection refused"))

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.server': mock_server_mod,
            'plexapi.exceptions': mock_exceptions,
        }):
            success, msg = mgr._connect_direct()
            assert success is False
            assert "Connection failed" in msg
            assert mgr._server is None


# ======================================================================
# 7. PlexManager - connect (dispatch)
# ======================================================================

class TestConnect:
    """Tests for PlexManager.connect dispatch."""

    def test_connect_dispatches_to_direct(self):
        mgr = PlexManager(url="http://plex:32400", token="token")
        mgr._connection_mode = "direct"
        mgr._connect_direct = MagicMock(return_value=(True, "OK"))
        success, msg = mgr.connect()
        mgr._connect_direct.assert_called_once()
        assert success is True

    def test_connect_dispatches_to_account(self):
        mgr = PlexManager()
        mgr._connection_mode = "account"
        mgr.connect_via_account = MagicMock(return_value=(True, "OK"))
        success, msg = mgr.connect()
        mgr.connect_via_account.assert_called_once()
        assert success is True

    def test_connect_passes_timeout(self):
        mgr = PlexManager(url="http://plex:32400", token="token")
        mgr._connection_mode = "direct"
        mgr._connect_direct = MagicMock(return_value=(True, "OK"))
        mgr.connect(timeout=30)
        mgr._connect_direct.assert_called_once_with(timeout=30)


# ======================================================================
# 8. PlexManager - connect_via_account
# ======================================================================

class TestConnectViaAccount:
    """Tests for PlexManager.connect_via_account."""

    def test_connect_account_no_credentials(self):
        mgr = PlexManager()
        mgr._username = ""
        mgr._password = ""
        success, msg = mgr.connect_via_account()
        assert success is False
        assert "username and password required" in msg

    def test_connect_account_no_password(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = ""
        success, msg = mgr.connect_via_account()
        assert success is False

    def test_connect_account_success(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = "pass"

        mock_account = MagicMock()
        mock_resource = MagicMock()
        mock_resource.name = "TestServer"
        mock_resource.provides = "server"
        mock_server = MagicMock()
        mock_server.friendlyName = "TestServer"
        mock_resource.connect.return_value = mock_server
        mock_account.resources.return_value = [mock_resource]

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(return_value=mock_account)

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg = mgr.connect_via_account()
            assert success is True
            assert "TestServer" in msg
            assert mgr._server is mock_server

    def test_connect_account_no_servers(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = "pass"

        mock_account = MagicMock()
        mock_account.resources.return_value = []  # no resources at all

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(return_value=mock_account)

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg = mgr.connect_via_account()
            assert success is False
            assert "No Plex servers" in msg

    def test_connect_account_named_server_found(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = "pass"
        mgr._server_name = "TargetServer"

        mock_account = MagicMock()
        r1 = MagicMock()
        r1.name = "OtherServer"
        r1.provides = "server"
        r2 = MagicMock()
        r2.name = "TargetServer"
        r2.provides = "server"
        mock_server = MagicMock()
        mock_server.friendlyName = "TargetServer"
        r2.connect.return_value = mock_server
        mock_account.resources.return_value = [r1, r2]

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(return_value=mock_account)

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg = mgr.connect_via_account()
            assert success is True
            assert "TargetServer" in msg

    def test_connect_account_named_server_not_found(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = "pass"
        mgr._server_name = "NonexistentServer"

        mock_account = MagicMock()
        r1 = MagicMock()
        r1.name = "ServerA"
        r1.provides = "server"
        mock_account.resources.return_value = [r1]

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(return_value=mock_account)

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg = mgr.connect_via_account()
            assert success is False
            assert "not found" in msg
            assert "ServerA" in msg

    def test_connect_account_unauthorized_error(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = "wrongpass"

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(side_effect=Exception("(401) unauthorized"))

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg = mgr.connect_via_account()
            assert success is False
            assert "Invalid Plex username or password" in msg
            assert mgr._server is None
            assert mgr._account is None

    def test_connect_account_generic_error(self):
        mgr = PlexManager()
        mgr._username = "user"
        mgr._password = "pass"

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(side_effect=Exception("Network error"))

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg = mgr.connect_via_account()
            assert success is False
            assert "Connection failed" in msg


# ======================================================================
# 9. PlexManager - discover_servers
# ======================================================================

class TestDiscoverServers:
    """Tests for discover_servers."""

    def test_discover_success(self):
        mgr = PlexManager()

        mock_account = MagicMock()
        r1 = MagicMock()
        r1.name = "Server1"
        r1.provides = "server"
        r2 = MagicMock()
        r2.name = "Server2"
        r2.provides = "server"
        mock_account.resources.return_value = [r1, r2]

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(return_value=mock_account)

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg, names = mgr.discover_servers("user", "pass")
            assert success is True
            assert len(names) == 2
            assert "Server1" in names
            assert "Server2" in names

    def test_discover_unauthorized(self):
        mgr = PlexManager()

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(side_effect=Exception("401 unauthorized"))

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg, names = mgr.discover_servers("user", "wrongpass")
            assert success is False
            assert "Invalid username or password" in msg
            assert names == []

    def test_discover_generic_error(self):
        mgr = PlexManager()

        mock_myplex = MagicMock()
        mock_myplex.MyPlexAccount = MagicMock(side_effect=Exception("Network down"))

        with patch.dict('sys.modules', {
            'plexapi': MagicMock(),
            'plexapi.myplex': mock_myplex,
        }):
            success, msg, names = mgr.discover_servers("user", "pass")
            assert success is False
            assert names == []


# ======================================================================
# 10. PlexManager - disconnect
# ======================================================================

class TestDisconnect:
    """Tests for disconnect method."""

    def test_disconnect_clears_server(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr.disconnect()
        assert mgr._server is None

    def test_disconnect_notifies(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        cb = MagicMock()
        mgr.add_callback(cb)
        mgr.disconnect()
        cb.assert_called_once_with('disconnected', None)


# ======================================================================
# 11. Callbacks
# ======================================================================

class TestCallbacks:
    """Tests for callback system."""

    def test_add_callback(self):
        mgr = PlexManager()
        cb = MagicMock()
        mgr.add_callback(cb)
        assert cb in mgr._callbacks

    def test_notify_calls_all_callbacks(self):
        mgr = PlexManager()
        cb1 = MagicMock()
        cb2 = MagicMock()
        mgr.add_callback(cb1)
        mgr.add_callback(cb2)
        mgr._notify('test_event', 'test_data')
        cb1.assert_called_once_with('test_event', 'test_data')
        cb2.assert_called_once_with('test_event', 'test_data')

    def test_notify_handles_callback_error(self):
        mgr = PlexManager()
        bad_cb = MagicMock(side_effect=Exception("callback error"))
        good_cb = MagicMock()
        mgr.add_callback(bad_cb)
        mgr.add_callback(good_cb)
        # Should not raise
        mgr._notify('test', None)
        # Good callback should still be called
        good_cb.assert_called_once_with('test', None)

    def test_notify_no_callbacks(self):
        mgr = PlexManager()
        # Should not raise
        mgr._notify('test', None)


# ======================================================================
# 12. refresh_libraries
# ======================================================================

class TestRefreshLibraries:
    """Tests for refresh_libraries."""

    def test_refresh_from_server(self):
        mgr = PlexManager()
        mock_server = MagicMock()
        mgr._server = mock_server

        section1 = MagicMock()
        section1.key = 1
        section1.title = "Movies"
        section1.type = "movie"
        section1.scanner = ""
        section1.agent = ""
        section1.uuid = ""
        section1.totalSize = 10
        section1.scannedAt = None
        loc1 = MagicMock()
        loc1.path = "/movies"
        section1.locations = [loc1]

        section2 = MagicMock()
        section2.key = 2
        section2.title = "TV Shows"
        section2.type = "show"
        section2.scanner = ""
        section2.agent = ""
        section2.uuid = ""
        section2.totalSize = 20
        section2.scannedAt = None
        loc2 = MagicMock()
        loc2.path = "/tv"
        section2.locations = [loc2]

        mock_server.library.sections.return_value = [section1, section2]

        libs = mgr.refresh_libraries(force=True)
        assert len(libs) == 2
        titles = {lib.title for lib in libs}
        assert "Movies" in titles
        assert "TV Shows" in titles

    def test_refresh_uses_cache(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        lib1 = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": lib1}
        mgr._cache_timestamp = datetime.now(timezone.utc)  # fresh cache

        libs = mgr.refresh_libraries(force=False)
        assert len(libs) == 1
        # Should NOT have called sections() because cache is valid
        mgr._server.library.sections.assert_not_called()

    def test_refresh_force_ignores_cache(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr._server.library.sections.return_value = []
        lib1 = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": lib1}
        mgr._cache_timestamp = datetime.now(timezone.utc)

        libs = mgr.refresh_libraries(force=True)
        mgr._server.library.sections.assert_called_once()

    def test_refresh_expired_cache(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr._server.library.sections.return_value = []
        mgr._libraries = {}
        # Expired cache (2 hours ago)
        mgr._cache_timestamp = datetime.now(timezone.utc) - timedelta(hours=2)

        mgr.refresh_libraries(force=False)
        mgr._server.library.sections.assert_called_once()

    def test_refresh_not_connected_tries_connect(self):
        mgr = PlexManager()
        mgr._server = None
        mgr.connect = MagicMock(return_value=(False, "failed"))

        libs = mgr.refresh_libraries()
        mgr.connect.assert_called_once()
        assert libs == []

    def test_refresh_server_error_returns_cached(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr._server.library.sections.side_effect = Exception("server error")
        lib1 = PlexLibrary(key="1", title="Cached", type=LibraryType.MOVIE)
        mgr._libraries = {"Cached": lib1}

        libs = mgr.refresh_libraries(force=True)
        assert len(libs) == 1
        assert libs[0].title == "Cached"

    def test_refresh_notifies_callbacks(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr._server.library.sections.return_value = []
        cb = MagicMock()
        mgr.add_callback(cb)

        mgr.refresh_libraries(force=True)
        cb.assert_called_with('libraries_updated', [])


# ======================================================================
# 13. get_libraries, get_library
# ======================================================================

class TestGetLibraries:
    """Tests for get_libraries and get_library."""

    def test_get_libraries_empty_triggers_refresh(self):
        mgr = PlexManager()
        mgr.refresh_libraries = MagicMock(return_value=[])
        result = mgr.get_libraries()
        mgr.refresh_libraries.assert_called_once()
        assert result == []

    def test_get_libraries_cached(self):
        mgr = PlexManager()
        lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": lib}
        result = mgr.get_libraries()
        assert len(result) == 1
        assert result[0].title == "Movies"

    def test_get_library_found(self):
        mgr = PlexManager()
        lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": lib}
        result = mgr.get_library("Movies")
        assert result is not None
        assert result.title == "Movies"

    def test_get_library_not_found(self):
        mgr = PlexManager()
        lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": lib}
        result = mgr.get_library("TV Shows")
        assert result is None

    def test_get_library_empty_triggers_refresh(self):
        mgr = PlexManager()
        mgr.refresh_libraries = MagicMock()
        mgr.get_library("Movies")
        mgr.refresh_libraries.assert_called_once()


# ======================================================================
# 14. get_movie_libraries, get_tv_libraries
# ======================================================================

class TestFilteredLibraries:
    """Tests for get_movie_libraries and get_tv_libraries."""

    def _setup_mgr(self):
        mgr = PlexManager()
        mgr._libraries = {
            "Movies": PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE),
            "4K Movies": PlexLibrary(key="2", title="4K Movies", type=LibraryType.MOVIE),
            "TV Shows": PlexLibrary(key="3", title="TV Shows", type=LibraryType.SHOW),
            "Music": PlexLibrary(key="4", title="Music", type=LibraryType.MUSIC),
        }
        return mgr

    def test_get_movie_libraries(self):
        mgr = self._setup_mgr()
        result = mgr.get_movie_libraries()
        assert len(result) == 2
        assert all(lib.type == LibraryType.MOVIE for lib in result)

    def test_get_tv_libraries(self):
        mgr = self._setup_mgr()
        result = mgr.get_tv_libraries()
        assert len(result) == 1
        assert result[0].title == "TV Shows"

    def test_get_movie_libraries_none(self):
        mgr = PlexManager()
        mgr._libraries = {
            "Music": PlexLibrary(key="1", title="Music", type=LibraryType.MUSIC),
        }
        result = mgr.get_movie_libraries()
        assert result == []


# ======================================================================
# 15. validate_library_names
# ======================================================================

class TestValidateLibraryNames:
    """Tests for validate_library_names."""

    def _setup_mgr(self):
        mgr = PlexManager()
        mgr._libraries = {
            "Movies": PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE),
            "TV Shows": PlexLibrary(key="2", title="TV Shows", type=LibraryType.SHOW),
        }
        return mgr

    def test_all_valid(self):
        mgr = self._setup_mgr()
        valid, invalid = mgr.validate_library_names(["Movies", "TV Shows"])
        assert valid == ["Movies", "TV Shows"]
        assert invalid == []

    def test_some_invalid(self):
        mgr = self._setup_mgr()
        valid, invalid = mgr.validate_library_names(["Movies", "Nonexistent"])
        assert valid == ["Movies"]
        assert invalid == ["Nonexistent"]

    def test_all_invalid(self):
        mgr = self._setup_mgr()
        valid, invalid = mgr.validate_library_names(["Foo", "Bar"])
        assert valid == []
        assert invalid == ["Foo", "Bar"]

    def test_empty_list(self):
        mgr = self._setup_mgr()
        valid, invalid = mgr.validate_library_names([])
        assert valid == []
        assert invalid == []


# ======================================================================
# 16. get_library_section
# ======================================================================

class TestGetLibrarySection:
    """Tests for get_library_section."""

    def test_get_section_success(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mock_section = MagicMock()
        mgr._server.library.section.return_value = mock_section

        result = mgr.get_library_section("Movies")
        assert result is mock_section

    def test_get_section_not_connected(self):
        mgr = PlexManager()
        mgr._server = None
        mgr.connect = MagicMock(return_value=(False, "failed"))

        result = mgr.get_library_section("Movies")
        assert result is None

    def test_get_section_error(self):
        mgr = PlexManager()
        mgr._server = MagicMock()
        mgr._server.library.section.side_effect = Exception("Section not found")

        result = mgr.get_library_section("Nonexistent")
        assert result is None


# ======================================================================
# 17. Path Mapping Operations
# ======================================================================

class TestPathMappingOperations:
    """Tests for path mapping management."""

    def test_add_path_mapping(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        assert len(mgr._path_mappings) == 1
        assert mgr._path_mappings[0].plex_path == "/plex/movies"
        assert mgr._path_mappings[0].local_path == "/local/movies"

    def test_add_multiple_mappings(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        mgr.add_path_mapping("/plex/tv", "/local/tv")
        assert len(mgr._path_mappings) == 2

    def test_remove_path_mapping(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        mgr.add_path_mapping("/plex/tv", "/local/tv")
        mgr.remove_path_mapping(0)
        assert len(mgr._path_mappings) == 1
        assert mgr._path_mappings[0].plex_path == "/plex/tv"

    def test_remove_invalid_index(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        mgr.remove_path_mapping(5)  # out of range
        assert len(mgr._path_mappings) == 1

    def test_remove_negative_index(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        mgr.remove_path_mapping(-1)  # negative
        assert len(mgr._path_mappings) == 1

    def test_clear_path_mappings(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/a", "/local/a")
        mgr.add_path_mapping("/plex/b", "/local/b")
        mgr.clear_path_mappings()
        assert len(mgr._path_mappings) == 0

    def test_translate_path_with_mapping(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        result = mgr.translate_path("/plex/movies/file.mkv")
        assert result == "/local/movies/file.mkv"

    def test_translate_path_no_matching_mapping(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        result = mgr.translate_path("/other/path/file.mkv")
        assert result == "/other/path/file.mkv"

    def test_translate_path_no_mappings(self):
        mgr = PlexManager()
        result = mgr.translate_path("/plex/file.mkv")
        assert result == "/plex/file.mkv"

    def test_translate_path_longest_prefix_wins(self):
        """More specific (longer) mapping should take precedence."""
        mgr = PlexManager()
        mgr.add_path_mapping("/media", "/short")
        mgr.add_path_mapping("/media/movies", "/long")
        result = mgr.translate_path("/media/movies/file.mkv")
        assert result == "/long/file.mkv"

    def test_get_path_mappings_returns_copy(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex", "/local")
        mappings = mgr.get_path_mappings()
        assert len(mappings) == 1
        # Modifying the returned list shouldn't affect the original
        mappings.append(PathMapping(plex_path="/x", local_path="/y"))
        assert len(mgr._path_mappings) == 1


# ======================================================================
# 18. Serialization
# ======================================================================

class TestSerialization:
    """Tests for save_to_dict and load_from_dict."""

    def test_save_to_dict_basic(self):
        mgr = PlexManager(url="http://plex:32400", token="token123")
        d = mgr.save_to_dict()
        assert d["url"] == "http://plex:32400"
        assert d["token"] == "token123"
        assert d["libraries"] == {}
        assert d["path_mappings"] == []
        assert d["cache_timestamp"] is None

    def test_save_to_dict_with_libraries(self):
        mgr = PlexManager()
        mgr._libraries = {
            "Movies": PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        }
        d = mgr.save_to_dict()
        assert "Movies" in d["libraries"]
        assert d["libraries"]["Movies"]["title"] == "Movies"

    def test_save_to_dict_with_path_mappings(self):
        mgr = PlexManager()
        mgr.add_path_mapping("/plex", "/local")
        d = mgr.save_to_dict()
        assert len(d["path_mappings"]) == 1
        assert d["path_mappings"][0]["plex_path"] == "/plex"

    def test_save_to_dict_with_cache_timestamp(self):
        mgr = PlexManager()
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mgr._cache_timestamp = ts
        d = mgr.save_to_dict()
        assert d["cache_timestamp"] == ts.isoformat()

    def test_load_from_dict_basic(self):
        mgr = PlexManager()
        mgr.load_from_dict({
            "url": "http://loaded:32400",
            "token": "loaded_token",
        })
        assert mgr._url == "http://loaded:32400"
        assert mgr._token == "loaded_token"

    def test_load_from_dict_with_libraries(self):
        mgr = PlexManager()
        mgr.load_from_dict({
            "libraries": {
                "Movies": {"key": "1", "title": "Movies", "type": "movie"}
            }
        })
        assert "Movies" in mgr._libraries
        assert mgr._libraries["Movies"].title == "Movies"

    def test_load_from_dict_with_path_mappings(self):
        mgr = PlexManager()
        mgr.load_from_dict({
            "path_mappings": [
                {"plex_path": "/plex", "local_path": "/local", "enabled": True}
            ]
        })
        assert len(mgr._path_mappings) == 1
        assert mgr._path_mappings[0].plex_path == "/plex"

    def test_load_from_dict_with_cache_timestamp(self):
        mgr = PlexManager()
        ts = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        mgr.load_from_dict({"cache_timestamp": ts.isoformat()})
        assert mgr._cache_timestamp == ts

    def test_load_from_dict_naive_timestamp(self):
        """Legacy naive timestamps should get UTC timezone."""
        mgr = PlexManager()
        mgr.load_from_dict({"cache_timestamp": "2024-01-01T12:00:00"})
        assert mgr._cache_timestamp.tzinfo == timezone.utc

    def test_load_from_dict_no_cache_timestamp(self):
        mgr = PlexManager()
        mgr.load_from_dict({})
        assert mgr._cache_timestamp is None

    def test_load_from_dict_clears_old_state(self):
        mgr = PlexManager()
        mgr._libraries = {"Old": PlexLibrary(key="0", title="Old", type=LibraryType.OTHER)}
        mgr._path_mappings = [PathMapping(plex_path="/old", local_path="/old")]

        mgr.load_from_dict({
            "libraries": {"New": {"key": "1", "title": "New", "type": "movie"}},
            "path_mappings": [],
        })
        assert "Old" not in mgr._libraries
        assert "New" in mgr._libraries
        assert len(mgr._path_mappings) == 0

    def test_roundtrip_save_load(self):
        mgr = PlexManager(url="http://plex:32400", token="tok")
        mgr._libraries = {
            "Movies": PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE,
                                  location=["/movies"], item_count=50)
        }
        mgr.add_path_mapping("/plex/movies", "/local/movies")
        mgr._cache_timestamp = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        d = mgr.save_to_dict()

        mgr2 = PlexManager()
        mgr2.load_from_dict(d)
        assert mgr2._url == "http://plex:32400"
        assert mgr2._token == "tok"
        assert "Movies" in mgr2._libraries
        assert len(mgr2._path_mappings) == 1
        assert mgr2._cache_timestamp is not None

    def test_load_from_dict_empty(self):
        mgr = PlexManager()
        mgr.load_from_dict({})
        assert mgr._url == ""
        assert mgr._token == ""
        assert mgr._libraries == {}
        assert mgr._path_mappings == []


# ======================================================================
# 19. get_server_info
# ======================================================================

class TestGetServerInfo:
    """Tests for get_server_info."""

    def test_not_connected_returns_none(self):
        mgr = PlexManager()
        mgr._server = None
        result = mgr.get_server_info()
        assert result is None

    def test_connected_returns_info(self):
        mgr = PlexManager()
        mock_server = MagicMock()
        mock_server.friendlyName = "MyServer"
        mock_server.version = "1.32.0"
        mock_server.platform = "Linux"
        mock_server.machineIdentifier = "abc123"
        mock_server.myPlexUsername = "user@test.com"
        mock_server.transcoderActiveVideoSessions = 2
        mgr._server = mock_server

        result = mgr.get_server_info()
        assert result["name"] == "MyServer"
        assert result["version"] == "1.32.0"
        assert result["platform"] == "Linux"
        assert result["machine_id"] == "abc123"
        assert result["my_plex_username"] == "user@test.com"
        assert result["transcoder_active"] == 2

    def test_server_info_error(self):
        mgr = PlexManager()
        mock_server = MagicMock()
        type(mock_server).friendlyName = PropertyMock(side_effect=Exception("error"))
        mgr._server = mock_server

        result = mgr.get_server_info()
        assert result is None


# ======================================================================
# 20. scan_library
# ======================================================================

class TestScanLibrary:
    """Tests for scan_library."""

    def test_scan_success(self):
        mgr = PlexManager()
        mock_section = MagicMock()
        mgr.get_library_section = MagicMock(return_value=mock_section)

        result = mgr.scan_library("Movies")
        assert result is True
        mock_section.update.assert_called_once()

    def test_scan_no_section(self):
        mgr = PlexManager()
        mgr.get_library_section = MagicMock(return_value=None)

        result = mgr.scan_library("Nonexistent")
        assert result is False

    def test_scan_error(self):
        mgr = PlexManager()
        mock_section = MagicMock()
        mock_section.update.side_effect = Exception("scan error")
        mgr.get_library_section = MagicMock(return_value=mock_section)

        result = mgr.scan_library("Movies")
        assert result is False


# ======================================================================
# 21. get_recently_added
# ======================================================================

class TestGetRecentlyAdded:
    """Tests for get_recently_added."""

    def test_not_connected_tries_connect(self):
        mgr = PlexManager()
        mgr._server = None
        mgr.connect = MagicMock(return_value=(False, "failed"))

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.get_recently_added(since)
        assert result == []
        mgr.connect.assert_called_once()

    def test_recently_added_success(self):
        mgr = PlexManager()
        mgr._server = MagicMock()

        # Setup libraries
        movie_lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": movie_lib}

        mock_section = MagicMock()
        mock_item = MagicMock()
        mock_section.search.return_value = [mock_item]
        mgr.get_library_section = MagicMock(return_value=mock_section)

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.get_recently_added(since)
        assert len(result) == 1
        assert result[0] is mock_item

    def test_recently_added_skips_non_media_libraries(self):
        mgr = PlexManager()
        mgr._server = MagicMock()

        music_lib = PlexLibrary(key="1", title="Music", type=LibraryType.MUSIC)
        mgr._libraries = {"Music": music_lib}
        mgr.get_library_section = MagicMock()

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.get_recently_added(since)
        assert result == []
        mgr.get_library_section.assert_not_called()

    def test_recently_added_handles_section_error(self):
        mgr = PlexManager()
        mgr._server = MagicMock()

        movie_lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": movie_lib}

        mock_section = MagicMock()
        mock_section.search.side_effect = Exception("search failed")
        mgr.get_library_section = MagicMock(return_value=mock_section)

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.get_recently_added(since)
        assert result == []

    def test_recently_added_multiple_libraries(self):
        mgr = PlexManager()
        mgr._server = MagicMock()

        movie_lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        tv_lib = PlexLibrary(key="2", title="TV Shows", type=LibraryType.SHOW)
        mgr._libraries = {"Movies": movie_lib, "TV Shows": tv_lib}

        mock_section_movies = MagicMock()
        mock_section_movies.search.return_value = [MagicMock(), MagicMock()]
        mock_section_tv = MagicMock()
        mock_section_tv.search.return_value = [MagicMock()]

        def get_section(name):
            if name == "Movies":
                return mock_section_movies
            return mock_section_tv

        mgr.get_library_section = MagicMock(side_effect=get_section)

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.get_recently_added(since)
        assert len(result) == 3

    def test_recently_added_no_section(self):
        mgr = PlexManager()
        mgr._server = MagicMock()

        movie_lib = PlexLibrary(key="1", title="Movies", type=LibraryType.MOVIE)
        mgr._libraries = {"Movies": movie_lib}
        mgr.get_library_section = MagicMock(return_value=None)

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = mgr.get_recently_added(since)
        assert result == []


# ======================================================================
# 22. migrate_library_config
# ======================================================================

class TestMigrateLibraryConfig:
    """Tests for migrate_library_config function."""

    def test_no_old_keys(self):
        config = {"movie_libs": ["Movies"], "tv_libs": ["TV"]}
        result = migrate_library_config(config)
        assert result["movie_libs"] == ["Movies"]
        assert result["tv_libs"] == ["TV"]

    def test_merge_selected_movie_libraries(self):
        config = {
            "movie_libs": ["Movies"],
            "selected_movie_libraries": ["4K Movies"],
        }
        result = migrate_library_config(config)
        assert set(result["movie_libs"]) == {"Movies", "4K Movies"}
        assert "selected_movie_libraries" not in result

    def test_merge_selected_tv_libraries(self):
        config = {
            "tv_libs": ["TV Shows"],
            "selected_tv_libraries": ["Anime"],
        }
        result = migrate_library_config(config)
        assert set(result["tv_libs"]) == {"TV Shows", "Anime"}
        assert "selected_tv_libraries" not in result

    def test_empty_selected_no_change(self):
        config = {
            "movie_libs": ["Movies"],
            "tv_libs": ["TV"],
            "selected_movie_libraries": [],
            "selected_tv_libraries": [],
        }
        result = migrate_library_config(config)
        assert result["movie_libs"] == ["Movies"]
        assert result["tv_libs"] == ["TV"]

    def test_removes_old_keys(self):
        config = {
            "movie_libs": [],
            "tv_libs": [],
            "selected_movie_libraries": ["Movies"],
            "selected_tv_libraries": ["TV"],
        }
        result = migrate_library_config(config)
        assert "selected_movie_libraries" not in result
        assert "selected_tv_libraries" not in result

    def test_does_not_mutate_input(self):
        config = {"movie_libs": ["A"], "selected_movie_libraries": ["B"],
                   "tv_libs": [], "selected_tv_libraries": []}
        original = config.copy()
        migrate_library_config(config)
        assert config.get("selected_movie_libraries") == original.get("selected_movie_libraries")

    def test_deduplicate_libs(self):
        """When movie_libs and selected_movie_libraries overlap, no duplicates."""
        config = {
            "movie_libs": ["Movies", "4K"],
            "selected_movie_libraries": ["Movies", "Anime"],
        }
        result = migrate_library_config(config)
        assert len(result["movie_libs"]) == len(set(result["movie_libs"]))

    def test_empty_config(self):
        result = migrate_library_config({})
        assert result["movie_libs"] == []
        assert result["tv_libs"] == []

    def test_preserves_other_keys(self):
        config = {
            "plex_url": "http://plex:32400",
            "movie_libs": ["Movies"],
            "tv_libs": ["TV"],
        }
        result = migrate_library_config(config)
        assert result["plex_url"] == "http://plex:32400"


# ======================================================================
# 23. Global manager functions
# ======================================================================

class TestGlobalManager:
    """Tests for get_plex_manager and configure_plex."""

    def test_get_plex_manager_returns_instance(self):
        # Reset the global
        import backend.plex_manager as pm
        old = pm._plex_manager
        pm._plex_manager = None
        try:
            mgr = get_plex_manager()
            assert isinstance(mgr, PlexManager)
        finally:
            pm._plex_manager = old

    def test_get_plex_manager_singleton(self):
        import backend.plex_manager as pm
        old = pm._plex_manager
        pm._plex_manager = None
        try:
            mgr1 = get_plex_manager()
            mgr2 = get_plex_manager()
            assert mgr1 is mgr2
        finally:
            pm._plex_manager = old

    def test_configure_plex(self):
        import backend.plex_manager as pm
        old = pm._plex_manager
        pm._plex_manager = None
        try:
            mgr = configure_plex("http://plex:32400", "token123")
            assert isinstance(mgr, PlexManager)
            assert mgr._url == "http://plex:32400"
            assert mgr._token == "token123"
        finally:
            pm._plex_manager = old

    def test_configure_plex_account_mode(self):
        import backend.plex_manager as pm
        old = pm._plex_manager
        pm._plex_manager = None
        try:
            mgr = configure_plex("", "", connection_mode="account",
                                  username="user", password="pass",
                                  server_name="MyServer")
            assert mgr._connection_mode == "account"
            assert mgr._username == "user"
        finally:
            pm._plex_manager = old
