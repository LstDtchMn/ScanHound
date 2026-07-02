"""Tests for DV FEL/MEL settings config keys and SettingsUpdate round-trip."""
import json
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.api.routes.settings import SettingsUpdate


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
    with TestClient(app) as c:
        yield c


def test_settings_model_accepts_dv_keys_and_4k():
    m = SettingsUpdate(
        dv_library_roots="Y:\\Movies;E:\\4K",
        dv_detection=True,
        dv_file_tagging=False,
        dv_label_vocab=json.dumps({"fel": "DV FEL", "mel": "DV MEL"}),
        auto_rename_movie_library_4k="Movies 4K",
    )
    dumped = m.model_dump(exclude_unset=True)
    assert dumped["dv_library_roots"] == "Y:\\Movies;E:\\4K"
    assert dumped["dv_detection"] is True
    assert dumped["auto_rename_movie_library_4k"] == "Movies 4K"


def test_defaults_have_dv_keys():
    from backend.config import _DEFAULT_CONFIG
    assert _DEFAULT_CONFIG["dv_detection"] is False
    assert _DEFAULT_CONFIG["dv_file_tagging"] is False
    assert _DEFAULT_CONFIG["dv_library_roots"] == ""
    assert isinstance(_DEFAULT_CONFIG["dv_label_vocab"], str)


def test_put_settings_round_trips_dv_and_4k(client):
    from backend.api.dependencies import registry
    registry.config = {}

    class _Backend:
        _cleared_keys = set()
        def save_config(self):  # no-op; config isolated by conftest
            pass
    registry.backend = _Backend()

    payload = {
        "dv_library_roots": "Y:\\M",
        "dv_detection": True,
        "auto_rename_movie_library_4k": "Movies 4K",
    }
    r = client.put("/settings", json=payload)
    assert r.status_code == 200, r.text  # was 422 for the 4k key before the fix
    updated = set(r.json()["updated_keys"])
    assert {"dv_library_roots", "dv_detection",
            "auto_rename_movie_library_4k"} <= updated
    assert registry.config["auto_rename_movie_library_4k"] == "Movies 4K"
