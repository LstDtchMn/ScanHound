"""Tests for all API route modules."""
import json
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.dependencies import registry, ServiceRegistry


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the module-level registry between tests."""
    yield
    # Reset state that persists across tests
    registry.config = {}
    registry.backend = None
    registry.db = None
    registry.bridge = None
    registry._scanner_service = None
    registry._plex_service = None
    registry._download_service = None
    registry._auto_grab_service = None
    registry._notification_bridge = None
    registry._watchlist_manager = None
    registry._analytics_dashboard = None


@pytest.fixture(autouse=True)
def _reset_results_selection():
    """Reset results selection state between tests."""
    from backend.api.routes.results import _selected, _selected_lock
    with _selected_lock:
        _selected.clear()
    yield
    with _selected_lock:
        _selected.clear()


@pytest.fixture(autouse=True)
def _reset_dismissed():
    """Clear persisted dismissals between tests (the DB path is shared)."""
    def _clear():
        try:
            from backend.database import DatabaseManager
            dm = DatabaseManager()
            dm.clear_dismissed_items()
            dm.close()
        except Exception:
            pass
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_scan_state():
    """Reset scan state between tests."""
    from backend.api.routes.scanner import _scan_state, _scan_lock, _items_lock
    import backend.api.routes.scanner as _scanner
    with _scan_lock:
        _scan_state["state"] = "idle"
        _scan_state["progress"] = 0.0
        _scan_state["phase"] = ""
        _scan_state["scanned"] = 0
        _scan_state["total"] = 0
        _scan_state["holds_slot"] = False
    # The last-scan items are a module global shared across tests; without
    # clearing them a prior scan test leaks results into e.g.
    # test_export_csv_no_results (which then gets 200 instead of 400).
    with _items_lock:
        _scanner._last_scan_items.clear()
    yield


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


# ── Settings ──────────────────────────────────────────────────────────

class TestSettings:
    def test_get_settings(self, client):
        # Set a value first to ensure config is non-empty
        client.put("/settings", json={"theme_mode": "dark"})
        resp = client.get("/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data.get("theme_mode") == "dark"

    def test_get_settings_masks_sensitive(self, client):
        # Set a sensitive value and verify it gets masked
        client.put("/settings", json={"plex_token": "secret123"})
        resp = client.get("/settings")
        data = resp.json()
        assert data["plex_token"] != "secret123"

    def test_get_settings_masks_all_passwords(self, client):
        # Every password/secret — incl. jd_password & plex_password — must be masked.
        client.put("/settings", json={
            "jd_password": "jdsecret", "plex_password": "plexsecret",
            "adithd_password": "aditsecret", "smtp_password": "smtpsecret",
        })
        data = client.get("/settings").json()
        for k, secret in [("jd_password", "jdsecret"), ("plex_password", "plexsecret"),
                          ("adithd_password", "aditsecret"), ("smtp_password", "smtpsecret")]:
            assert data.get(k) != secret, f"{k} was returned in plaintext"

    def test_put_settings_partial_update(self, client):
        resp = client.put("/settings", json={"theme_mode": "light"})
        assert resp.status_code == 200
        assert "theme_mode" in resp.json()["updated_keys"]

    def test_put_settings_multiple_keys(self, client):
        resp = client.put("/settings", json={"theme_mode": "dark", "tile_columns": 3})
        assert resp.status_code == 200
        keys = resp.json()["updated_keys"]
        assert "theme_mode" in keys
        assert "tile_columns" in keys

    def test_put_settings_rejects_unknown_keys(self, client):
        """Unknown keys should be rejected with 422 (extra='forbid')."""
        resp = client.put("/settings", json={"theme_mode": "dark", "bogus_key": 123})
        assert resp.status_code == 422

    def test_ollama_vision_model_round_trips(self, client):
        """The vision-model config key must be settable/gettable independently
        of ollama_model — the vision rung's model source (see
        TestLlmVisionModelRouting in test_rename_service.py)."""
        resp = client.put("/settings", json={"ollama_vision_model": "minicpm-v:latest"})
        assert resp.status_code == 200
        assert "ollama_vision_model" in resp.json()["updated_keys"]
        data = client.get("/settings").json()
        assert data.get("ollama_vision_model") == "minicpm-v:latest"

    def test_put_settings_ignores_masked_values(self, client):
        """Masked values (••••••••) should not be saved."""
        resp = client.put("/settings", json={"plex_token": "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"})
        assert resp.status_code == 200
        assert "plex_token" not in resp.json()["updated_keys"]

    def test_get_settings_masks_all_sensitive_keys(self, client):
        """All defined sensitive keys should be masked."""
        sensitive = {
            "plex_token": "t1", "tmdb_api_key": "t2", "omdb_api_key": "t3",
            "cuty_password": "t4", "adithd_password": "t5", "discord_webhook": "t6",
            "smtp_password": "t7", "pushover_token": "t8",
        }
        client.put("/settings", json=sensitive)
        resp = client.get("/settings")
        data = resp.json()
        for key in sensitive:
            assert data[key] == "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022", f"{key} was not masked"

    def test_put_settings_empty_body(self, client):
        resp = client.put("/settings", json={})
        assert resp.status_code == 200
        assert resp.json()["updated_keys"] == []

    def test_test_notification_unknown_channel(self, client):
        resp = client.post("/settings/test/nonexistent")
        assert resp.status_code == 400
        assert "Unknown channel" in resp.json()["detail"]

    def test_test_notification_discord_returns_result(self, client):
        # Clear the discord webhook so the test won't make a real HTTP call
        client.put("/settings", json={"discord_webhook": ""})
        resp = client.post("/settings/test/discord")
        assert resp.status_code == 400
        assert "detail" in resp.json()

    def test_test_notification_slack_returns_result(self, client):
        client.put("/settings", json={"slack_webhook": ""})
        resp = client.post("/settings/test/slack")
        assert resp.status_code == 400

    def test_test_notification_pushover_returns_result(self, client):
        client.put("/settings", json={"pushover_user": "", "pushover_token": ""})
        resp = client.post("/settings/test/pushover")
        assert resp.status_code == 400

    def test_test_notification_webhook_returns_result(self, client):
        client.put("/settings", json={"webhook_url": ""})
        resp = client.post("/settings/test/webhook")
        assert resp.status_code == 400

    def test_test_notification_email_returns_result(self, client):
        client.put("/settings", json={"smtp_host": ""})
        resp = client.post("/settings/test/email")
        assert resp.status_code == 400

    def test_test_notification_tmdb_returns_result(self, client):
        client.put("/settings", json={"tmdb_api_key": ""})
        resp = client.post("/settings/test/tmdb")
        assert resp.status_code == 400

    def test_test_notification_omdb_returns_result(self, client):
        client.put("/settings", json={"omdb_api_key": ""})
        resp = client.post("/settings/test/omdb")
        assert resp.status_code == 400

    def test_test_notification_plex_returns_result(self, client):
        resp = client.post("/settings/test/plex")
        assert resp.status_code == 400

    def test_ssrf_rejects_loopback_webhook(self, client):
        """SSRF protection: webhook URLs pointing to loopback must be rejected."""
        client.put("/settings", json={"webhook_url": "http://127.0.0.1:8080/evil"})
        resp = client.post("/settings/test/webhook")
        assert resp.status_code == 400
        assert "non-public address" in resp.json()["detail"]

    def test_ssrf_rejects_private_discord_webhook(self, client):
        """SSRF protection: Discord webhook pointing to private range must be rejected."""
        client.put("/settings", json={"discord_webhook": "http://192.168.1.1/hook"})
        resp = client.post("/settings/test/discord")
        assert resp.status_code == 400
        assert "non-public address" in resp.json()["detail"]

    def test_ssrf_rejects_ftp_scheme(self, client):
        """SSRF protection: non-HTTP(S) schemes must be rejected."""
        client.put("/settings", json={"slack_webhook": "ftp://example.com/hook"})
        resp = client.post("/settings/test/slack")
        assert resp.status_code == 400
        assert "scheme" in resp.json()["detail"].lower()


# ── Sources ───────────────────────────────────────────────────────────

class TestSources:
    def test_list_sources(self, client):
        resp = client.get("/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert "name" in data[0]

    def test_toggle_source(self, client):
        resp = client.put("/sources/hdencode", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["source"] == "hdencode"

    def test_toggle_source_enable(self, client):
        resp = client.put("/sources/hdencode", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_update_source_no_enabled_key(self, client):
        """Body without 'enabled' key should still return ok."""
        resp = client.put("/sources/hdencode", json={"other": "value"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_update_different_source_id(self, client):
        resp = client.put("/sources/mysource", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["source"] == "mysource"


# ── Plex ──────────────────────────────────────────────────────────────

class TestPlex:
    def test_plex_status(self, client):
        resp = client.get("/plex/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data

    def test_plex_libraries(self, client):
        resp = client.get("/plex/libraries")
        assert resp.status_code == 200
        data = resp.json()
        assert "movie_libraries" in data
        assert "tv_libraries" in data
        assert "known_libraries" in data

    def test_plex_stats(self, client):
        resp = client.get("/plex/stats")
        assert resp.status_code == 200

    def test_plex_status_fields(self, client):
        resp = client.get("/plex/status")
        data = resp.json()
        assert "connected" in data
        assert "server" in data
        assert "movie_count" in data
        assert "tv_count" in data

    def test_plex_connect(self, client):
        resp = client.post("/plex/connect")
        assert resp.status_code == 200
        assert resp.json()["status"] == "connecting"

    def test_plex_refresh(self, client):
        resp = client.post("/plex/refresh")
        assert resp.status_code == 200
        assert resp.json()["status"] == "refreshing"

    def test_plex_update_libraries(self, client):
        resp = client.put("/plex/libraries", json={
            "movie_libraries": ["Movies"],
            "tv_libraries": ["TV Shows"],
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # Verify the config was updated
        resp2 = client.get("/plex/libraries")
        data = resp2.json()
        assert data["movie_libraries"] == ["Movies"]
        assert data["tv_libraries"] == ["TV Shows"]


# ── Scanner ───────────────────────────────────────────────────────────

class TestScanner:
    def test_scan_status_idle(self, client):
        resp = client.get("/scan/status")
        assert resp.status_code == 200
        assert resp.json()["state"] == "idle"

    def test_scan_stop(self, client):
        resp = client.post("/scan/stop")
        assert resp.status_code == 200

    def test_scan_status_fields(self, client):
        resp = client.get("/scan/status")
        data = resp.json()
        assert "state" in data
        assert "progress" in data
        assert "phase" in data

    def test_scan_status_never_reports_running_without_holding_slot(self, client):
        """B3: _scan_state['state']='running' alone must not make /scan/status
        report running -- only when this scan thread actually holds the
        ScannerService scan slot (holds_slot=True). Reproduces the
        scan_start()-vs-_run_scan race window where the optimistic claim
        hasn't (or never will) be backed by a real slot acquisition."""
        from backend.api.routes.scanner import _scan_state, _scan_lock
        with _scan_lock:
            _scan_state["state"] = "running"
            _scan_state["holds_slot"] = False  # optimistic claim, slot not acquired
        resp = client.get("/scan/status")
        assert resp.json()["state"] == "idle"

    def test_scan_status_reports_running_when_slot_actually_held(self, client):
        from unittest.mock import MagicMock
        from backend.api.routes.scanner import _scan_state, _scan_lock
        from backend.api.dependencies import registry
        mock_scanner = MagicMock()
        mock_scanner.scan_in_progress = True
        registry._scanner_service = mock_scanner
        with _scan_lock:
            _scan_state["state"] = "running"
            _scan_state["holds_slot"] = True
        resp = client.get("/scan/status")
        assert resp.json()["state"] == "running"

    def test_scan_status_does_not_leak_holds_slot_field(self, client):
        resp = client.get("/scan/status")
        assert "holds_slot" not in resp.json()

    def test_scan_status_running_but_slot_since_released_reports_idle(self, client):
        """Covers a hard-crashed scan thread that set holds_slot=True but died
        before its `finally` released the slot and reset state -- the
        ScannerService's own slot state is the final cross-check."""
        from unittest.mock import MagicMock
        from backend.api.routes.scanner import _scan_state, _scan_lock
        from backend.api.dependencies import registry
        mock_scanner = MagicMock()
        mock_scanner.scan_in_progress = False  # slot was released/never truly held
        registry._scanner_service = mock_scanner
        with _scan_lock:
            _scan_state["state"] = "running"
            _scan_state["holds_slot"] = True
        resp = client.get("/scan/status")
        assert resp.json()["state"] == "idle"

    def test_scan_stop_returns_stopping(self, client):
        resp = client.post("/scan/stop")
        assert resp.json()["status"] == "stopping"

    def test_scan_start(self, client):
        resp = client.post("/scan/start", json={
            "type": "deep",
            "source": "HDEncode",
            "pages": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"

    def test_scan_start_default_params(self, client):
        resp = client.post("/scan/start", json={})
        assert resp.status_code == 200

    def test_scan_start_search_type(self, client):
        resp = client.post("/scan/start", json={
            "type": "search",
            "search_query": "inception",
        })
        assert resp.status_code == 200

    def test_scan_start_with_sources_list(self, client):
        resp = client.post("/scan/start", json={
            "type": "deep",
            "sources": ["HDEncode"],
        })
        assert resp.status_code == 200

    def test_scan_start_with_flags(self, client):
        resp = client.post("/scan/start", json={
            "type": "deep",
            "flags": {"4k": True, "1080p": False},
        })
        assert resp.status_code == 200


# ── Results ───────────────────────────────────────────────────────────

class TestResults:
    def test_get_results_empty(self, client):
        resp = client.get("/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_select_items(self, client):
        resp = client.post("/results/select", json={"group_keys": ["test|S0"], "selected": True})
        assert resp.status_code == 200
        assert resp.json()["selected_count"] == 1

    def test_deselect_all(self, client):
        client.post("/results/select", json={"group_keys": ["a|S0", "b|S0"], "selected": True})
        resp = client.post("/results/deselect-all")
        assert resp.status_code == 200
        assert resp.json()["selected_count"] == 0

    def test_select_multiple_items(self, client):
        resp = client.post("/results/select", json={
            "group_keys": ["a|S0", "b|S0", "c|S0"],
            "selected": True,
        })
        assert resp.status_code == 200
        assert resp.json()["selected_count"] == 3

    def test_deselect_specific_items(self, client):
        client.post("/results/select", json={"group_keys": ["a|S0", "b|S0"], "selected": True})
        resp = client.post("/results/select", json={"group_keys": ["a|S0"], "selected": False})
        assert resp.status_code == 200
        assert resp.json()["selected_count"] == 1

    def test_select_all_empty(self, client):
        resp = client.post("/results/select-all")
        assert resp.status_code == 200
        assert resp.json()["selected_count"] == 0

    def test_results_pagination_params(self, client):
        resp = client.get("/results?page=1&per_page=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 1
        assert data["per_page"] == 50

    def test_results_sort_params(self, client):
        resp = client.get("/results?sort=title&order=desc")
        assert resp.status_code == 200

    def test_results_filter_param(self, client):
        resp = client.get("/results?filter=missing")
        assert resp.status_code == 200

    def test_results_search_param(self, client):
        resp = client.get("/results?search=inception")
        assert resp.status_code == 200

    def test_results_response_structure(self, client):
        resp = client.get("/results")
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert "per_page" in data
        assert "stats" in data
        assert "filtered_stats" in data

    def test_results_stats_structure(self, client):
        resp = client.get("/results")
        stats = resp.json()["stats"]
        assert "total" in stats
        assert "missing" in stats
        assert "upgrade" in stats
        assert "library" in stats

    def test_export_csv_no_results(self, client):
        resp = client.post("/results/export")
        # No scan results to export → 400
        assert resp.status_code == 400
        assert "no results" in resp.json()["detail"].lower()

    def test_select_requires_group_keys(self, client):
        resp = client.post("/results/select", json={"selected": True})
        assert resp.status_code == 422

    def test_per_page_max_validation(self, client):
        resp = client.get("/results?per_page=501")
        assert resp.status_code == 422

    def test_page_min_validation(self, client):
        resp = client.get("/results?page=0")
        assert resp.status_code == 422


class TestDismiss:
    def test_dismiss_adds_items(self, client):
        resp = client.post("/results/dismiss", json={
            "urls": ["http://x/a", "http://x/b"],
            "titles": {"http://x/a": "Movie A"},
        })
        assert resp.status_code == 200
        assert resp.json()["dismissed_count"] == 2

    def test_dismiss_is_idempotent(self, client):
        client.post("/results/dismiss", json={"urls": ["http://x/a"]})
        resp = client.post("/results/dismiss", json={"urls": ["http://x/a"]})
        assert resp.json()["dismissed_count"] == 1

    def test_undismiss_removes_item(self, client):
        client.post("/results/dismiss", json={"urls": ["http://x/a", "http://x/b"]})
        resp = client.post("/results/dismiss", json={"urls": ["http://x/a"], "dismissed": False})
        assert resp.json()["dismissed_count"] == 1

    def test_list_dismissed(self, client):
        client.post("/results/dismiss", json={"urls": ["http://x/a"], "titles": {"http://x/a": "A"}})
        resp = client.get("/results/dismissed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["url"] == "http://x/a"
        assert data["items"][0]["title"] == "A"

    def test_clear_dismissed(self, client):
        client.post("/results/dismiss", json={"urls": ["http://x/a", "http://x/b"]})
        resp = client.delete("/results/dismissed")
        assert resp.status_code == 200
        assert resp.json()["dismissed_count"] == 0
        assert client.get("/results/dismissed").json()["count"] == 0


# ── Downloads ─────────────────────────────────────────────────────────

class TestDownloads:
    def test_download_history_empty(self, client):
        resp = client.get("/download/history")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_download_requires_url(self, client):
        resp = client.post("/download", json={"title": "test"})
        assert resp.status_code == 422  # missing required field 'url'

    def test_download_valid_request(self, client):
        resp = client.post("/download", json={
            "url": "https://example.com/file.torrent",
            "title": "Test Movie",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_download_with_all_fields(self, client):
        resp = client.post("/download", json={
            "url": "https://example.com/file.torrent",
            "title": "Test Movie",
            "season": 2,
            "resolution": "4K",
            "size": "15 GB",
        })
        assert resp.status_code == 200

    def test_download_batch_valid(self, client):
        resp = client.post("/download/batch", json={
            "items": [
                {"url": "https://example.com/1.torrent", "title": "Movie 1"},
                {"url": "https://example.com/2.torrent", "title": "Movie 2"},
            ]
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_download_batch_empty_items(self, client):
        resp = client.post("/download/batch", json={"items": []})
        assert resp.status_code == 200

    def test_download_batch_missing_url(self, client):
        resp = client.post("/download/batch", json={
            "items": [{"title": "no url"}]
        })
        assert resp.status_code == 422

    def test_open_plex_not_found(self, client):
        resp = client.post("/download/open-plex", json={
            "title": "Nonexistent Movie That Does Not Exist 9999",
            "year": 2099,
        })
        # Title not found in Plex → 404
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_open_plex_with_all_fields(self, client):
        resp = client.post("/download/open-plex", json={
            "title": "Nonexistent Movie 9999",
            "year": 2099,
            "season": None,
            "imdb_id": "tt0000000",
            "plex_rating_key": "99999",
        })
        # Not found → 404
        assert resp.status_code == 404

    def test_open_plex_requires_title(self, client):
        resp = client.post("/download/open-plex", json={})
        assert resp.status_code == 422

    def test_download_history_with_limit(self, client):
        resp = client.get("/download/history?limit=10")
        assert resp.status_code == 200

    def test_download_no_body(self, client):
        resp = client.post("/download")
        assert resp.status_code == 422


# ── JD Controls & Download Results ──────────────────────────────────────

class TestJdControlsAndResults:
    """/download/jd-state, /jd-status, /jd-control (POST), /results (GET/DELETE)."""

    @pytest.fixture(autouse=True)
    def _clear_download_results(self, client):
        registry.db.clear_download_results()
        yield

    # -- jd-state --

    def test_jd_state_disabled_by_default(self, client):
        resp = client.get("/download/jd-state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert data["state"] == "unknown"
        assert "error" in data

    def test_jd_state_no_service_returns_503(self, client):
        registry._download_service = None
        resp = client.get("/download/jd-state")
        assert resp.status_code == 503

    def test_jd_state_enabled_connected(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        mock_dl.get_jd_state.return_value = {"connected": True, "state": "running"}
        registry._download_service = mock_dl
        resp = client.get("/download/jd-state")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "state": "running"}

    def test_jd_state_enabled_disconnected(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        mock_dl.get_jd_state.return_value = {"connected": False, "error": "no device", "state": "unknown"}
        registry._download_service = mock_dl
        resp = client.get("/download/jd-state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert data["error"] == "no device"

    def test_jd_state_wrong_method_treated_as_disabled(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "folder"
        resp = client.get("/download/jd-state")
        assert resp.status_code == 200
        assert resp.json()["connected"] is False

    # -- jd-status --

    def test_jd_status_disabled_by_default(self, client):
        resp = client.get("/download/jd-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert data["links"] == []
        assert data["total"] == 0

    def test_jd_status_no_service_returns_503(self, client):
        registry._download_service = None
        resp = client.get("/download/jd-status")
        assert resp.status_code == 503

    def test_jd_status_enabled_success(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        mock_dl.get_jd_status.return_value = {
            "connected": True, "links": [{"name": "x"}], "online": 1, "offline": 0,
            "total": 1, "state": "running",
        }
        registry._download_service = mock_dl
        resp = client.get("/download/jd-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        assert data["total"] == 1

    # -- jd-control (POST) --

    def test_jd_control_no_service_returns_503(self, client):
        registry._download_service = None
        resp = client.post("/download/jd-control", json={"action": "start"})
        assert resp.status_code == 503

    def test_jd_control_disabled_returns_400(self, client):
        resp = client.post("/download/jd-control", json={"action": "start"})
        assert resp.status_code == 400
        assert "settings" in resp.json()["detail"].lower()

    def test_jd_control_wrong_method_returns_400(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "folder"
        resp = client.post("/download/jd-control", json={"action": "start"})
        assert resp.status_code == 400

    def test_jd_control_invalid_action_returns_400(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        registry._download_service = mock_dl
        resp = client.post("/download/jd-control", json={"action": "frobnicate"})
        assert resp.status_code == 400
        assert "unknown" in resp.json()["detail"].lower()
        mock_dl.jd_control.assert_not_called()

    @pytest.mark.parametrize("action", ["start", "stop", "pause", "resume"])
    def test_jd_control_valid_actions_succeed(self, client, action):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        mock_dl.jd_control.return_value = {"ok": True, "state": "running", "action": action}
        registry._download_service = mock_dl
        resp = client.post("/download/jd-control", json={"action": action})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_dl.jd_control.assert_called_once_with(action)

    def test_jd_control_failure_returns_502(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        mock_dl.jd_control.return_value = {"ok": False, "error": "device offline"}
        registry._download_service = mock_dl
        resp = client.post("/download/jd-control", json={"action": "start"})
        assert resp.status_code == 502
        assert resp.json()["detail"] == "device offline"

    def test_jd_control_failure_without_error_message_uses_default(self, client):
        registry.config["jd_enabled"] = True
        registry.config["jd_method"] = "api"
        mock_dl = MagicMock()
        mock_dl.jd_control.return_value = {"ok": False}
        registry._download_service = mock_dl
        resp = client.post("/download/jd-control", json={"action": "stop"})
        assert resp.status_code == 502
        assert "failed" in resp.json()["detail"].lower()

    def test_jd_control_requires_action(self, client):
        resp = client.post("/download/jd-control", json={})
        assert resp.status_code == 422

    # -- results (GET/DELETE) --

    def test_results_empty_by_default(self, client):
        resp = client.get("/download/results")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_results_no_db_returns_empty_list(self, client):
        registry.db = None
        resp = client.get("/download/results")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_results_returns_tracked_packages(self, client):
        registry.db.upsert_download_result("Some.Movie.2024.1080p", title="Some Movie", state="downloading")
        resp = client.get("/download/results")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Some.Movie.2024.1080p"
        assert data[0]["title"] == "Some Movie"
        assert data[0]["state"] == "downloading"

    def test_results_respects_limit(self, client):
        for i in range(5):
            registry.db.upsert_download_result(f"pkg-{i}", state="queued")
        resp = client.get("/download/results?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_clear_results(self, client):
        registry.db.upsert_download_result("pkg-1", state="downloaded")
        resp = client.delete("/download/results")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cleared"
        assert client.get("/download/results").json() == []

    def test_clear_results_no_db_is_noop(self, client):
        registry.db = None
        resp = client.delete("/download/results")
        assert resp.status_code == 200

    # -- results/remove (POST) --

    def test_remove_download_result_endpoint(self, client):
        mock_dl = MagicMock()
        mock_dl.remove_package.return_value = {"ok": True, "removed": 1}
        registry._download_service = mock_dl
        resp = client.post("/download/results/remove", json={"name": "Foo [1080p]"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": 1}
        mock_dl.remove_package.assert_called_once_with("Foo [1080p]")

    def test_remove_download_result_no_service_returns_503(self, client):
        registry._download_service = None
        resp = client.post("/download/results/remove", json={"name": "Foo [1080p]"})
        assert resp.status_code == 503


# ── Analytics ─────────────────────────────────────────────────────────

class TestAnalytics:
    def test_analytics_summary(self, client):
        resp = client.get("/analytics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "library" in data
        assert "scans" in data
        assert "trends" in data
        assert "quality_breakdown" in data
        assert "generated_at" in data

    def test_analytics_summary_library_structure(self, client):
        resp = client.get("/analytics/summary")
        library = resp.json()["library"]
        assert "movies" in library
        assert "tv_shows" in library
        assert "total_items" in library
        assert "total_size_gb" in library

    def test_analytics_library_movies(self, client):
        resp = client.get("/analytics/library?mode=Movies")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_items" in data
        assert "total_size_gb" in data
        assert "resolution_counts" in data
        assert "hdr_count" in data
        assert "quality_score" in data

    def test_analytics_library_tv(self, client):
        resp = client.get("/analytics/library?mode=TV Shows")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_items" in data

    def test_analytics_library_invalid_mode(self, client):
        resp = client.get("/analytics/library?mode=Invalid")
        assert resp.status_code == 422

    def test_analytics_scans(self, client):
        resp = client.get("/analytics/scans")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_scans" in data
        assert "avg_duration" in data
        assert "total_items_scanned" in data
        assert "total_missing_found" in data
        assert "total_upgrades_found" in data

    def test_analytics_scans_custom_days(self, client):
        resp = client.get("/analytics/scans?days=7")
        assert resp.status_code == 200

    def test_analytics_scans_max_days(self, client):
        resp = client.get("/analytics/scans?days=365")
        assert resp.status_code == 200

    def test_analytics_scans_invalid_days_zero(self, client):
        resp = client.get("/analytics/scans?days=0")
        assert resp.status_code == 422

    def test_analytics_scans_invalid_days_over_max(self, client):
        resp = client.get("/analytics/scans?days=366")
        assert resp.status_code == 422

    def test_analytics_trends(self, client):
        resp = client.get("/analytics/trends")
        assert resp.status_code == 200
        data = resp.json()
        assert "dates" in data
        assert "items_scanned" in data
        assert "missing_found" in data
        assert "upgrades_found" in data
        assert "avg_duration" in data
        assert "scan_count" in data

    def test_analytics_trends_custom_days(self, client):
        resp = client.get("/analytics/trends?days=14")
        assert resp.status_code == 200

    def test_analytics_trends_invalid_days(self, client):
        resp = client.get("/analytics/trends?days=0")
        assert resp.status_code == 422

    def test_analytics_quality_movies(self, client):
        resp = client.get("/analytics/quality?mode=Movies")
        assert resp.status_code == 200
        data = resp.json()
        assert "resolution" in data
        assert "hdr" in data
        assert "labels" in data["resolution"]
        assert "counts" in data["resolution"]
        assert "labels" in data["hdr"]
        assert "counts" in data["hdr"]

    def test_analytics_quality_tv(self, client):
        resp = client.get("/analytics/quality?mode=TV Shows")
        assert resp.status_code == 200

    def test_analytics_quality_invalid_mode(self, client):
        resp = client.get("/analytics/quality?mode=Podcasts")
        assert resp.status_code == 422

    def test_analytics_export_json(self, client):
        resp = client.get("/analytics/export?format=json")
        assert resp.status_code == 200
        data = resp.json()
        assert "library" in data

    def test_analytics_export_html(self, client):
        resp = client.get("/analytics/export?format=html")
        assert resp.status_code == 200
        assert "ScanHound" in resp.text

    def test_analytics_export_invalid_format(self, client):
        resp = client.get("/analytics/export?format=csv")
        assert resp.status_code == 422

    def test_analytics_export_default_format(self, client):
        resp = client.get("/analytics/export")
        assert resp.status_code == 200
        # Default is json
        data = resp.json()
        assert isinstance(data, dict)

    def test_analytics_library_default_mode(self, client):
        """Default mode should be Movies."""
        resp = client.get("/analytics/library")
        assert resp.status_code == 200

    def test_analytics_quality_default_mode(self, client):
        """Default mode should be Movies."""
        resp = client.get("/analytics/quality")
        assert resp.status_code == 200

    def test_analytics_scans_default_days(self, client):
        """Default days should be 30."""
        resp = client.get("/analytics/scans")
        assert resp.status_code == 200

    def test_analytics_trends_arrays_same_length(self, client):
        """All trend arrays should be the same length."""
        resp = client.get("/analytics/trends")
        data = resp.json()
        lengths = [
            len(data["dates"]),
            len(data["items_scanned"]),
            len(data["missing_found"]),
            len(data["upgrades_found"]),
            len(data["avg_duration"]),
            len(data["scan_count"]),
        ]
        assert len(set(lengths)) == 1  # All same length


# ── Watchlist ─────────────────────────────────────────────────────────

class TestWatchlist:
    def test_list_empty_watchlist(self, client):
        resp = client.get("/watchlist")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_add_movie(self, client):
        resp = client.post("/watchlist", json={
            "title": "Inception",
            "year": 2010,
            "item_type": "movie",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "added"

    def test_add_tv_show(self, client):
        resp = client.post("/watchlist", json={
            "title": "Breaking Bad",
            "year": 2008,
            "item_type": "tv_show",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "added"

    def test_add_tv_season(self, client):
        resp = client.post("/watchlist", json={
            "title": "Breaking Bad",
            "year": 2008,
            "item_type": "tv_season",
            "season": 3,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "added"

    def test_add_tv_season_requires_season(self, client):
        resp = client.post("/watchlist", json={
            "title": "Breaking Bad",
            "item_type": "tv_season",
        })
        assert resp.status_code == 422

    def test_add_with_all_fields(self, client):
        resp = client.post("/watchlist", json={
            "title": "Dune",
            "year": 2021,
            "imdb_id": "tt1160419",
            "tmdb_id": "438631",
            "item_type": "movie",
            "min_resolution": "4K",
            "prefer_dovi": True,
            "notes": "Want DV version",
            "priority": 3,
        })
        assert resp.status_code == 200
        item_id = resp.json()["id"]
        assert item_id > 0

    def test_add_default_type_is_movie(self, client):
        resp = client.post("/watchlist", json={"title": "Test"})
        assert resp.status_code == 200

    def test_get_single_item(self, client):
        add_resp = client.post("/watchlist", json={"title": "Inception", "year": 2010})
        item_id = add_resp.json()["id"]
        resp = client.get(f"/watchlist/{item_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Inception"
        assert data["year"] == 2010
        assert data["item_type"] == "movie"
        assert data["status"] == "wanted"

    def test_get_nonexistent_item(self, client):
        resp = client.get("/watchlist/99999")
        assert resp.status_code == 404

    def test_update_item(self, client):
        add_resp = client.post("/watchlist", json={"title": "Inception", "year": 2010})
        item_id = add_resp.json()["id"]
        resp = client.put(f"/watchlist/{item_id}", json={
            "status": "found",
            "notes": "Found on HDEncode",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"
        # Verify the update
        get_resp = client.get(f"/watchlist/{item_id}")
        data = get_resp.json()
        assert data["status"] == "found"
        assert data["notes"] == "Found on HDEncode"

    def test_update_nonexistent_item(self, client):
        resp = client.put("/watchlist/99999", json={"title": "Nope"})
        assert resp.status_code == 404

    def test_update_item_type(self, client):
        add_resp = client.post("/watchlist", json={"title": "Test", "item_type": "movie"})
        item_id = add_resp.json()["id"]
        resp = client.put(f"/watchlist/{item_id}", json={"item_type": "tv_show"})
        assert resp.status_code == 200

    def test_update_to_tv_season_requires_season(self, client):
        add_resp = client.post("/watchlist", json={"title": "Test", "item_type": "movie"})
        item_id = add_resp.json()["id"]
        resp = client.put(f"/watchlist/{item_id}", json={"item_type": "tv_season"})
        assert resp.status_code == 422

    def test_update_to_tv_season_with_season(self, client):
        add_resp = client.post("/watchlist", json={"title": "Test", "item_type": "movie"})
        item_id = add_resp.json()["id"]
        resp = client.put(f"/watchlist/{item_id}", json={
            "item_type": "tv_season",
            "season": 1,
        })
        assert resp.status_code == 200

    def test_delete_item(self, client):
        add_resp = client.post("/watchlist", json={"title": "Inception"})
        item_id = add_resp.json()["id"]
        resp = client.delete(f"/watchlist/{item_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"
        # Verify deletion
        get_resp = client.get(f"/watchlist/{item_id}")
        assert get_resp.status_code == 404

    def test_delete_nonexistent_item(self, client):
        resp = client.delete("/watchlist/99999")
        assert resp.status_code == 404

    def test_watchlist_stats(self, client):
        resp = client.get("/watchlist/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_status" in data
        assert "by_type" in data
        assert "recent_additions" in data
        assert "recently_found" in data
        assert isinstance(data["total"], int)

    def test_watchlist_stats_increases_after_add(self, client):
        initial = client.get("/watchlist/stats").json()["total"]
        client.post("/watchlist", json={"title": f"StatsTest_{initial}"})
        after = client.get("/watchlist/stats").json()["total"]
        assert after == initial + 1

    def test_search_watchlist(self, client):
        # Use a unique title to avoid conflicts with existing DB data
        unique_title = "UniqueSearchTest_XYZ_12345"
        client.post("/watchlist", json={"title": unique_title})
        resp = client.get(f"/watchlist/search?q={unique_title}")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1
        assert any(i["title"] == unique_title for i in items)

    def test_search_watchlist_no_results(self, client):
        resp = client.get("/watchlist/search?q=zzz_nonexistent_title_zzz")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_requires_query(self, client):
        resp = client.get("/watchlist/search")
        assert resp.status_code == 422

    def test_search_empty_query(self, client):
        resp = client.get("/watchlist/search?q=")
        assert resp.status_code == 422

    def test_list_filter_by_status(self, client):
        client.post("/watchlist", json={"title": "Movie 1"})
        resp = client.get("/watchlist?status=wanted")
        assert resp.status_code == 200
        items = resp.json()
        for item in items:
            assert item["status"] == "wanted"

    def test_list_filter_by_type(self, client):
        client.post("/watchlist", json={"title": "FilterTypeMovie", "item_type": "movie"})
        client.post("/watchlist", json={"title": "FilterTypeShow", "item_type": "tv_show"})
        resp = client.get("/watchlist?item_type=movie")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1
        for item in items:
            assert item["item_type"] == "movie"

    def test_list_filter_invalid_status(self, client):
        resp = client.get("/watchlist?status=invalid")
        assert resp.status_code == 422

    def test_list_filter_invalid_type(self, client):
        resp = client.get("/watchlist?item_type=invalid")
        assert resp.status_code == 422

    def test_import_json(self, client):
        json_data = json.dumps([
            {"title": "Movie 1", "year": 2020, "item_type": "movie", "status": "wanted"},
            {"title": "Movie 2", "year": 2021, "item_type": "movie", "status": "wanted"},
        ])
        resp = client.post(
            "/watchlist/import/json",
            content=json_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported"] == 2

    def test_import_json_invalid(self, client):
        resp = client.post(
            "/watchlist/import/json",
            content="not valid json{{{",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 400

    def test_import_imdb_csv(self, client):
        csv_data = "Position,Const,Created,Modified,Description,Title,URL,Title Type,IMDb Rating,Runtime (mins),Year,Genres,Num Votes,Release Date,Directors\n"
        csv_data += '1,tt1375666,2020-01-01,,,"Inception",https://imdb.com,movie,8.8,148,2010,,,,\n'
        resp = client.post(
            "/watchlist/import/imdb",
            content=csv_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] >= 1

    def test_import_imdb_empty(self, client):
        resp = client.post(
            "/watchlist/import/imdb",
            content="",
            headers={"Content-Type": "text/plain"},
        )
        # Empty body may return 400 (route validation) or 422 (pydantic)
        assert resp.status_code in (400, 422)

    def test_import_letterboxd_csv(self, client):
        csv_data = "Date,Name,Year,Letterboxd URI,Rating\n"
        csv_data += "2020-01-01,Inception,2010,https://letterboxd.com/film/inception/,5\n"
        resp = client.post(
            "/watchlist/import/letterboxd",
            content=csv_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] >= 1

    def test_import_letterboxd_empty(self, client):
        resp = client.post(
            "/watchlist/import/letterboxd",
            content="",
            headers={"Content-Type": "text/plain"},
        )
        # Empty body may return 400 (route validation) or 422 (pydantic)
        assert resp.status_code in (400, 422)

    def test_export_json(self, client):
        client.post("/watchlist", json={"title": "Inception", "year": 2010})
        resp = client.get("/watchlist/export/json")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "count" in data
        assert data["count"] >= 1

    def test_export_json_structure(self, client):
        resp = client.get("/watchlist/export/json")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert "items" in data
        assert isinstance(data["items"], list)
        assert data["count"] == len(data["items"])

    def test_clear_watchlist(self, client):
        client.post("/watchlist", json={"title": "ClearTest1"})
        client.post("/watchlist", json={"title": "ClearTest2"})
        resp = client.delete("/watchlist")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cleared"
        # Verify cleared
        list_resp = client.get("/watchlist")
        assert list_resp.json() == []

    def test_clear_watchlist_by_status(self, client):
        # First clear everything to get a clean slate
        client.delete("/watchlist")
        client.post("/watchlist", json={"title": "ClearStatusTest1"})
        add_resp = client.post("/watchlist", json={"title": "ClearStatusTest2"})
        item_id = add_resp.json()["id"]
        # Update one to "found" status
        client.put(f"/watchlist/{item_id}", json={"status": "found"})
        # Clear only "wanted" items
        resp = client.delete("/watchlist?status=wanted")
        assert resp.status_code == 200
        # The "found" item should remain
        list_resp = client.get("/watchlist")
        items = list_resp.json()
        assert len(items) == 1
        assert items[0]["status"] == "found"

    def test_add_item_priority_validation(self, client):
        # Priority must be 1-3
        resp = client.post("/watchlist", json={"title": "Test", "priority": 0})
        assert resp.status_code == 422

    def test_add_item_priority_max_validation(self, client):
        resp = client.post("/watchlist", json={"title": "Test", "priority": 4})
        assert resp.status_code == 422

    def test_add_item_season_validation(self, client):
        # Season must be >= 1
        resp = client.post("/watchlist", json={
            "title": "Test",
            "item_type": "tv_season",
            "season": 0,
        })
        assert resp.status_code == 422

    def test_update_priority(self, client):
        add_resp = client.post("/watchlist", json={"title": "Test"})
        item_id = add_resp.json()["id"]
        resp = client.put(f"/watchlist/{item_id}", json={"priority": 3})
        assert resp.status_code == 200
        get_resp = client.get(f"/watchlist/{item_id}")
        assert get_resp.json()["priority"] == 3

    def test_update_all_statuses(self, client):
        """Test updating to each valid status."""
        add_resp = client.post("/watchlist", json={"title": "Test"})
        item_id = add_resp.json()["id"]
        for status in ("wanted", "found", "downloaded", "in_library"):
            resp = client.put(f"/watchlist/{item_id}", json={"status": status})
            assert resp.status_code == 200
            get_resp = client.get(f"/watchlist/{item_id}")
            assert get_resp.json()["status"] == status

    def test_import_json_with_items_key(self, client):
        """JSON import should support both list and {items: []} format."""
        json_data = json.dumps({
            "items": [
                {"title": "Movie 1", "year": 2020, "item_type": "movie", "status": "wanted"},
            ]
        })
        resp = client.post(
            "/watchlist/import/json",
            content=json_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 1

    def test_search_partial_match(self, client):
        unique = "UniqueDarkKnightTest_98765"
        client.post("/watchlist", json={"title": unique})
        resp = client.get(f"/watchlist/search?q=UniqueDarkKnightTest")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1
        assert any(i["title"] == unique for i in items)

    def test_add_duplicate_imdb_returns_existing_id(self, client):
        """Adding a duplicate IMDb ID should return the existing item ID."""
        resp1 = client.post("/watchlist", json={
            "title": "Inception", "imdb_id": "tt1375666",
        })
        id1 = resp1.json()["id"]
        resp2 = client.post("/watchlist", json={
            "title": "Inception Copy", "imdb_id": "tt1375666",
        })
        id2 = resp2.json()["id"]
        assert id1 == id2

    def test_clear_invalid_status(self, client):
        resp = client.delete("/watchlist?status=invalid")
        assert resp.status_code == 422

    def test_import_imdb_tv_series(self, client):
        """IMDb import should detect TV series type."""
        csv_data = "Position,Const,Created,Modified,Description,Title,URL,Title Type,IMDb Rating,Runtime (mins),Year,Genres,Num Votes,Release Date,Directors\n"
        csv_data += '1,tt0903747,2020-01-01,,,"Breaking Bad",https://imdb.com,tvSeries,9.5,49,2008,,,,\n'
        resp = client.post(
            "/watchlist/import/imdb",
            content=csv_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        # Verify it was imported as tv_show
        list_resp = client.get("/watchlist")
        items = list_resp.json()
        assert any(i["item_type"] == "tv_show" for i in items)


# ── Scheduler ────────────────────────────────────────────────────────


class TestScheduler:
    def test_status_defaults(self, client):
        """Scheduler status returns expected structure with correct types."""
        # Clear scheduler keys to test defaults (real config may have last_scan_time)
        for key in ("scheduler_enabled", "scheduler_interval", "scheduler_only_when_idle", "last_scan_time"):
            registry.config.pop(key, None)
        resp = client.get("/scheduler/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["interval_hours"] == 24
        assert data["idle_only"] is False
        assert data["last_run"] is None
        assert data["next_run"] is None
        assert isinstance(data["scheduler_active"], bool)

    def test_status_with_config(self, client):
        """Scheduler status reflects config values."""
        import time
        ts = time.time() - 3600  # 1 hour ago
        registry.config.update({
            "scheduler_enabled": True,
            "scheduler_interval": 6,
            "scheduler_only_when_idle": True,
            "last_scan_time": ts,
        })
        resp = client.get("/scheduler/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["interval_hours"] == 6
        assert data["idle_only"] is True
        assert data["last_run"] is not None
        assert data["next_run"] is not None  # 6h from 1h ago = 5h in future

    def test_status_no_next_run_when_past(self, client):
        """next_run is None when the next scheduled time is in the past."""
        import time
        registry.config.update({
            "scheduler_enabled": True,
            "scheduler_interval": 1,
            "last_scan_time": time.time() - 7200,  # 2h ago, interval=1h → past
        })
        resp = client.get("/scheduler/status")
        data = resp.json()
        assert data["next_run"] is None

    def test_config_update_enabled(self, client):
        """PUT /scheduler/config updates enabled flag."""
        resp = client.put("/scheduler/config", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["updated"]["scheduler_enabled"] is True
        assert registry.config["scheduler_enabled"] is True

    def test_config_update_interval(self, client):
        """PUT /scheduler/config updates interval."""
        resp = client.put("/scheduler/config", json={"interval_hours": 12})
        assert resp.status_code == 200
        assert resp.json()["updated"]["scheduler_interval"] == 12
        assert registry.config["scheduler_interval"] == 12

    def test_config_clamps_interval_low(self, client):
        """Interval is clamped to minimum of 1 hour."""
        resp = client.put("/scheduler/config", json={"interval_hours": 0})
        assert resp.status_code == 200
        assert resp.json()["updated"]["scheduler_interval"] == 1

    def test_config_clamps_interval_high(self, client):
        """Interval is clamped to maximum of 168 hours."""
        resp = client.put("/scheduler/config", json={"interval_hours": 500})
        assert resp.status_code == 200
        assert resp.json()["updated"]["scheduler_interval"] == 168

    def test_config_update_idle_only(self, client):
        """PUT /scheduler/config updates idle_only flag."""
        resp = client.put("/scheduler/config", json={"idle_only": True})
        assert resp.status_code == 200
        assert resp.json()["updated"]["scheduler_only_when_idle"] is True

    def test_config_update_multiple(self, client):
        """PUT /scheduler/config can update multiple fields at once."""
        resp = client.put("/scheduler/config", json={
            "enabled": True, "interval_hours": 8, "idle_only": True,
        })
        assert resp.status_code == 200
        updated = resp.json()["updated"]
        assert updated["scheduler_enabled"] is True
        assert updated["scheduler_interval"] == 8
        assert updated["scheduler_only_when_idle"] is True

    def test_config_empty_body(self, client):
        """PUT /scheduler/config with empty body updates nothing."""
        resp = client.put("/scheduler/config", json={})
        assert resp.status_code == 200
        assert resp.json()["updated"] == {}

    def test_config_no_backend_still_updates_dict(self, client):
        """Config dict is updated even if backend is None (save_config skipped)."""
        registry.backend = None
        resp = client.put("/scheduler/config", json={"enabled": True})
        assert resp.status_code == 200
        assert registry.config["scheduler_enabled"] is True

    def test_trigger_no_scanner(self, client):
        """POST /scheduler/trigger returns 503 when scanner not initialized."""
        registry._scanner_service = None
        resp = client.post("/scheduler/trigger")
        assert resp.status_code == 503

    def test_trigger_scan_in_progress(self, client):
        """POST /scheduler/trigger returns 409 when scan is already running."""
        from backend.api.routes.scanner import _scan_state, _scan_lock
        mock_scanner = MagicMock()
        mock_scanner.scan_in_progress = False  # 409 must come from the running-state check
        registry._scanner_service = mock_scanner
        with _scan_lock:
            _scan_state["state"] = "running"
        resp = client.post("/scheduler/trigger")
        assert resp.status_code == 409

    def test_trigger_success(self, client):
        """POST /scheduler/trigger starts a background scan using scanner route's _run_scan."""
        from backend.api.routes.scanner import _run_scan, ScanRequest
        mock_scanner = MagicMock()
        mock_scanner.is_scanning = False
        mock_scanner.scan_in_progress = False
        registry._scanner_service = mock_scanner
        with patch("backend.api.routes.scheduler.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            resp = client.post("/scheduler/trigger")
        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"
        # Verify the thread target is the scanner route's _run_scan
        mock_thread.assert_called_once()
        call_kwargs = mock_thread.call_args
        assert call_kwargs.kwargs["target"] is _run_scan
        # Verify the request is an incremental scan
        req_arg = call_kwargs.kwargs["args"][1]
        assert isinstance(req_arg, ScanRequest)
        assert req_arg.type == "incremental"

    def test_trigger_executes_run_scan_synchronously(self, client):
        """Execute _run_scan synchronously to verify it calls scanner.run_scan with correct args."""
        from backend.api.routes.scanner import _run_scan, ScanRequest
        mock_scanner = MagicMock()
        mock_scanner.is_scanning = False
        mock_scanner.run_scan.return_value = []
        registry._scanner_service = mock_scanner
        registry._auto_grab_service = None

        req = ScanRequest(type="incremental")
        with patch("backend.api.routes.scanner.ws_manager") as mock_ws:
            _run_scan(registry, req)

        # Verify scanner.run_scan was called with correct signature
        mock_scanner.run_scan.assert_called_once()
        call_kwargs = mock_scanner.run_scan.call_args
        assert call_kwargs.kwargs.get("scan_type") == "Incremental"
        # Verify WebSocket notifications were sent
        broadcast_calls = [c.args[0]["type"] for c in mock_ws.broadcast_sync.call_args_list]
        assert "scan:complete" in broadcast_calls


# ── Error Contract Tests ─────────────────────────────────────────────

class TestErrorContracts:
    """Verify failure paths return proper HTTP error codes, not 200."""

    def test_scan_start_already_running_returns_409(self, client):
        """Starting a scan while one is running should return 409 Conflict."""
        from backend.api.routes.scanner import _scan_state, _scan_lock
        with _scan_lock:
            _scan_state["state"] = "running"
        resp = client.post("/scan/start", json={"type": "deep"})
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"].lower()

    def test_download_no_service_returns_503(self, client):
        """Download with no service available should return 503."""
        registry._download_service = None
        resp = client.post("/download", json={
            "url": "https://example.com/file.torrent",
            "title": "Test",
        })
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_download_batch_no_service_returns_503(self, client):
        """Batch download with no service should return 503."""
        registry._download_service = None
        resp = client.post("/download/batch", json={
            "items": [{"url": "https://example.com/1.torrent", "title": "M1"}]
        })
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_open_plex_no_service_returns_503(self, client):
        """Open-in-Plex with no service should return 503."""
        registry._download_service = None
        registry._plex_service = None
        resp = client.post("/download/open-plex", json={"title": "Test"})
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_open_plex_not_found_returns_404(self, client):
        """Open-in-Plex when title not found should return 404."""
        mock_dl = MagicMock()
        mock_dl.open_in_plex.return_value = None
        mock_plex = MagicMock()
        mock_plex.plex_movies = []
        mock_plex.plex_tv = []
        registry._download_service = mock_dl
        registry._plex_service = mock_plex
        resp = client.post("/download/open-plex", json={
            "title": "Nonexistent Movie",
            "year": 2099,
        })
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_open_plex_upstream_error_returns_502(self, client):
        """Open-in-Plex when Plex lookup throws should return 502."""
        mock_dl = MagicMock()
        mock_dl.open_in_plex.side_effect = Exception("Plex connection refused")
        mock_plex = MagicMock()
        mock_plex.plex_movies = []
        mock_plex.plex_tv = []
        registry._download_service = mock_dl
        registry._plex_service = mock_plex
        resp = client.post("/download/open-plex", json={"title": "Test"})
        assert resp.status_code == 502
        assert "detail" in resp.json()

    def test_export_csv_no_service_returns_503(self, client):
        """Export CSV with no download service should return 503."""
        registry._download_service = None
        resp = client.post("/results/export")
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_export_csv_no_results_returns_400(self, client):
        """Export CSV with no scan results should return 400."""
        resp = client.post("/results/export")
        assert resp.status_code == 400
        assert "no results" in resp.json()["detail"].lower()

    def test_plex_connect_no_service_returns_503(self, client):
        """Plex connect with no service should return 503."""
        registry._plex_service = None
        resp = client.post("/plex/connect")
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_plex_refresh_no_service_returns_503(self, client):
        """Plex refresh with no service should return 503."""
        registry._plex_service = None
        resp = client.post("/plex/refresh")
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_error_responses_use_detail_field(self, client):
        """All error responses should use the 'detail' field consistently."""
        registry._download_service = None
        registry._plex_service = None
        error_endpoints = [
            ("POST", "/download", {"url": "https://x.com/f", "title": "T"}),
            ("POST", "/download/batch", {"items": [{"url": "https://x.com/f", "title": "T"}]}),
            ("POST", "/download/open-plex", {"title": "T"}),
            ("POST", "/plex/connect", None),
            ("POST", "/plex/refresh", None),
        ]
        for method, path, body in error_endpoints:
            if method == "POST":
                resp = client.post(path, json=body) if body else client.post(path)
            assert resp.status_code >= 400, f"{path} returned {resp.status_code}"
            data = resp.json()
            assert "detail" in data, f"{path} error response missing 'detail' field: {data}"


class TestRenameDedupKey:
    """The auto-rename hand-off hook must dedup by package_uuid, not name,
    so two extracted packages that share a display name are each handed to
    auto-rename instead of the second being skipped as a false "duplicate".
    """

    def test_same_name_distinct_uuid_yields_distinct_keys(self):
        from backend.api.main import _rename_dedup_key
        row_a = {"name": "Movie.Name.2024", "package_uuid": "uuid-aaa"}
        row_b = {"name": "Movie.Name.2024", "package_uuid": "uuid-bbb"}
        key_a = _rename_dedup_key(row_a)
        key_b = _rename_dedup_key(row_b)
        assert key_a != key_b
        assert key_a == "uuid-aaa"
        assert key_b == "uuid-bbb"

    def test_legacy_row_without_uuid_falls_back_to_name(self):
        from backend.api.main import _rename_dedup_key
        row = {"name": "Legacy.Package.2020", "package_uuid": None}
        assert _rename_dedup_key(row) == "Legacy.Package.2020"

    def test_row_missing_both_returns_empty_string(self):
        from backend.api.main import _rename_dedup_key
        assert _rename_dedup_key({}) == ""
