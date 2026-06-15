"""Focused tests for cache settings and maintenance actions."""

from types import SimpleNamespace
from unittest.mock import MagicMock
import time
import threading

import pytest

pytest.importorskip("PySide6.QtCore")

from PySide6.QtCore import QCoreApplication

from backend.plex_service import PlexService
from ui.controllers.scanner_controller import ScannerController
from ui.controllers.settings_controller import SettingsController


_APP = QCoreApplication.instance() or QCoreApplication([])


def test_plex_service_can_ignore_recently_added_check_when_disabled():
    db = MagicMock()
    now = time.time()
    db.get_plex_cache_max_timestamp.return_value = {
        "Movies": now,
        "TV Shows": now,
    }
    plex_manager = MagicMock()
    plex_manager.is_connected = True
    plex_manager.get_recently_added.return_value = [{"title": "New Movie"}]

    svc = PlexService(
        config={
            "cache_duration": 4,
            "plex_invalidate_on_new_content": False,
        },
        db=db,
        plex_manager=plex_manager,
    )

    valid, message = svc.check_cache_status()

    assert valid is True
    assert message == ""
    plex_manager.get_recently_added.assert_not_called()


def test_settings_controller_separates_plex_and_metadata_cache_purges():
    tmdb_cache = {"a": 1}
    omdb_cache = {"b": 2}
    db = MagicMock()
    db.plex_cache_counts_per_library.return_value = []

    backend = SimpleNamespace(
        config={},
        db=db,
        tmdb_cache=tmdb_cache,
        omdb_cache=omdb_cache,
    )
    controller = SettingsController(backend)

    controller.purgeCache()
    db.clear_plex_cache.assert_called_once()
    assert tmdb_cache == {"a": 1}
    assert omdb_cache == {"b": 2}

    controller.purgeMetadataCache()
    assert tmdb_cache == {}
    assert omdb_cache == {}


def test_scanner_controller_uses_and_persists_default_refresh_mode():
    backend = SimpleNamespace(
        config={"plex_refresh_mode": "cache_only"},
        tmdb_cache={},
        omdb_cache={},
        db=MagicMock(),
        plex_manager=MagicMock(),
        add_shutdown_hook=lambda _fn: None,
        set_scan_trigger=lambda _fn: None,
    )

    controller = ScannerController(backend)

    assert controller.plexRefreshMode == "cache_only"

    controller.setPlexRefreshMode("force_refresh")

    assert controller.plexRefreshMode == "force_refresh"
    assert backend.config["plex_refresh_mode"] == "force_refresh"


def test_start_scan_with_cache_choice_prepares_off_main_thread():
    backend = SimpleNamespace(
        config={"plex_refresh_mode": "auto"},
        tmdb_cache={},
        omdb_cache={},
        db=MagicMock(),
        plex_manager=MagicMock(is_connected=True),
        add_shutdown_hook=lambda _fn: None,
        set_scan_trigger=lambda _fn: None,
    )

    controller = ScannerController(backend)

    seen_threads = []
    launched = []
    ready = threading.Event()

    def fake_ensure_services():
        seen_threads.append(threading.current_thread().name)

    controller._ensure_services = fake_ensure_services

    # Intercept _launchScanWorker which is called by the PrepareWorker's signal
    orig_launch = controller._launchScanWorker
    def capture_launch(use_expired):
        launched.append(use_expired)
        ready.set()
    controller._launchScanWorker = capture_launch

    controller.startScanWithCacheChoice(True)

    deadline = time.time() + 2.0
    while not ready.is_set() and time.time() < deadline:
        _APP.processEvents()
        time.sleep(0.01)

    assert ready.is_set()
    assert seen_threads
    assert seen_threads[0] != threading.current_thread().name
    assert launched == [True]
