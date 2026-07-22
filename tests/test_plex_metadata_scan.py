import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.plex_metadata_scan import PlexMetadataScanJob


def _fake_db():
    db = MagicMock()
    db.media_probe_is_current.return_value = True
    db.dv_scan_is_current.return_value = False
    db.upsert_dv_scan.return_value = True
    return db


def _wait_until_done(job, timeout=5.0):
    start = time.time()
    while job.status_dict()["status"] == "running":
        if time.time() - start > timeout:
            raise AssertionError("job never finished")
        time.sleep(0.01)


def test_idle_status_before_start():
    job = PlexMetadataScanJob(_fake_db())
    s = job.status_dict()
    assert s["status"] == "idle"
    assert s["processed"] == 0
    assert s["total"] == 0


def test_start_processes_every_target_and_reaches_done(tmp_path):
    db = _fake_db()
    files = []
    for i in range(3):
        f = tmp_path / f"movie{i}.mkv"
        f.write_bytes(b"x")
        files.append(str(f))
    targets = [{"path": p, "title": f"Movie {i}"} for i, p in enumerate(files)]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "HDR10"}):
        job = PlexMetadataScanJob(db)
        assert job.start(targets) is True
        _wait_until_done(job)

    s = job.status_dict()
    assert s["status"] == "done"
    assert s["processed"] == 3
    assert s["total"] == 3


def test_dolby_vision_file_triggers_dv_layer_detection(tmp_path):
    db = _fake_db()
    f = tmp_path / "dv_movie.mkv"
    f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "DV Movie"}]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "Dolby Vision"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer",
               return_value={"layer": "fel", "tool": True, "error": None}) as mock_detect:
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    mock_detect.assert_called_once_with(str(f))
    db.upsert_dv_scan.assert_called_once()
    assert db.upsert_dv_scan.call_args.kwargs["dv_layer"] == "fel"
    assert db.upsert_dv_scan.call_args.kwargs["source"] == "scan"


def test_non_dolby_vision_file_skips_dv_layer_detection(tmp_path):
    db = _fake_db()
    f = tmp_path / "hdr10_movie.mkv"
    f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "HDR10 Movie"}]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "HDR10"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer") as mock_detect:
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    mock_detect.assert_not_called()
    db.upsert_dv_scan.assert_not_called()


def test_dv_layer_detection_skipped_when_cache_already_current(tmp_path):
    db = _fake_db()
    db.dv_scan_is_current.return_value = True
    f = tmp_path / "dv_movie.mkv"
    f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "DV Movie"}]

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "Dolby Vision"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer") as mock_detect:
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    mock_detect.assert_not_called()


def test_one_bad_file_does_not_abort_the_batch(tmp_path):
    db = _fake_db()
    good = tmp_path / "good.mkv"; good.write_bytes(b"x")
    bad = tmp_path / "bad.mkv"; bad.write_bytes(b"x")
    targets = [{"path": str(bad), "title": "Bad"}, {"path": str(good), "title": "Good"}]

    def _probe(path, db=None):
        if os.path.basename(path) == "bad.mkv":
            raise RuntimeError("ffprobe exploded")
        return {"present": True, "hdr": "HDR10"}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_probe):
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job)

    s = job.status_dict()
    assert s["status"] == "done"
    assert s["processed"] == 2
    assert s["succeeded"] == 1
    assert s["failed"] == 1


def test_unknown_dv_detection_is_failed_and_not_cached(tmp_path):
    """A transient dovi_tool failure must remain retryable, not look complete."""
    db = _fake_db()
    f = tmp_path / "dv_movie.mkv"
    f.write_bytes(b"x")

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "Dolby Vision"}), \
         patch("backend.plex_metadata_scan.dv_detect.detect_layer",
               return_value={"layer": "unknown", "tool": True, "error": "timeout"}):
        job = PlexMetadataScanJob(db)
        job.start([{"path": str(f), "title": "DV Movie"}])
        _wait_until_done(job)

    status = job.status_dict()
    assert status["failed"] == 1
    assert status["succeeded"] == 0
    db.upsert_dv_scan.assert_not_called()


def test_probe_result_is_failed_when_media_cache_did_not_persist(tmp_path):
    """The completeness counter must require durable media_probe evidence."""
    db = _fake_db()
    db.media_probe_is_current.return_value = False
    f = tmp_path / "hdr10_movie.mkv"
    f.write_bytes(b"x")

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": "HDR10"}):
        job = PlexMetadataScanJob(db)
        job.start([{"path": str(f), "title": "HDR10 Movie"}])
        _wait_until_done(job)

    status = job.status_dict()
    assert status["failed"] == 1
    assert status["succeeded"] == 0


def test_cancel_stops_the_job():
    db = _fake_db()
    targets = [{"path": f"/fake/movie{i}.mkv"} for i in range(50)]

    def _slow_probe(path, db=None):
        time.sleep(0.05)
        return {"present": True, "hdr": None}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_slow_probe):
        job = PlexMetadataScanJob(db)
        job.start(targets)
        time.sleep(0.05)
        job.cancel()
        _wait_until_done(job, timeout=10.0)

    s = job.status_dict()
    assert s["status"] == "cancelled"
    assert s["processed"] < 50


def test_start_returns_false_when_already_running(tmp_path):
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    targets = [{"path": str(f)}]

    def _slow_probe(path, db=None):
        time.sleep(0.2)
        return {"present": True, "hdr": None}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_slow_probe):
        job = PlexMetadataScanJob(_fake_db())
        assert job.start(targets) is True
        assert job.start(targets) is False
        _wait_until_done(job, timeout=5.0)


def test_progress_callback_invoked_on_state_changes(tmp_path):
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    targets = [{"path": str(f), "title": "M"}]
    seen_statuses = []

    def _cb(status_dict):
        seen_statuses.append(status_dict["status"])

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs",
               return_value={"present": True, "hdr": None}):
        job = PlexMetadataScanJob(_fake_db(), progress_cb=_cb)
        job.start(targets)
        _wait_until_done(job)

    assert "running" in seen_statuses
    assert "done" in seen_statuses


def test_eta_is_none_before_any_progress():
    job = PlexMetadataScanJob(_fake_db())
    s = job.status_dict()
    assert s["eta_seconds"] is None


def test_never_exceeds_bounded_concurrency(tmp_path):
    """The per-file pipeline (which includes the expensive dovi_tool step)
    must never run more than 2 files at once -- guards the 'never unbounded
    parallel dovi_tool' constraint."""
    db = _fake_db()
    targets = []
    for i in range(12):
        f = tmp_path / f"movie{i}.mkv"
        f.write_bytes(b"x")
        targets.append({"path": str(f), "title": f"Movie {i}"})

    lock = threading.Lock()
    state = {"active": 0, "max": 0}

    def _probe(path, db=None):
        with lock:
            state["active"] += 1
            if state["active"] > state["max"]:
                state["max"] = state["active"]
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        return {"present": True, "hdr": None}

    with patch("backend.plex_metadata_scan.mediainfo.probe_specs", side_effect=_probe):
        job = PlexMetadataScanJob(db)
        job.start(targets)
        _wait_until_done(job, timeout=15.0)

    assert state["max"] <= 2
    assert state["max"] >= 2  # with 12 files it should actually reach the bound
    assert job.status_dict()["processed"] == 12
