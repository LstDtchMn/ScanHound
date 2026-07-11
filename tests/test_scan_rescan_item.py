"""Tests for POST /scan/rescan-item."""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _seed_cache_row(url):
    dm = DatabaseManager()
    dm.upsert_background_cache([{
        "url": url, "title": "Old Title", "year": 1969, "status": "missing",
        "source_category": "hdencode", "data": '{"title": "Old Title"}',
    }])
    dm.close()


def test_rescan_item_not_in_cache_returns_404(client):
    resp = client.post("/scan/rescan-item", json={"url": "https://hdencode.org/unknown/"})
    assert resp.status_code == 404


def test_rescan_item_success_updates_cache(client):
    url = "https://hdencode.org/journey-example/"
    _seed_cache_row(url)
    fake_details = {
        "display_title": "Doppelganger", "year": 1969, "rating": "-",
        "url": url, "imdb_id": "tt0064519", "size": "23.9 GB", "res": "1080p",
        "hdr": "SDR", "dovi": False, "is_tv": False, "season": None,
        "episode_number": None, "episodes": None, "posted_date": None,
    }
    import backend.api.dependencies as deps
    with patch.object(
        deps.registry.scanner.scrapers, "scrape_details", return_value=fake_details,
    ):
        resp = client.post("/scan/rescan-item", json={"url": url})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["item"]["imdb_id"] == "tt0064519"
    # Confirm it actually persisted.
    dm = DatabaseManager()
    row = dm.get_background_cache_by_url(url)
    dm.close()
    assert row is not None
    assert '"imdb_id": "tt0064519"' in row["data"]


def test_rescan_item_scrape_failure_returns_502(client):
    url = "https://hdencode.org/journey-example-2/"
    _seed_cache_row(url)
    import backend.api.dependencies as deps
    with patch.object(deps.registry.scanner.scrapers, "scrape_details", return_value=None):
        resp = client.post("/scan/rescan-item", json={"url": url})
    assert resp.status_code == 502
