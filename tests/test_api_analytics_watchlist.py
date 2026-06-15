"""Tests for analytics and watchlist API route modules."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry, ServiceRegistry
from backend.watchlist import WatchlistItem, WatchlistItemStatus, WatchlistItemType


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the module-level registry between tests."""
    yield
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


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)


def _make_watchlist_item(**overrides):
    """Helper to create a WatchlistItem with sensible defaults."""
    defaults = {
        "id": 1,
        "title": "Test Movie",
        "year": 2024,
        "item_type": WatchlistItemType.MOVIE,
        "status": WatchlistItemStatus.WANTED,
        "priority": 2,
        "notes": "",
    }
    defaults.update(overrides)
    return WatchlistItem(**defaults)


# ── Analytics ────────────────────────────────────────────────────────


class TestAnalyticsSummary:
    def test_get_summary(self, client):
        mock_dashboard = MagicMock()
        mock_dashboard.get_dashboard_summary.return_value = {
            "library": {"total_items": 100},
            "scans": {"total_scans": 5},
        }
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["library"]["total_items"] == 100
        mock_dashboard.get_dashboard_summary.assert_called_once()

    def test_get_summary_service_unavailable(self, client):
        """When stats_dashboard is None, returns 503."""
        registry._analytics_dashboard = None
        resp = client.get("/analytics/summary")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestAnalyticsLibrary:
    def test_library_stats_movies(self, client):
        mock_dashboard = MagicMock()
        mock_stats = MagicMock()
        mock_stats.to_dict.return_value = {
            "total_items": 50,
            "total_size_gb": 1200.5,
            "resolution_counts": {"4K": 20, "1080p": 30},
        }
        mock_dashboard.get_library_stats.return_value = mock_stats
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/library?mode=Movies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_items"] == 50
        mock_dashboard.get_library_stats.assert_called_once_with("Movies")

    def test_library_stats_tv(self, client):
        mock_dashboard = MagicMock()
        mock_stats = MagicMock()
        mock_stats.to_dict.return_value = {"total_items": 200}
        mock_dashboard.get_library_stats.return_value = mock_stats
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/library?mode=TV%20Shows")
        assert resp.status_code == 200
        mock_dashboard.get_library_stats.assert_called_once_with("TV Shows")

    def test_library_stats_response_matches_typescript_contract(self, client):
        """Verify library stats response keys match the LibraryStats TypeScript interface."""
        # LibraryStats expected keys from frontend/src/lib/api/types.ts
        LIBRARY_STATS_KEYS = {
            "total_items", "total_size_gb", "resolution_counts", "resolution_sizes",
            "hdr_count", "dovi_count", "sdr_count", "codec_counts",
            "quality_score", "upgrade_potential",
        }
        mock_dashboard = MagicMock()
        mock_stats = MagicMock()
        mock_stats.to_dict.return_value = {
            "total_items": 50,
            "total_size_gb": 1200.5,
            "resolution_counts": {"4K": 20, "1080p": 30},
            "resolution_sizes": {"4K": 800.0, "1080p": 400.5},
            "hdr_count": 10,
            "dovi_count": 5,
            "sdr_count": 35,
            "codec_counts": {"HEVC": 40, "AVC": 10},
            "quality_score": 7.5,
            "upgrade_potential": 15,
        }
        mock_dashboard.get_library_stats.return_value = mock_stats
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/library?mode=Movies")
        assert resp.status_code == 200
        data = resp.json()
        assert LIBRARY_STATS_KEYS.issubset(set(data.keys()))


class TestAnalyticsScans:
    def test_scan_history(self, client):
        mock_dashboard = MagicMock()
        mock_scan_stats = MagicMock()
        mock_scan_stats.to_dict.return_value = {
            "total_scans": 10,
            "avg_duration": 45.2,
        }
        mock_dashboard.get_scan_stats.return_value = mock_scan_stats
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/scans")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_scans"] == 10
        mock_dashboard.get_scan_stats.assert_called_once_with(30)  # default days


class TestAnalyticsTrends:
    # TrendData TypeScript interface expected keys
    TREND_KEYS = {"dates", "items_scanned", "missing_found", "upgrades_found", "avg_duration", "scan_count"}

    def _make_trend_payload(self, n=1):
        """Create a trend payload matching the TrendData TypeScript interface."""
        return {
            "dates": [f"2024-01-0{i+1}" for i in range(n)],
            "items_scanned": [10] * n,
            "missing_found": [3] * n,
            "upgrades_found": [2] * n,
            "avg_duration": [45.0] * n,
            "scan_count": [1] * n,
        }

    def test_trends_with_days(self, client):
        mock_dashboard = MagicMock()
        mock_dashboard.get_trend_data.return_value = self._make_trend_payload(1)
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/trends?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "dates" in data
        mock_dashboard.get_trend_data.assert_called_once_with(7)

    def test_trends_default_days(self, client):
        mock_dashboard = MagicMock()
        mock_dashboard.get_trend_data.return_value = self._make_trend_payload(0)
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/trends")
        assert resp.status_code == 200
        mock_dashboard.get_trend_data.assert_called_once_with(30)

    def test_trends_response_matches_typescript_contract(self, client):
        """Verify trend response keys match the TrendData TypeScript interface."""
        mock_dashboard = MagicMock()
        mock_dashboard.get_trend_data.return_value = self._make_trend_payload(3)
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/trends?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == self.TREND_KEYS
        # Verify value types
        assert isinstance(data["dates"], list)
        assert all(isinstance(d, str) for d in data["dates"])
        assert all(isinstance(v, (int, float)) for v in data["items_scanned"])


