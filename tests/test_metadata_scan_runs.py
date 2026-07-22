"""Persistent run/resume contracts for read-only metadata scanning."""

import time

from backend.database import DatabaseManager
from backend.plex_metadata_scan import PlexMetadataScanJob
from backend.rename import mediainfo


def _wait_for_terminal(db, run_uuid, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = db.get_metadata_scan_run(run_uuid)
        if run["status"] in {"completed", "cancelled", "failed", "interrupted"}:
            return run
        time.sleep(0.01)
    raise AssertionError("metadata scan did not reach a terminal state")


def test_durable_scan_records_current_item_and_inventory(tmp_path, monkeypatch):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"generated test media")
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    monkeypatch.setattr(mediainfo, "probe_detailed", lambda path, **_kwargs: {
        "present": True, "path": path, "resolution": "2160p", "hdr": "HDR10",
        "hdr10plus_state": "absent", "dv_layer": None, "video_codec": "HEVC",
    })

    run = PlexMetadataScanJob(db).start_run("pilot", [{
        "path": str(media), "title": "Example", "library_name": "Movies", "rating_key": "9",
    }])
    terminal = _wait_for_terminal(db, run["run_uuid"])

    assert terminal["status"] == "completed"
    item = db.list_metadata_scan_items(run["run_uuid"])[0]
    assert item["status"] == "current"
    inventory = db._query_dicts("SELECT * FROM media_inventory WHERE path = ?", (str(media),))[0]
    assert inventory["scan_state"] == "current"
    assert inventory["hdr10plus_state"] == "absent"


def test_durable_probe_failure_is_retained_for_retry(tmp_path, monkeypatch):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"generated test media")
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    monkeypatch.setattr(mediainfo, "probe_detailed", lambda *_args, **_kwargs: None)

    run = PlexMetadataScanJob(db).start_run("pilot", [{"path": str(media)}])
    terminal = _wait_for_terminal(db, run["run_uuid"])

    assert terminal["status"] == "completed"
    item = db.list_metadata_scan_items(run["run_uuid"])[0]
    assert item["status"] == "failed"
    assert item["failure_stage"] == "ffprobe"
