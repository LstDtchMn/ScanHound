"""Tests for POST /scan/rescan-item."""
import json

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


def test_rescan_item_preserves_existing_plex_match(client):
    """Rescanning an item never consults Plex (see the route's docstring), so
    it must not clobber a cached IN_LIBRARY/UPGRADE match with the
    download-history-only status _create_media_item() derives on its own.
    Regression test for the "rescan wipes Plex library match" bug."""
    url = "https://hdencode.org/journey-example-3/"
    dm = DatabaseManager()
    cached_data = {
        "title": "Old Title", "year": 1969, "status": "in_library",
        "plex_info": "4K DV", "plex_rating_key": "12345",
        "plex_versions": json.dumps([{"resolution": "2160p", "hdr": "Dolby Vision"}]),
    }
    dm.upsert_background_cache([{
        "url": url, "title": "Old Title", "year": 1969, "status": "in_library",
        "source_category": "hdencode", "data": json.dumps(cached_data),
    }])
    dm.close()

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
    item = body["item"]
    assert item["status"] in ("in_library", "upgrade", "dv_upgrade")
    assert item["plex_info"] == "4K DV"
    assert item["plex_rating_key"] == "12345"
    versions = item["plex_versions"]
    if isinstance(versions, str):
        versions = json.loads(versions)
    assert versions

    dm = DatabaseManager()
    row = dm.get_background_cache_by_url(url)
    dm.close()
    assert row is not None
    assert row["status"] in ("in_library", "upgrade", "dv_upgrade")
    persisted = json.loads(row["data"])
    assert persisted.get("plex_info") == "4K DV"
    persisted_versions = persisted.get("plex_versions")
    if isinstance(persisted_versions, str):
        persisted_versions = json.loads(persisted_versions)
    assert persisted_versions


def test_rescan_item_scrape_failure_returns_502(client):
    url = "https://hdencode.org/journey-example-2/"
    _seed_cache_row(url)
    import backend.api.dependencies as deps
    with patch.object(deps.registry.scanner.scrapers, "scrape_details", return_value=None):
        resp = client.post("/scan/rescan-item", json={"url": url})
    assert resp.status_code == 502
