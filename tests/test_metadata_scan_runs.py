"""Persistent run/resume contracts for read-only metadata scanning."""

import json
import threading
import time

from backend.database import DatabaseManager
from backend.plex_metadata_scan import PlexMetadataScanJob
from backend.rename import mediainfo
from backend.rename import dv_detect


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
    assert json.loads(inventory["probe_json"])["video_codec"] == "HEVC"


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
    inventory = db.search_media_inventory(scan_state="failed")["items"]
    assert [(row["path"], row["hdr10plus_state"]) for row in inventory] == [
        (str(media), "unknown")
    ]


def test_startup_marks_abandoned_run_and_item_interrupted(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=1)
    db.create_metadata_scan_items(run["run_uuid"], [{"path": "/generated/movie.mkv"}])
    db.update_metadata_scan_run(run["run_uuid"], status="running")
    db.update_metadata_scan_item(run["run_uuid"], "/generated/movie.mkv", status="running")

    assert db.interrupt_abandoned_metadata_scans() == 1

    assert db.get_metadata_scan_run(run["run_uuid"])["status"] == "interrupted"
    assert db.list_metadata_scan_items(run["run_uuid"])[0]["status"] == "interrupted"


def test_retry_preparation_resets_only_retryable_items(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=2)
    db.create_metadata_scan_items(run["run_uuid"], [
        {"path": "/generated/current.mkv"},
        {"path": "/generated/failed.mkv"},
    ])
    db.update_metadata_scan_item(run["run_uuid"], "/generated/current.mkv", status="current")
    db.update_metadata_scan_item(
        run["run_uuid"], "/generated/failed.mkv", status="failed",
        failure_stage="ffprobe", error_code="probe_unavailable",
    )
    db.update_metadata_scan_run(run["run_uuid"], status="completed")

    assert db.prepare_metadata_scan_resume(run["run_uuid"], retry_failed=True) == 1

    items = {item["path"]: item for item in db.list_metadata_scan_items(run["run_uuid"])}
    assert items["/generated/current.mkv"]["status"] == "current"
    assert items["/generated/failed.mkv"]["status"] == "pending"
    assert items["/generated/failed.mkv"]["error_code"] is None
    assert db.get_metadata_scan_run(run["run_uuid"])["status"] == "queued"


def test_job_resume_starts_existing_manifest_without_rescanning_current(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=2)
    db.create_metadata_scan_items(run["run_uuid"], [
        {"path": "/generated/current.mkv"},
        {"path": "/generated/interrupted.mkv"},
    ])
    db.update_metadata_scan_item(run["run_uuid"], "/generated/current.mkv", status="current")
    db.update_metadata_scan_item(run["run_uuid"], "/generated/interrupted.mkv", status="interrupted")
    db.update_metadata_scan_run(run["run_uuid"], status="interrupted")
    starts = []
    monkeypatch.setattr("backend.plex_metadata_scan.threading.Thread.start", lambda thread: starts.append(thread))

    resumed = PlexMetadataScanJob(db).resume(run["run_uuid"])

    assert resumed["status"] == "running"
    assert len(starts) == 1
    assert db.list_metadata_scan_items(run["run_uuid"], status="pending")[0]["path"].endswith(
        "interrupted.mkv"
    )
    assert db.list_metadata_scan_items(run["run_uuid"], status="current")[0]["path"].endswith(
        "current.mkv"
    )


def test_pause_and_cancel_are_distinct_durable_stop_requests(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    paused = db.create_metadata_scan_run(scope="pilot", expected_count=0)
    job = PlexMetadataScanJob(db)
    job._active_run_uuid = paused["run_uuid"]
    job.status = "running"
    db.update_metadata_scan_run(paused["run_uuid"], status="running")

    assert job.pause(paused["run_uuid"])["status"] == "pausing"
    assert job._stop_mode == "paused"

    job._stop_flag = False
    assert job.cancel(paused["run_uuid"])["status"] == "cancelling"
    assert job._stop_mode == "cancelled"


def test_cancel_interrupts_dovi_probe_and_keeps_item_retryable(tmp_path, monkeypatch):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"generated test media")
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    entered = threading.Event()

    monkeypatch.setattr(mediainfo, "probe_detailed", lambda path, **_kwargs: {
        "present": True,
        "path": path,
        "resolution": "2160p",
        "hdr": "Dolby Vision",
        "hdr10plus_state": "unknown",
        "dv_layer": "unknown",
        "video_codec": "HEVC",
    })

    def cancellable_detect(_path, *, cancel_requested=None):
        entered.set()
        assert callable(cancel_requested)
        deadline = time.time() + 2
        while time.time() < deadline:
            if cancel_requested():
                return {"layer": "unknown", "tool": True, "error": "cancelled"}
            time.sleep(0.01)
        raise AssertionError("Dolby Vision probe did not observe cancellation")

    monkeypatch.setattr(dv_detect, "detect_layer", cancellable_detect)
    job = PlexMetadataScanJob(db)
    run = job.start_run("pilot", [{"path": str(media), "title": "Example"}])
    assert entered.wait(1)

    job.cancel(run["run_uuid"])
    terminal = _wait_for_terminal(db, run["run_uuid"])

    assert terminal["status"] == "cancelled"
    item = db.list_metadata_scan_items(run["run_uuid"])[0]
    assert item["status"] == "pending"
    assert job.status_dict()["processed"] == 0
    assert job.status_dict()["failed"] == 0


def test_cancelled_hdr10plus_probe_keeps_item_retryable(tmp_path, monkeypatch):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"generated test media")
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    entered = threading.Event()

    def cancellable_probe(path, *, cancel_requested=None, **_kwargs):
        entered.set()
        assert callable(cancel_requested)
        deadline = time.time() + 2
        while time.time() < deadline:
            if cancel_requested():
                return {
                    "present": True,
                    "path": path,
                    "resolution": "2160p",
                    "hdr": "HDR10",
                    "hdr10plus_state": "unknown",
                    "hdr10plus_evidence": {
                        "state": "unknown", "method": "full_extract",
                        "tool_version": "test", "error": "cancelled",
                    },
                }
            time.sleep(0.01)
        raise AssertionError("HDR10+ probe did not observe cancellation")

    monkeypatch.setattr(mediainfo, "probe_detailed", cancellable_probe)
    job = PlexMetadataScanJob(db)
    run = job.start_run("pilot", [{"path": str(media), "title": "Example"}])
    assert entered.wait(1)

    job.cancel(run["run_uuid"])
    terminal = _wait_for_terminal(db, run["run_uuid"])

    assert terminal["status"] == "cancelled"
    item = db.list_metadata_scan_items(run["run_uuid"])[0]
    assert item["status"] == "pending"
    assert job.status_dict()["processed"] == 0
    assert job.status_dict()["failed"] == 0
