# tests/test_conflict_analyzer.py
from unittest.mock import MagicMock, patch
from backend.rename import conflict_analyzer


def _job(**over):
    base = {"id": 1, "status": "matched", "media_type": "movie", "title": "X", "year": 2020,
            "imdb_id": "tt1", "original_path": "/incoming/X.mkv",
            "destination_path": "/library/movies/X (2020)",
            "new_filename": "X (2020).mkv", "conflict_analysis": None,
            "detected_at": "2026-07-11T00:00:00+00:00"}
    base.update(over)
    return base


def test_analyze_job_conflict_same_path_writes_analysis():
    db = MagicMock()
    db.get_rename_job.return_value = _job()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "2160p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/library/movies/X (2020)/X (2020).mkv"},
            {"present": True, "resolution": "1080p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 5, "path": "/incoming/X.mkv"},
        ]
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result["kind"] == "same_path"
    assert result["recommended"] == "existing"
    db.update_rename_job.assert_called_once()
    args, kwargs = db.update_rename_job.call_args
    assert args[0] == 1
    assert kwargs["conflict_analysis"]["kind"] == "same_path"


def test_analyze_job_conflict_library_duplicate_writes_analysis():
    db = MagicMock()
    plex_rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0,
                  "file_path": "/library/movies-other/X (2020)/X.mkv", "rating_key": "99"}]
    with patch("os.path.lexists", return_value=False), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "1080p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 5, "path": "/library/movies-other/X (2020)/X.mkv"},
            {"present": True, "resolution": "2160p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/incoming/X.mkv"},
        ]
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=plex_rows)
    assert result["kind"] == "library_duplicate"
    assert result["recommended"] == "incoming"


def test_analyze_job_conflict_no_duplicate_returns_none():
    db = MagicMock()
    with patch("os.path.lexists", return_value=False):
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result is None
    db.update_rename_job.assert_not_called()


def test_analyze_job_conflict_degraded_when_probe_fails():
    # probe_specs returns None only on a genuine ffprobe FAILURE (missing
    # binary/timeout/error) — never for a merely-absent file, which it
    # already reports as a full {"present": False, ...} dict itself. This
    # test exercises that genuine-failure path.
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs", return_value=None):
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result["degraded"] is True
    assert result["recommended"] is None
    # The fallback dict must carry every FileSpec field (as null), matching
    # probe_specs' OWN not-present shape exactly — not an abbreviated dict —
    # so the frontend's FileSpec type never sees a field silently missing.
    assert set(result["existing"].keys()) == {
        "present", "path", "size_bytes", "container", "duration_min",
        "bitrate", "resolution", "video_codec", "hdr", "dv_layer", "audio"}


def test_analyze_job_conflict_degraded_when_incoming_vanished():
    # Both sides probe successfully (probe_specs returns full dicts, NOT
    # None) but report present: False -- a TOCTOU race where the file(s)
    # vanished between detection and analysis. rank_conflict() would
    # otherwise happily recommend "incoming" here (it only special-cases
    # a None/absent `existing`), which would be a phantom "keep Incoming"
    # recommendation for a file that isn't actually there. The guard in
    # analyze_job_conflict must force degraded=True and suppress advice.
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock:
        probe_mock.side_effect = [
            {"present": False, "path": "/library/movies/X (2020)/X (2020).mkv",
             "size_bytes": None, "container": None, "duration_min": None,
             "bitrate": None, "resolution": None, "video_codec": None,
             "hdr": None, "dv_layer": None, "audio": None},
            {"present": False, "path": "/incoming/X.mkv",
             "size_bytes": None, "container": None, "duration_min": None,
             "bitrate": None, "resolution": None, "video_codec": None,
             "hdr": None, "dv_layer": None, "audio": None},
        ]
        result = conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert result["degraded"] is True
    assert result["recommended"] is None
    db.update_rename_job.assert_called_once()
    args, kwargs = db.update_rename_job.call_args
    assert kwargs["conflict_analysis"]["degraded"] is True
    assert kwargs["conflict_analysis"]["recommended"] is None


def test_analyze_job_conflict_fires_detect_layer_when_gate_says_yes():
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock, \
         patch("backend.rename.conflict_analyzer._dv.available", return_value=True), \
         patch("backend.rename.conflict_analyzer._dv.detect_layer") as detect_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/library/movies/X (2020)/X (2020).mkv"},
            {"present": True, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/incoming/X.mkv"},
        ]
        detect_mock.return_value = {"layer": "fel", "tool": True, "error": None}
        conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    assert detect_mock.call_count == 2  # both sides scanned


def test_analyze_job_conflict_skips_detect_layer_when_gate_says_no():
    db = MagicMock()
    with patch("os.path.lexists", return_value=True), \
         patch("backend.rename.conflict_analyzer.probe_specs") as probe_mock, \
         patch("backend.rename.conflict_analyzer._dv.detect_layer") as detect_mock:
        probe_mock.side_effect = [
            {"present": True, "resolution": "1080p", "hdr": None, "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/library/movies/X (2020)/X (2020).mkv"},
            {"present": True, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
             "audio": "AC3", "size_bytes": 10, "path": "/incoming/X.mkv"},
        ]
        conflict_analyzer.analyze_job_conflict(db, _job(), plex_cache_rows=[])
    detect_mock.assert_not_called()


def test_analyze_pending_conflicts_only_counts_jobs_with_an_active_duplicate_flag():
    # 3 jobs total, but only 1 has an actual duplicate (destination_conflict
    # via conflict_annotations) — the other 2 are plain matched jobs with no
    # conflict at all. The 50(here: 2)-per-pass limit must apply to the
    # FILTERED set, not the raw job list, or the budget is mostly wasted on
    # non-duplicates every pass.
    db = MagicMock()
    dup_job = _job(id=1, destination_path="/library/movies/Dup (2020)",
                   new_filename="Dup (2020).mkv")
    other_dup_job = _job(id=2, destination_path="/library/movies/Dup (2020)",
                         new_filename="Dup (2020).mkv")  # same dest as dup_job -> conflict pair
    plain_job = _job(id=3, imdb_id="tt999", destination_path="/library/movies/Plain (2020)",
                     new_filename="Plain (2020).mkv")
    db.list_rename_jobs.return_value = [dup_job, other_dup_job, plain_job]
    db.list_plex_cache_movies.return_value = []
    with patch("backend.rename.conflict_analyzer.analyze_job_conflict", return_value=None) as analyze_mock:
        n = conflict_analyzer.analyze_pending_conflicts(db, limit=50)
    analyzed_ids = {call.args[1]["id"] for call in analyze_mock.call_args_list}
    assert analyzed_ids == {1, 2}  # the conflicting pair only — plain_job excluded
    assert n == 2


def test_analyze_pending_conflicts_respects_limit_within_the_filtered_set():
    db = MagicMock()
    # 4 jobs all sharing one destination -> all 4 flagged destination_conflict.
    jobs = [_job(id=i, destination_path="/library/movies/Dup (2020)",
                new_filename="Dup (2020).mkv") for i in range(4)]
    db.list_rename_jobs.return_value = jobs
    db.list_plex_cache_movies.return_value = []
    with patch("backend.rename.conflict_analyzer.analyze_job_conflict", return_value=None) as analyze_mock:
        n = conflict_analyzer.analyze_pending_conflicts(db, limit=2)
    assert analyze_mock.call_count == 2
    assert n == 2
