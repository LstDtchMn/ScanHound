"""End-to-end DV acceptance gate.

host dv_host.db row -> POST /rename/dv-import -> dv_scan(source='scan')
-> dv_labeler.sync_labels (the same call the /rename/dv-sync-labels route
makes from its background thread, invoked directly here for determinism)
-> exactly one 'DV FEL' add_label on the target movie, and no non-managed
label touched.

Units can all pass while the feature labels nothing; this test is the gate.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager
from backend.rename import dv_labeler

# The host-native path the detector would have recorded, and the exact string
# Plex serves as part.file. normalize_path() must equate the two (drive->UNC /
# case / separator). Here we use the SAME path so the test exercises the wiring,
# not the mapping table (mapping variants are covered by the normalize unit tests).
MOVIE_PATH = r"Y:\Movies\Dune (2021)\Dune (2021) 2160p.mkv"


@pytest.fixture(autouse=True)
def _reset_dv():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_dv_scans(); dm.close()
        except Exception:
            pass
    _clear(); yield; _clear()


def _seed_host_db(path: str, host_db: str = None) -> str:
    """Create a standalone dv_host.db with one FEL row, return its path.

    host_db: destination path for the db (defaults to a system temp file).
    Callers that must satisfy the dv-import handoff-dir confinement pass a
    path inside the configured handoff dir.
    """
    import tempfile, os
    if host_db is None:
        fd, host_db = tempfile.mkstemp(prefix="dv_host_", suffix=".db"); os.close(fd)
    con = sqlite3.connect(host_db)
    con.execute(
        "CREATE TABLE dv_host (path TEXT PRIMARY KEY, dv_layer TEXT, "
        "sig_mtime REAL, sig_size INTEGER, title TEXT, scanned_at TEXT)"
    )
    con.execute(
        "INSERT INTO dv_host VALUES (?,?,?,?,?,?)",
        (path, "fel", 1000.0, 42, "Dune", "2026-06-30T00:00:00"),
    )
    con.commit(); con.close()
    return host_db


def _make_plex_movie(path: str):
    """A Plex movie MagicMock whose single part.file == path, tracking labels."""
    part = MagicMock(); part.file = path; part.size = 42
    media = MagicMock(); media.parts = [part]; media.videoResolution = "2160"
    movie = MagicMock()
    movie.title = "Dune"; movie.year = 2021; movie.ratingKey = 555
    movie.media = [media]; movie.guids = []
    # Non-managed label already present -- must survive the sync untouched.
    fav = MagicMock(); fav.tag = "Favorites"
    movie.labels = [fav]
    return movie


def test_end_to_end_fel_labels_exactly_once(monkeypatch, tmp_path):
    # 1. App with DV enabled + a root; DB is the container's sole crawler.db.
    app = create_app(config_override={
        "plex_url": "http://x", "plex_token": "t",
        "movie_libs": ["Movies"],
        "dv_detection": True,
        "dv_library_roots": r"Y:\Movies",
        "dv_label_vocab": '{"fel":"DV FEL","mel":"DV MEL","profile8":"DV P8","profile5":"DV P5"}',
    })

    # 2. Seed the host store (the import endpoint takes the path directly in
    #    its request body -- see DvImportRequest.host_db_path). dv-import
    #    confines host_db_path to the configured handoff dir (dirname of
    #    SCANHOUND_DV_HOST_DB, /data in prod); seed inside tmp_path and point
    #    that root there so the real confinement passes.
    from backend.api.routes import rename as rename_routes
    host_db = _seed_host_db(MOVIE_PATH, str(tmp_path / "dv_host.db"))
    monkeypatch.setattr(
        rename_routes, "_DEFAULT_DV_HOST_DB", str(tmp_path / "dv_host.db")
    )

    # 3. Monkeypatch the Plex client at the PlexManager._server boundary:
    #    - get_library_section() -> _server.library.section(name) -> our fake
    #      library, whose .all() serves the one movie (sync's discovery path).
    #    - add_label/remove_label -> _server.fetchItem(rating_key) -> the SAME
    #      movie object, so label mutations land on movie.addLabel/removeLabel
    #      (the real write path -- PlexManager never calls movie.addLabel via
    #      the lib.all() object directly).
    movie = _make_plex_movie(MOVIE_PATH)
    fake_lib = MagicMock(); fake_lib.all.return_value = [movie]
    fake_server = MagicMock()
    fake_server.library.section.return_value = fake_lib
    fake_server.fetchItem.return_value = movie

    with TestClient(app) as client:
        pm = registry._plex_service.plex_manager
        # is_connected is a read-only property (`self._server is not None`) --
        # setting _server to a MagicMock makes it True without patching it.
        monkeypatch.setattr(pm, "_server", fake_server, raising=False)

        # 4. Import host rows into dv_scan(source='scan') via the real endpoint.
        r_imp = client.post("/rename/dv-import", json={"host_db_path": host_db})
        assert r_imp.status_code == 200, r_imp.text
        assert r_imp.json()["imported"] == 1

        # 5. Run the real sync logic synchronously (not dry_run). This is the
        #    exact call the /rename/dv-sync-labels route makes from its
        #    background thread; invoked directly here so the assertion below
        #    is deterministic instead of racing a daemon thread.
        result = dv_labeler.sync_labels(
            registry.db, pm, registry.config, dry_run=False)

    # 6. Assertions -- the merge gate.
    movie.addLabel.assert_called_once_with("DV FEL")    # exactly one add
    movie.removeLabel.assert_not_called()                # nothing to remove
    tags = {getattr(lab, "tag", lab) for lab in movie.labels}
    assert "Favorites" in tags                           # non-managed untouched
    assert result["added"] == 1
    assert result["removed"] == 0
    assert result["matched"] == 1
