from datetime import datetime, timezone, timedelta
from backend.pipeline_service import categorize, find_plex_match, reconcile_batch
from backend.database import DatabaseManager


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


class TestResultRowDeletedFallthrough:
    """The download_results row is NOT permanent — the Downloads UI's per-item
    'remove' (removeDownloadResult) and 'clear all' (clearDownloadResults)
    actions delete rows, and the poller never repopulates a deleted one. A grab
    that fully processed (all rename_jobs 'applied', Plex ingested) but whose
    result row was later removed by routine housekeeping must NOT be reported
    'never_started' (which surfaces a misleading Re-grab button for a release
    already correctly sitting in Plex). categorize() should fall through to the
    same rename-status/Plex-verification logic it uses when the result row
    exists — GATED so stale rename rows from a superseded pre-regrab attempt
    can't fool it."""

    def test_result_row_deleted_but_processed_and_plex_fresh_is_verified(self, monkeypatch):
        # EXACT confirmed scenario: result_row=None (row removed via UI), all
        # rename_rows 'applied', Plex cache fresh past the grace margin, and NO
        # excluded_uuid (grab was never regrabbed). Must be 'verified', not
        # 'never_started'.
        import backend.pipeline_service as ps
        monkeypatch.setattr(ps, "find_plex_match",
                            lambda db, imdb_id, title, year, season, resolution: {"rating_key": "rk_live"})
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")  # long past 30 min
        rows = [_rename_row(status="applied", processed_at=datetime.now(timezone.utc).isoformat())]
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp() + 10000}
        cat, detail, uuid, rk = categorize(d, None, rows, fresh_cache, jd_method="api")
        assert cat == "verified"
        assert rk == "rk_live"

    def test_result_row_deleted_but_processed_and_no_plex_match_is_not_in_plex(self, monkeypatch):
        # Same fallthrough, but Plex has no matching item -> honest 'not_in_plex',
        # still NOT 'never_started'.
        import backend.pipeline_service as ps
        monkeypatch.setattr(ps, "find_plex_match", lambda *a, **k: None)
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")
        rows = [_rename_row(status="applied", processed_at=datetime.now(timezone.utc).isoformat())]
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp() + 10000}
        cat, *_ = categorize(d, None, rows, fresh_cache, jd_method="api")
        assert cat == "not_in_plex"

    def test_result_row_deleted_after_regrab_does_not_trust_stale_rename_rows(self, monkeypatch):
        # Regrab-safety GATE: this grab HAS been regrabbed (excluded_uuid set),
        # so the only rename_rows present are STALE ones left over from the
        # prior, now-superseded attempt (regrab clears the download_results uuid
        # pin + adds to excluded_uuid but does NOT delete old rename_jobs rows).
        # find_plex_match is stubbed to return a match, so IF the ungated
        # fallthrough were reached it would wrongly report 'verified' from stale
        # evidence for a NEW attempt that never reached that stage. The gate must
        # hold: no verified/not_in_plex, and the honest answer past 30 min is
        # 'never_started'.
        import backend.pipeline_service as ps
        monkeypatch.setattr(ps, "find_plex_match", lambda *a, **k: {"rating_key": "stale_rk"})
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00", excluded_uuid="old-superseded-uuid")
        stale = _rename_row(status="applied",
                            processed_at="2019-06-01T00:00:00+00:00")  # from before the regrab
        fresh_cache = {"Movies": datetime.now(timezone.utc).timestamp() + 10000}
        cat, *_ = categorize(d, None, [stale], fresh_cache, jd_method="api")
        assert cat not in ("verified", "not_in_plex")
        assert cat == "never_started"

    def test_result_row_none_and_no_rename_rows_still_never_started(self):
        # The genuine never_started case must remain reachable: no result row AND
        # no rename evidence at all, past the 30-min window.
        d = _download_row(last_grabbed_at="2020-01-01 00:00:00")
        cat, *_ = categorize(d, None, [], {}, jd_method="api")
        assert cat == "never_started"


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


