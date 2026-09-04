"""HDE-6 regression: a db-less bridge must not wipe a live cooldown.

Two mechanisms combined to lose real cooldown/health-persistence state in
production:

1. Identity reset: HDEncodeTrafficCoordinator.configure() used to treat any
   new config/db *object identity* as a new application context and clear
   _block_streak/_local_cooldown_until/_local_cooldown_reason.
   DetailScraper.__init__ calls configure_hdencode_coordinator() on every
   construction; the config object it passes is the app's stable one, but
   the db it passes is None (see 2), a different object from the real db a
   previous caller attached, so the identity check fired and wiped whatever
   cooldown was live.
   `test_constructing_a_detail_scraper_through_a_bridge_without_db_keeps_cooldown_and_db`
   pins that a DetailScraper build no longer clears an active cooldown.

2. DB detach: configure() also unconditionally set `self._db = db`, so a
   caller with no db (every production parent_app bridge, which had no `db`
   attribute at all) detached whatever db a previous caller had attached,
   silently turning off health persistence. The same test's `db is real_db`
   assertion pins that a None db can no longer detach an already-attached
   one, and `test_the_api_bridge_now_exposes_the_database` pins the other
   half of the fix: the bridges now actually expose a `db` attribute for
   DetailScraper's `getattr(parent_app, "db", None)` to find.
"""
from __future__ import annotations

import types

import pytest

from backend.database import DatabaseManager
from backend.detail_scraper import DetailScraper
from backend.hdencode_coordinator import (
    configure_hdencode_coordinator,
    get_hdencode_coordinator,
)


def test_constructing_a_detail_scraper_through_a_bridge_without_db_keeps_cooldown_and_db(
    tmp_path,
):
    real_db = DatabaseManager(db_path=str(tmp_path / "hde6_test.db"))
    try:
        real_config = {"hdencode_enabled": True}
        configure_hdencode_coordinator(real_config, real_db)
        get_hdencode_coordinator().observe_challenge()
        assert get_hdencode_coordinator().snapshot()["blocked"] is True

        # A minimal bridge shaped like ScannerAppBridge/_ScannerAppBridge in
        # production before HDE-6: it exposes `config` but has no `db`
        # attribute at all, so DetailScraper's
        # getattr(parent_app, "db", None) resolves to None.
        bridge = types.SimpleNamespace(
            config=real_config,
            clean_string=lambda s: s,
            safe_log=lambda message, level="info": None,
            log=lambda message, level="info": None,
            parse_size=lambda size_str: 0.0,
        )
        assert not hasattr(bridge, "db")
        # The in-memory protection fields are what the identity reset used to
        # wipe. With a real db attached, observe_challenge() ALSO persisted the
        # cooldown to the database, so snapshot()["blocked"] alone would stay
        # True even with the reset restored (the DB read masks the wipe): a
        # first version of this test asserted only "blocked" and did NOT fail
        # against that mutant. Pin the fields themselves.
        coordinator = get_hdencode_coordinator()
        local_cooldown_before = coordinator._local_cooldown_until
        streak_before = coordinator._block_streak
        assert local_cooldown_before is not None

        DetailScraper(bridge)

        snapshot = coordinator.snapshot()
        assert snapshot["blocked"] is True
        assert coordinator._local_cooldown_until == local_cooldown_before
        assert coordinator._block_streak == streak_before
        assert coordinator._local_cooldown_reason is not None
        assert coordinator._db is real_db
    finally:
        real_db.close()


def test_the_api_bridge_now_exposes_the_database():
    from backend.api.dependencies import ScannerAppBridge

    class _MinimalBackend:
        def __init__(self):
            self.config = {}
            self.db = object()
            self.tmdb_cache = None
            self.omdb_cache = None

    backend = _MinimalBackend()
    bridge = ScannerAppBridge(backend)
    assert bridge.db is backend.db


def test_the_desktop_bridge_now_exposes_the_database():
    pytest.importorskip("PySide6.QtCore")  # repo idiom; skips cleanly on CI without Qt
    from ui.controllers.scanner_controller import _ScannerAppBridge

    class _MinimalBackend:
        def __init__(self):
            self.config = {}
            self.db = object()
            self.tmdb_cache = None
            self.omdb_cache = None

    backend = _MinimalBackend()
    bridge = _ScannerAppBridge(backend)
    assert bridge.db is backend.db
