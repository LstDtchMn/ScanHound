"""Tests for the Plex library metadata-scan API: /plex/scan-metadata,
/plex/scan-metadata/cancel, /plex/scan-metadata/status."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.api.routes.plex import _movie_targets_for_scope


@pytest.fixture(autouse=True)
def _reset_metadata_scan_job():
    """Reset the module-level registry's scan job between tests.

    The registry is a module-level singleton; each test's `client` fixture
    creates a fresh app (and therefore a fresh reg.db) via `create_app`,
    which eagerly (re)constructs `_plex_metadata_scan_job` in `_init_services`
    -- but a previously constructed instance would keep pointing at a stale
    db and stale in-progress state unless cleared here first.
    """
    yield
    registry._plex_metadata_scan_job = None


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def test_scan_metadata_all_starts_job(client, monkeypatch):
    from backend.api.routes import plex as plex_routes
    monkeypatch.setattr(
        plex_routes, "_movie_targets_for_scope",
        lambda reg, scope, ids: [{"path": "/x/movie.mkv", "title": "Movie"}])
    resp = client.post("/plex/scan-metadata", json={"scope": "all"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "starting"


def test_scan_metadata_selected_requires_ids(client):
    resp = client.post("/plex/scan-metadata", json={"scope": "selected"})
    assert resp.status_code == 400


def test_scan_metadata_status_reports_idle_before_any_scan(client):
    resp = client.get("/plex/scan-metadata/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("idle", "done", "cancelled")


def test_scan_metadata_cancel_is_safe_when_not_running(client):
    resp = client.post("/plex/scan-metadata/cancel")
    assert resp.status_code == 200


def test_movie_targets_translates_file_path():
    reg = MagicMock()
    reg.config = {"plex_library_path_mappings": "G:\\Movies 1 => /library/plex-source/g-movies-1"}
    reg.db.list_plex_cache_movies.return_value = [{
        "key": "1", "title": "X", "rating_key": "1", "imdb_id": "tt1",
        "file_path": "G:\\Movies 1\\X.mkv",
    }]
    targets = _movie_targets_for_scope(reg, "all", None)
    assert targets[0]["path"] == "/library/plex-source/g-movies-1/X.mkv"
