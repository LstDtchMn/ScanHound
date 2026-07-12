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


def test_settings_model_accepts_jd_4k_folder_and_path_mappings():
    # Regression: the 4K download folder + host=>container path mappings must be
    # settable via the API. path_mappings was editable in the UI but missing
    # from the extra="forbid" model, so saving it 422'd — breaking the 4K
    # instant-move workflow (which needs a G:\Downloads => /library/movies-4k map).
    m = SettingsUpdate(
        jd_movies_folder_4k="G:\\Downloads",
        auto_rename_path_mappings="F:\\Downloads => /library/movies\nG:\\Downloads => /library/movies-4k",
    )
    dumped = m.model_dump(exclude_unset=True)
    assert dumped["jd_movies_folder_4k"] == "G:\\Downloads"
    assert "movies-4k" in dumped["auto_rename_path_mappings"]


def test_settings_model_accepts_auto_rename_movie_flat():
    # Regression: auto_rename_movie_flat was added to the Settings UI and to
    # AppConfig/_DEFAULT_CONFIG but never added to SettingsUpdate (which uses
    # extra="forbid"). Toggling the flat-folders checkbox and saving would
    # 422 the ENTIRE settings PUT, silently dropping every other changed key
    # in that same save alongside the flat toggle.
    m = SettingsUpdate(auto_rename_movie_flat=True)
    dumped = m.model_dump(exclude_unset=True)
    assert dumped["auto_rename_movie_flat"] is True


def test_all_frontend_editable_settings_keys_are_in_model():
    # Guard against the whole class of bug: any settings key editable in the UI
    # but absent from SettingsUpdate silently 422s the user's entire save
    # (frontend sends a diff of changed keys). Keep the model a superset.
    import re
    from pathlib import Path
    page = Path("frontend/src/routes/settings/+page.svelte").read_text(encoding="utf-8")
    editable = set(re.findall(r"\.\.\.s,\s*([a-z_0-9]+):", page))
    model = set(SettingsUpdate.model_fields.keys())
    missing = editable - model
    assert not missing, f"UI-editable settings missing from SettingsUpdate (would 422): {sorted(missing)}"


def test_defaults_have_dv_keys():
    from backend.config import _DEFAULT_CONFIG
    assert _DEFAULT_CONFIG["dv_detection"] is False
    assert _DEFAULT_CONFIG["dv_file_tagging"] is False
    assert _DEFAULT_CONFIG["dv_library_roots"] == ""
    assert isinstance(_DEFAULT_CONFIG["dv_label_vocab"], str)


def test_plex_library_path_mappings_has_seeded_default():
    # Regression: plex_cache.file_path comes back in Plex's own terms (drive
    # letters, NTFS junction-folder aliases, or NAS UNC paths) which aren't
    # directly readable in the container. Seed the 23 mappings verified
    # working end-to-end this session so probing works out of the box.
    from backend.config import DEFAULT_CONFIG
    assert "plex_library_path_mappings" in DEFAULT_CONFIG
    seeded = DEFAULT_CONFIG["plex_library_path_mappings"]
    assert "C:\\1080p Drives\\1080p Bismark => /library/plex-source/l-1080p-bismark" in seeded
    assert "\\\\TURTLELANDSRV2\\4K Magellan => /library/plex-source/nas-4k-magellan" in seeded


def test_settings_model_accepts_plex_library_path_mappings():
    m = SettingsUpdate(plex_library_path_mappings="A: => /library/plex-source/a")
    dumped = m.model_dump(exclude_unset=True)
    assert dumped["plex_library_path_mappings"] == "A: => /library/plex-source/a"


def test_put_settings_accepts_plex_library_path_mappings(client):
    from backend.api.dependencies import registry
    registry.config = {}

    class _Backend:
        _cleared_keys = set()
        def save_config(self):  # no-op; config isolated by conftest
            pass
    registry.backend = _Backend()

    payload = {"plex_library_path_mappings": "A: => /library/plex-source/a"}
    r = client.put("/settings", json=payload)
    assert r.status_code == 200, r.text
    assert "plex_library_path_mappings" in set(r.json()["updated_keys"])
    assert registry.config["plex_library_path_mappings"] == "A: => /library/plex-source/a"


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