class TestMatchingAndReconcileBatch:
    def test_uuid_recorded_verdict_matches_directly(self, db_manager):
        db_manager.add_to_history("http://m/1", "Foo", package_name="Foo [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/1'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('999', 'Foo [1080p]', 'failed', datetime('now'))")
        conn.commit()
        db_manager.upsert_pipeline_verdict("http://m/1", "download_failed", package_uuid="999")
        n = reconcile_batch(db_manager)
        assert n >= 1
        rows = db_manager.get_pipeline_verdicts()
        assert rows[0]["package_uuid"] == "999"

    def test_max_id_tiebreak_not_state_progression(self, db_manager):
        # Two rows, SAME name, SAME updated_at (simulating a post-restart
        # repoll bump) — the OLDER row (lower id) is further-along ('extracted'),
        # the NEWER row (higher id) is earlier-stage ('downloading'). The
        # higher id must win.
        db_manager.add_to_history("http://m/2", "Bar", package_name="Bar [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/2'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('old-uuid', 'Bar [1080p]', 'extracted', datetime('now'))")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('new-uuid', 'Bar [1080p]', 'downloading', datetime('now'))")
        conn.commit()
        reconcile_batch(db_manager)
        rows = db_manager.get_pipeline_verdicts()
        row = next(r for r in rows if r["url"] == "http://m/2")
        assert row["package_uuid"] == "new-uuid"

    def test_excluded_uuid_prevents_readopting_stale_package(self, db_manager):
        db_manager.add_to_history("http://m/3", "Baz", package_name="Baz [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/3'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('stale-uuid', 'Baz [1080p]', 'extracted', datetime('now'))")
        conn.commit()
        db_manager.upsert_pipeline_verdict("http://m/3", "rename_failed", package_uuid="stale-uuid")
        db_manager.clear_pipeline_verdict("http://m/3")  # excludes stale-uuid
        n = reconcile_batch(db_manager)  # only the stale row exists — must NOT re-adopt it
        rows = db_manager.get_pipeline_verdicts(include_dismissed=True)
        row = next(r for r in rows if r["url"] == "http://m/3")
        assert row["package_uuid"] is None  # no match found, not the excluded stale row

    def test_reconcile_batch_is_batched_not_n_plus_1(self, db_manager, monkeypatch):
        # Seed 5 eligible grabs; assert the number of raw connection queries
        # stays small (a handful, not 5x per-item queries). Approximate via a
        # call-count wrapper on the connection's execute.
        #
        # NOTE: CPython's native sqlite3.Connection.execute is a read-only C
        # attribute — monkeypatch.setattr(conn, "execute", ...) raises
        # AttributeError. So instead of patching execute on the real connection,
        # wrap the connection in a thin proxy that counts execute() and delegates
        # everything else (cursor/commit/etc.), and patch get_connection to hand
        # back the proxy. This counts exactly the same calls the direct patch
        # would have: the per-item _match_download_results/_match_rename_rows
        # queries plus each upsert's _mutate conn.execute (cursor.execute paths
        # in _query-based reads go through the delegated real cursor and aren't
        # counted, same as they wouldn't be under the original approach).
        for i in range(5):
            db_manager.add_to_history(f"http://m/batch{i}", f"T{i}", package_name=f"T{i} [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url LIKE 'http://m/batch%'")
        conn.commit()
        calls = {"n": 0}

        class _CountingConn:
            def __init__(self, real):
                self._real = real

            def execute(self, *a, **k):
                calls["n"] += 1
                return self._real.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        wrapped = _CountingConn(conn)
        monkeypatch.setattr(db_manager, "get_connection", lambda: wrapped)
        reconcile_batch(db_manager)
        assert calls["n"] < 20  # well under one-query-per-item x several tables

    def test_malformed_row_does_not_stop_the_batch(self, db_manager):
        db_manager.add_to_history("http://m/ok", "OK", package_name="OK [1080p]")
        db_manager.add_to_history("http://m/bad", None, package_name="Bad [1080p]")  # malformed title
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url IN ('http://m/ok','http://m/bad')")
        conn.commit()
        n = reconcile_batch(db_manager)
        assert n == 2  # both processed, one may categorize 'unknown'

    def test_dismissed_and_verified_not_recomputed(self, db_manager):
        db_manager.add_to_history("http://m/term1", "V", package_name="V [1080p]")
        db_manager.add_to_history("http://m/term2", "D", package_name="D [1080p]")
        db_manager.upsert_pipeline_verdict("http://m/term1", "verified")
        db_manager.upsert_pipeline_verdict("http://m/term2", "download_failed")
        db_manager.dismiss_pipeline_verdict("http://m/term2")
        n = reconcile_batch(db_manager)
        assert n == 0  # nothing eligible

    def test_grace_margin_minutes_forwarded_to_categorize_changes_verdict(self, db_manager):
        # Proves reconcile_batch's grace_margin_minutes parameter actually
        # reaches categorize() and changes its verdict — not merely that the
        # parameter exists. Same downloads/download_results/rename_jobs/
        # plex_cache rows, same timestamps; the ONLY thing that differs
        # between the two reconcile_batch calls is grace_margin_minutes.
        db_manager.add_to_history("http://m/grace", "Grace", package_name="Grace [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/grace'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('grace-uuid', 'Grace [1080p]', 'extracted', datetime('now'))")
        # Rename applied 15 minutes ago.
        processed_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO rename_jobs (package_name, original_path, status, media_type, "
            "title, year, imdb_id, resolution, processed_at) VALUES (?, ?, 'applied', 'movie', "
            "?, ?, ?, ?, ?)",
            ("Grace [1080p]", "/x/Grace.mkv", "Grace", 2024, "tt_grace", "1080p", processed_at))
        # Plex cache last refreshed "now" — only 15 minutes newer than the rename.
        now_ts = datetime.now(timezone.utc).timestamp()
        conn.execute(
            """INSERT INTO plex_cache (
                   key, title, original_title, year, res, size, imdb_id,
                   rating_key, media_id, is_tv, season, episode_count,
                   content_type, dovi, hdr, last_updated, library_name
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("rk_grace", "Grace", "Grace", 2024, "1080p", 10.0, "tt_grace",
             "rk_grace", None, 0, None, None, "Movies", 0, 0, now_ts, "Test Library"))
        conn.commit()

        # 15 minutes of cache lag is well under the DEFAULT 30-minute grace
        # margin -> categorize() must still consider the cache not-fresh-enough
        # and report 'in_progress'.
        reconcile_batch(db_manager, grace_margin_minutes=30)
        rows = db_manager.get_pipeline_verdicts()
        row = next(r for r in rows if r["url"] == "http://m/grace")
        assert row["category"] == "in_progress"

        # Same rows, same cache timestamp, second pass (non-terminal verdicts
        # are re-picked-up per test_in_progress_verdict_reconsidered_on_second_pass
        # above) — but with a small custom margin the SAME 15-minute lag now
        # counts as fresh enough, flipping the verdict to a terminal category
        # via a real find_plex_match() lookup against the plex_cache row above.
        reconcile_batch(db_manager, grace_margin_minutes=1)
        rows = db_manager.get_pipeline_verdicts()
        row = next(r for r in rows if r["url"] == "http://m/grace")
        assert row["category"] == "verified"
        assert row["plex_rating_key"] == "rk_grace"

    def test_in_progress_verdict_reconsidered_on_second_pass(self, db_manager):
        # N1 regression: a non-terminal verdict must be re-picked-up even
        # though last_grabbed_at hasn't changed.
        db_manager.add_to_history("http://m/prog", "P", package_name="P [1080p]")
        conn = db_manager.get_connection()
        conn.execute("UPDATE downloads SET last_grabbed_at = datetime('now','-1 hour') "
                     "WHERE url='http://m/prog'")
        conn.execute("INSERT INTO download_results (package_uuid, name, state, updated_at) "
                     "VALUES ('p-uuid', 'P [1080p]', 'downloading', datetime('now'))")
        conn.commit()
        reconcile_batch(db_manager)  # pass 1: writes in_progress
        n2 = reconcile_batch(db_manager)  # pass 2: must still pick it up
        assert n2 >= 1
