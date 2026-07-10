from datetime import datetime, timezone, timedelta
from backend.pipeline_service import categorize


def _download_row(**kw):
    base = {"url": "http://x/1", "title": "Foo", "year": 2024, "season": None,
            "resolution": "2160p", "last_grabbed_at": "2026-07-10 10:00:00"}
    base.update(kw)
    return base


def _rename_row(**kw):
    base = {"status": "applied", "media_type": "movie", "imdb_id": "tt123",
            "title": "Foo", "year": 2024, "season": None, "resolution": "2160p",
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": None, "warning_message": None}
    base.update(kw)
    return base


class TestNeverStartedAndFolderMode:
    def test_no_results_row_api_mode_past_30min_is_never_started(self):
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")  # long past 30 min
        cat, detail, uuid, rk = categorize(d, None, [], {}, jd_method="api")
        assert cat == "never_started"

    def test_no_results_row_folder_mode_is_unknown_not_never_started(self):
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")
        cat, *_ = categorize(d, None, [], {}, jd_method="folder")
        assert cat == "unknown"

    def test_no_results_row_within_30min_writes_no_verdict(self):
        d = _download_row(last_grabbed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        cat, *_ = categorize(d, None, [], {}, jd_method="api")
        assert cat is None  # too soon to judge


class TestDownloadStates:
    def test_failed_state_is_download_failed_with_detail(self):
        r = {"state": "failed", "error": "Link offline", "package_uuid": "111"}
        cat, detail, uuid, rk = categorize(_download_row(), r, [], {}, jd_method="api")
        assert cat == "download_failed" and detail == "Link offline" and uuid == "111"

    def test_queued_downloading_extracting_are_in_progress(self):
        for state in ("queued", "downloading", "extracting", "downloaded"):
            r = {"state": state, "error": None, "package_uuid": "111"}
            cat, *_ = categorize(_download_row(), r, [], {}, jd_method="api")
            assert cat == "in_progress", state

    def test_extracted_with_no_rename_rows_is_pending_rename(self):
        r = {"state": "extracted", "error": None, "package_uuid": "111"}
        cat, *_ = categorize(_download_row(), r, [], {}, jd_method="api")
        assert cat == "pending_rename"


class TestRenameStates:
    def _extracted_result(self):
        return {"state": "extracted", "error": None, "package_uuid": "111"}

    def test_any_failed_or_needs_review_is_rename_failed(self):
        rows = [_rename_row(status="applied"), _rename_row(status="failed", error_message="boom")]
        cat, detail, *_ = categorize(_download_row(), self._extracted_result(), rows, {}, jd_method="api")
        assert cat == "rename_failed" and detail == "boom"

    def test_pending_matched_applying_map_to_pending_rename(self):
        for status in ("pending", "matched", "applying"):
            rows = [_rename_row(status=status)]
            cat, *_ = categorize(_download_row(), self._extracted_result(), rows, {}, jd_method="api")
            assert cat == "pending_rename", status

    def test_reverted_is_rename_failed(self):
        rows = [_rename_row(status="reverted")]
        cat, detail, *_ = categorize(_download_row(), self._extracted_result(), rows, {}, jd_method="api")
        assert cat == "rename_failed" and detail == "reverted"


class TestPlexGate:
    def _extracted_result(self):
        return {"state": "extracted", "error": None, "package_uuid": "111"}

    def test_cache_stale_relative_to_rename_stays_in_progress(self):
        # rename applied "now"; plex cache max timestamp is from BEFORE that,
        # even though wall-clock time since the rename is large.
        processed = datetime.now(timezone.utc).isoformat()
        rows = [_rename_row(status="applied", processed_at=processed)]
        stale_cache = {"Movies": (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()}
        cat, *_ = categorize(_download_row(), self._extracted_result(), rows, stale_cache, jd_method="api")
        assert cat == "in_progress"

    def test_cache_fresh_after_rename_plus_margin_runs_real_check(self, monkeypatch):
        processed = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = [_rename_row(status="applied", processed_at=processed, resolution="2160p")]
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp()}
        import backend.pipeline_service as ps
        monkeypatch.setattr(ps, "find_plex_match", lambda *a, **k: None)
        cat, *_ = categorize(_download_row(), self._extracted_result(), rows, fresh_cache, jd_method="api")
        assert cat == "not_in_plex"

    def test_resolution_normalization_2160p_matches_4k(self, monkeypatch):
        import backend.pipeline_service as ps
        rows = [_rename_row(status="applied", resolution="2160p")]
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp() + 10000}
        monkeypatch.setattr(ps, "find_plex_match",
                            lambda db, imdb_id, title, year, season, resolution: {"rating_key": "rk1"})
        cat, detail, uuid, rk = categorize(_download_row(), self._extracted_result(), rows,
                                           fresh_cache, jd_method="api")
        assert cat == "verified" and rk == "rk1"


class TestMalformed:
    def test_malformed_input_never_raises(self):
        cat, *_ = categorize({}, {"state": "bogus"}, [{"status": "bogus"}], {}, jd_method="api")
        assert cat == "unknown"
