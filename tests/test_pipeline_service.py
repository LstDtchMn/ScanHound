from datetime import datetime, timezone, timedelta
from backend.pipeline_service import categorize, find_plex_match


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

    def test_categorize_forwards_resolution_to_find_plex_match_stub(self, monkeypatch):
        # NOTE: despite the old name ("...2160p_matches_4k"), find_plex_match is
        # stubbed here to match unconditionally — this does NOT exercise
        # _normalize_res or any real resolution-matching logic. It only proves
        # categorize() wires the rename row's resolution through to
        # find_plex_match's positional args and surfaces the returned
        # rating_key as "verified". Real _normalize_res / find_plex_match
        # matching behavior (including 2160p==4K equivalence) is covered
        # against a real DB in TestFindPlexMatch below.
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


def _insert_plex_row(conn, *, key, rating_key, imdb_id=None, title=None, year=None,
                     res=None, season=None, is_tv=0):
    """Insert a row directly into the real plex_cache table (schema per
    backend/database.py's CREATE TABLE IF NOT EXISTS plex_cache)."""
    conn.execute(
        """INSERT INTO plex_cache (
               key, title, original_title, year, res, size, imdb_id,
               rating_key, media_id, is_tv, season, episode_count,
               content_type, dovi, hdr, last_updated, library_name
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, title, title, year, res, 10.0, imdb_id, rating_key, None,
         1 if is_tv else 0, season, None, "TV Shows" if is_tv else "Movies",
         0, 0, "2026-07-10T00:00:00", "Test Library"),
    )
    conn.commit()


class TestFindPlexMatch:
    """find_plex_match() exercised against a REAL sqlite plex_cache table
    (via the db_manager fixture's temp-file DatabaseManager), not mocked —
    this is what actually covers the multi-row-per-imdb_id fix and the '?'
    resolution-sentinel fix from commit dea3df9."""

    def test_movie_two_resolutions_returns_the_requested_one(self, db_manager):
        # Same imdb_id, two rows: a 1080p copy and a 4K copy — the exact
        # shape (library holding both versions of one film) that a plain
        # fetchone() could grab the wrong one of.
        conn = db_manager.get_connection()
        _insert_plex_row(conn, key="rk_1080", rating_key="rk_1080", imdb_id="tt999",
                         title="foo", year=2024, res="1080p")
        _insert_plex_row(conn, key="rk_4k", rating_key="rk_4k", imdb_id="tt999",
                         title="foo", year=2024, res="4K")

        match_4k = find_plex_match(db_manager, "tt999", "Foo", 2024, None, "2160p")
        assert match_4k is not None
        assert match_4k["rating_key"] == "rk_4k"

        match_1080 = find_plex_match(db_manager, "tt999", "Foo", 2024, None, "1080p")
        assert match_1080 is not None
        assert match_1080["rating_key"] == "rk_1080"

    def test_tv_show_multiple_seasons_returns_the_requested_season(self, db_manager):
        conn = db_manager.get_connection()
        _insert_plex_row(conn, key="s1", rating_key="rk_s1", imdb_id="tt777",
                         title="bar", year=2020, res="1080p", season=1, is_tv=1)
        _insert_plex_row(conn, key="s2", rating_key="rk_s2", imdb_id="tt777",
                         title="bar", year=2020, res="1080p", season=2, is_tv=1)

        match = find_plex_match(db_manager, "tt777", "Bar", 2020, 2, None)
        assert match is not None
        assert match["rating_key"] == "rk_s2"

        match_s1 = find_plex_match(db_manager, "tt777", "Bar", 2020, 1, None)
        assert match_s1 is not None
        assert match_s1["rating_key"] == "rk_s1"

    def test_unknown_resolution_sentinel_still_matches_any_requested_res(self, db_manager):
        # Plex's res="?" (unknown) must not be treated as a literal value that
        # fails to equal "1080p" — _normalize_res("?") -> None skips the gate.
        conn = db_manager.get_connection()
        _insert_plex_row(conn, key="unk", rating_key="rk_unk", imdb_id="tt555",
                         title="baz", year=2022, res="?")

        match = find_plex_match(db_manager, "tt555", "Baz", 2022, None, "1080p")
        assert match is not None
        assert match["rating_key"] == "rk_unk"

    def test_no_match_wrong_imdb_id_returns_none(self, db_manager):
        conn = db_manager.get_connection()
        _insert_plex_row(conn, key="k1", rating_key="rk1", imdb_id="tt111",
                         title="qux", year=2019, res="1080p")
        # imdb_id doesn't match ANY row, and title/year given also don't match
        # the stored row (title="qux"/year=2019) — so neither the imdb_id path
        # NOR the title+year fallback path can succeed. (A wrong imdb_id paired
        # with a title/year that DOES match a stored row legitimately falls
        # back to a title match by design — see the docstring: "imdb_id first,
        # else normalized title+year" — that's not exercised by this test.)
        assert find_plex_match(db_manager, "tt_does_not_exist", "Nonexistent Title", 1901, None, "1080p") is None

    def test_no_match_title_fallback_wrong_year_returns_none(self, db_manager):
        conn = db_manager.get_connection()
        _insert_plex_row(conn, key="k1", rating_key="rk1", imdb_id="tt111",
                         title="qux", year=2019, res="1080p")
        # No imdb_id given -> falls back to title match, but year mismatches.
        assert find_plex_match(db_manager, None, "Qux", 1999, None, "1080p") is None

    def test_none_connection_from_get_connection_returns_none(self):
        class NoneConnDB:
            def get_connection(self):
                return None
        assert find_plex_match(NoneConnDB(), "tt1", "Title", 2020, None, "1080p") is None

    def test_db_is_none_does_not_raise(self):
        assert find_plex_match(None, "tt1", "Title", 2020, None, "1080p") is None

    def test_malformed_db_handle_does_not_raise(self):
        class BadDB:
            def get_connection(self):
                return "not-a-real-connection"  # .cursor() will raise AttributeError
        assert find_plex_match(BadDB(), "tt1", "Title", 2020, None, "1080p") is None