class TestAnalyticsQuality:
    def test_quality_analysis(self, client):
        mock_dashboard = MagicMock()
        mock_dashboard.get_quality_breakdown.return_value = {
            "resolution": {"4K": 20, "1080p": 80},
            "hdr": {"HDR": 15, "SDR": 85},
        }
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/quality")
        assert resp.status_code == 200
        data = resp.json()
        assert "resolution" in data
        mock_dashboard.get_quality_breakdown.assert_called_once_with("Movies")


class TestAnalyticsExport:
    def test_export_json(self, client):
        mock_dashboard = MagicMock()
        mock_dashboard.get_dashboard_summary.return_value = {
            "library": {"total_items": 42},
        }
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/export")
        assert resp.status_code == 200
        data = resp.json()
        assert data["library"]["total_items"] == 42

    def test_export_html(self, client):
        mock_dashboard = MagicMock()
        mock_dashboard.export_report.return_value = "<html><body>Report</body></html>"
        registry._analytics_dashboard = mock_dashboard

        resp = client.get("/analytics/export?format=html")
        assert resp.status_code == 200
        assert "Report" in resp.text


# ── Watchlist ────────────────────────────────────────────────────────


class TestWatchlistList:
    def test_get_all_items(self, client):
        mock_mgr = MagicMock()
        item = _make_watchlist_item()
        mock_mgr.get_all.return_value = [item]
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Test Movie"
        mock_mgr.get_all.assert_called_once_with(status=None, item_type=None)

    def test_filter_by_status(self, client):
        mock_mgr = MagicMock()
        mock_mgr.get_all.return_value = []
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist?status=wanted")
        assert resp.status_code == 200
        mock_mgr.get_all.assert_called_once_with(
            status=WatchlistItemStatus.WANTED, item_type=None
        )

    def test_filter_by_type(self, client):
        mock_mgr = MagicMock()
        mock_mgr.get_all.return_value = []
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist?item_type=movie")
        assert resp.status_code == 200
        mock_mgr.get_all.assert_called_once_with(
            status=None, item_type=WatchlistItemType.MOVIE
        )


class TestWatchlistAdd:
    def test_add_valid_item(self, client):
        mock_mgr = MagicMock()
        mock_mgr.add.return_value = 42
        registry._watchlist_manager = mock_mgr

        resp = client.post(
            "/watchlist",
            json={"title": "New Movie", "year": 2025, "item_type": "movie"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 42
        assert data["status"] == "added"
        mock_mgr.add.assert_called_once()

    def test_add_missing_title_returns_422(self, client):
        mock_mgr = MagicMock()
        registry._watchlist_manager = mock_mgr

        resp = client.post("/watchlist", json={"year": 2025})
        assert resp.status_code == 422


class TestWatchlistGetItem:
    def test_get_specific_item(self, client):
        mock_mgr = MagicMock()
        item = _make_watchlist_item(id=7, title="Specific Movie")
        mock_mgr.get.return_value = item
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist/7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 7
        assert data["title"] == "Specific Movie"

    def test_get_item_not_found(self, client):
        mock_mgr = MagicMock()
        mock_mgr.get.return_value = None
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist/999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestWatchlistUpdate:
    def test_update_item(self, client):
        mock_mgr = MagicMock()
        item = _make_watchlist_item(id=3, title="Old Title")
        mock_mgr.get.return_value = item
        registry._watchlist_manager = mock_mgr

        resp = client.put("/watchlist/3", json={"title": "New Title"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"
        mock_mgr.update.assert_called_once()


class TestWatchlistDelete:
    def test_remove_item(self, client):
        mock_mgr = MagicMock()
        item = _make_watchlist_item(id=5)
        mock_mgr.get.return_value = item
        registry._watchlist_manager = mock_mgr

        resp = client.delete("/watchlist/5")
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"
        mock_mgr.remove.assert_called_once_with(5)


class TestWatchlistStats:
    def test_get_stats(self, client):
        mock_mgr = MagicMock()
        mock_mgr.get_stats.return_value = {
            "total": 10,
            "wanted": 5,
            "found": 3,
            "downloaded": 2,
        }
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 10


class TestWatchlistSearch:
    def test_search(self, client):
        mock_mgr = MagicMock()
        item = _make_watchlist_item(title="Blade Runner")
        mock_mgr.search.return_value = [item]
        registry._watchlist_manager = mock_mgr

        resp = client.get("/watchlist/search?q=blade")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Blade Runner"
        mock_mgr.search.assert_called_once_with("blade")


class TestWatchlistImport:
    def test_import_json(self, client):
        mock_mgr = MagicMock()
        mock_mgr.import_from_json.return_value = 3
        registry._watchlist_manager = mock_mgr

        payload = json.dumps([{"title": "Movie A"}, {"title": "Movie B"}, {"title": "Movie C"}])
        resp = client.post(
            "/watchlist/import/json",
            content=payload,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 3

    def test_import_imdb_csv(self, client):
        mock_mgr = MagicMock()
        mock_mgr.import_from_imdb_list.return_value = 2
        registry._watchlist_manager = mock_mgr

        csv_data = "Const,Your Rating,Date Rated,Title,Year\ntt1234567,,2024-01-01,Test Movie,2024"
        resp = client.post(
            "/watchlist/import/imdb",
            content=csv_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 2

    def test_import_letterboxd_csv(self, client):
        mock_mgr = MagicMock()
        mock_mgr.import_from_letterboxd.return_value = 1
        registry._watchlist_manager = mock_mgr

        csv_data = "Date,Name,Year,Letterboxd URI\n2024-01-01,Test Film,2024,https://example.com"
        resp = client.post(
            "/watchlist/import/letterboxd",
            content=csv_data,
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 1
