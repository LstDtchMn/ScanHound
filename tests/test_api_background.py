"""Tests for the background-cache API: GET /results/cached and /background/*."""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager


def _clear():
    try:
        dm = DatabaseManager()
        dm.clear_background_cache()
        dm.clear_dismissed_items()
        dm.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear()
    yield
    _clear()


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _seed(items):
    dm = DatabaseManager()
    dm.upsert_background_cache(items)
    dm.close()


def _row(url, title, status, source="HDEncode", year=2024, extra=None):
    data = {"url": url, "title": title, "status": status}
    if extra:
        data.update(extra)
    return {"url": url, "title": title, "year": year, "status": status,
            "source_category": source, "data": json.dumps(data)}


class TestBackgroundCache:
    def test_cached_empty(self, client):
        body = client.get("/results/cached").json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["source"] == "cache"
        assert body["last_updated"] is None

    def test_cached_returns_seeded_items(self, client):
        _seed([
            _row("u1", "Alpha", "missing", source="HDEncode", extra={"group_key": "g1"}),
            _row("u2", "Beta", "upgrade", source="DDLBase", extra={"group_key": "g2"}),
        ])
        body = client.get("/results/cached").json()
        assert body["total"] == 2
        assert {i["title"] for i in body["items"]} == {"Alpha", "Beta"}
        assert body["last_updated"] is not None
        assert body["stats"]["missing"] == 1
        assert body["stats"]["upgrade"] == 1

    def test_cached_status_filter(self, client):
        _seed([_row("u1", "Alpha", "missing"), _row("u2", "Beta", "upgrade")])
        body = client.get("/results/cached?filter=missing").json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Alpha"

    def test_cached_hides_dismissed_by_default(self, client):
        _seed([_row("u1", "Alpha", "missing")])
        dm = DatabaseManager(); dm.add_dismissed_item("u1", "Alpha"); dm.close()
        assert client.get("/results/cached").json()["total"] == 0
        assert client.get("/results/cached?include_dismissed=true").json()["total"] == 1


class TestBackgroundStatus:
    def test_status_shape_and_defaults(self, client):
        body = client.get("/background/status").json()
        for key in ("enabled", "interval_hours", "pages", "sources", "retain_days",
                    "last_run_at", "next_run_at", "cached_count", "running"):
            assert key in body, key
        assert body["enabled"] is False  # off by default
        assert isinstance(body["sources"], list)
        assert body["cached_count"] == 0

    def test_status_reports_cached_count(self, client):
        _seed([_row("u1", "A", "missing"), _row("u2", "B", "upgrade")])
        assert client.get("/background/status").json()["cached_count"] == 2

    def test_scan_now_triggers(self, client):
        # Scanner exists (created at startup). Stub scan_once so the trigger
        # doesn't kick off a real (network) scrape; just confirm it's invoked.
        called = threading.Event()
        registry._background_scanner.scan_once = lambda: called.set()
        assert client.post("/background/scan-now").status_code == 200
        assert called.wait(timeout=2.0)
