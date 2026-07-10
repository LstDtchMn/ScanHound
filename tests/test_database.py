"""Comprehensive tests for backend/database.py DatabaseManager."""

import json
import os
import sqlite3
import threading
from unittest.mock import patch

import pytest

from backend.database import DatabaseManager


# ---------------------------------------------------------------------------
# 1. Table creation / init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    """Verify that init_db creates all expected tables and indexes."""

    EXPECTED_TABLES = [
        "downloads",
        "plex_cache",
        "scan_history",
        "scanned_urls",
        "dismissed_items",
    ]

    def test_tables_exist(self, db_manager):
        """All four core tables must exist after init."""
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        for table in self.EXPECTED_TABLES:
            assert table in tables, f"Missing table: {table}"

    def test_downloads_columns(self, db_manager):
        """Downloads table must include migration columns."""
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(downloads)")
        col_names = {row[1] for row in cursor.fetchall()}
        for col in ("url", "title", "date_added", "normalized_title",
                     "season", "resolution", "size"):
            assert col in col_names, f"Missing column in downloads: {col}"

    def test_indexes_created(self, db_manager):
        """Key indexes should be present after init."""
        conn = db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        for idx in ("idx_plex_cache_imdb_id", "idx_plex_cache_title",
                     "idx_plex_cache_tv_season", "idx_plex_cache_year",
                     "idx_downloads_date", "idx_scan_history_timestamp"):
            assert idx in indexes, f"Missing index: {idx}"

    def test_reinit_is_idempotent(self, db_manager):
        """Calling init_db a second time must not raise or lose data."""
        db_manager.add_to_history("http://example.com", "Test")
        db_manager.init_db()
        assert db_manager.is_in_history("http://example.com")

    def test_init_depth_resets_after_recovery_failure(self, db_manager):
        """A failed corruption recovery must not poison future init attempts."""
        with patch.object(db_manager, "get_connection", side_effect=sqlite3.DatabaseError("boom")):
            with patch("backend.database.os.rename", side_effect=OSError("nope")):
                db_manager.init_db()

        assert db_manager._init_depth == 0


# ---------------------------------------------------------------------------
# 2. Connection management
# ---------------------------------------------------------------------------

class TestGetConnection:

    def test_returns_connection(self, db_manager):
        conn = db_manager.get_connection()
        assert conn is not None

    def test_row_factory_is_sqlite_row(self, db_manager):
        conn = db_manager.get_connection()
        assert conn.row_factory is sqlite3.Row

    def test_same_connection_on_repeat_call(self, db_manager):
        c1 = db_manager.get_connection()
        c2 = db_manager.get_connection()
        assert c1 is c2

    def test_reconnects_after_close(self, db_manager):
        c1 = db_manager.get_connection()
        db_manager.close()
        c2 = db_manager.get_connection()
        assert c2 is not None
        assert c2 is not c1


# ---------------------------------------------------------------------------
# 3. Download history CRUD
# ---------------------------------------------------------------------------

class TestDismissedItems:

    def test_add_and_get(self, db_manager):
        assert db_manager.get_dismissed_count() == 0
        db_manager.add_dismissed_item("http://x/a", "Movie A")
        assert db_manager.get_dismissed_urls() == {"http://x/a"}
        assert db_manager.get_dismissed_count() == 1

    def test_add_is_idempotent(self, db_manager):
        db_manager.add_dismissed_item("http://x/a", "A")
        db_manager.add_dismissed_item("http://x/a", "A again")
        assert db_manager.get_dismissed_count() == 1

    def test_remove(self, db_manager):
        db_manager.add_dismissed_item("http://x/a")
        db_manager.add_dismissed_item("http://x/b")
        db_manager.remove_dismissed_item("http://x/a")
        assert db_manager.get_dismissed_urls() == {"http://x/b"}

    def test_get_items_includes_title(self, db_manager):
        db_manager.add_dismissed_item("http://x/a", "Movie A")
        items = db_manager.get_dismissed_items()
        assert len(items) == 1
        assert items[0]["url"] == "http://x/a"
        assert items[0]["title"] == "Movie A"

    def test_clear(self, db_manager):
        db_manager.add_dismissed_item("http://x/a")
        db_manager.add_dismissed_item("http://x/b")
        db_manager.clear_dismissed_items()
        assert db_manager.get_dismissed_count() == 0

    def test_redismiss_fills_in_missing_title(self, db_manager):
        db_manager.add_dismissed_item("http://x/a")
        db_manager.add_dismissed_item("http://x/a", "Movie A")
        items = db_manager.get_dismissed_items()
        assert items[0]["title"] == "Movie A"

    def test_redismiss_without_title_keeps_existing_title(self, db_manager):
        db_manager.add_dismissed_item("http://x/a", "Movie A")
        db_manager.add_dismissed_item("http://x/a")
        items = db_manager.get_dismissed_items()
        assert items[0]["title"] == "Movie A"

    def test_add_dismissed_items_batch(self, db_manager):
        db_manager.add_dismissed_items([("http://x/a", "A"), ("http://x/b", "B")])
        assert db_manager.get_dismissed_urls() == {"http://x/a", "http://x/b"}

    def test_remove_dismissed_items_batch(self, db_manager):
        db_manager.add_dismissed_items([("http://x/a", None), ("http://x/b", None)])
        db_manager.remove_dismissed_items(["http://x/a", "http://x/b"])
        assert db_manager.get_dismissed_urls() == set()

    def test_reset_applying_rename_jobs(self, db_manager):
        # Crash recovery: jobs stuck 'applying' after an unclean shutdown must
        # be reset to 'matched' (retriable); other statuses are untouched.
        a = db_manager.create_rename_job({"original_path": "/x/a.mkv", "status": "applying"})
        b = db_manager.create_rename_job({"original_path": "/x/b.mkv", "status": "applying"})
        c = db_manager.create_rename_job({"original_path": "/x/c.mkv", "status": "applied"})
        d = db_manager.create_rename_job({"original_path": "/x/d.mkv", "status": "needs_review"})
        n = db_manager.reset_applying_rename_jobs()
        assert n == 2
        assert db_manager.get_rename_job(a)["status"] == "matched"
        assert db_manager.get_rename_job(b)["status"] == "matched"
        assert db_manager.get_rename_job(c)["status"] == "applied"        # untouched
        assert db_manager.get_rename_job(d)["status"] == "needs_review"   # untouched
        assert db_manager.reset_applying_rename_jobs() == 0  # idempotent

    def test_reset_applying_restores_prior_status(self, db_manager):
        # A job flipped needs_review -> applying (bulk-apply) must come BACK as
        # needs_review on crash recovery, not be silently promoted to auto-
        # appliable 'matched'. Legacy rows with no prior_status fall back to
        # 'matched'.
        gated = db_manager.create_rename_job(
            {"original_path": "/x/gated.mkv", "status": "applying",
             "prior_status": "needs_review"})
        legacy = db_manager.create_rename_job(
            {"original_path": "/x/legacy.mkv", "status": "applying"})  # no prior_status
        assert db_manager.reset_applying_rename_jobs() == 2
        assert db_manager.get_rename_job(gated)["status"] == "needs_review"
        assert db_manager.get_rename_job(legacy)["status"] == "matched"
        # prior_status is cleared so a later real apply can't re-stash a stale value.
        assert not db_manager.get_rename_job(gated).get("prior_status")

    def test_dismiss_records_title_quality(self, db_manager):
        # Rich tuple (url, title, group_key, resolution, dovi) powers title-level skip.
        db_manager.add_dismissed_items([("http://x/a", "Heat", "heat|1995", "1080p", False)])
        assert [tuple(r) for r in db_manager.get_dismissed_title_quality()] == [("heat|1995", "1080p", 0)]

    def test_dismiss_two_arg_tuple_has_no_group_key(self, db_manager):
        # Legacy (url, title) dismissals record no group_key → excluded from the
        # title-quality query (they still hide by exact URL).
        db_manager.add_dismissed_items([("http://x/a", "Heat")])
        assert list(db_manager.get_dismissed_title_quality()) == []

    def test_redismiss_backfills_group_key(self, db_manager):
        # A bare URL dismissal, later re-dismissed with metadata, gains the fields.
        db_manager.add_dismissed_item("http://x/a")
        db_manager.add_dismissed_items([("http://x/a", "Heat", "heat|1995", "4K", True)])
        assert [tuple(r) for r in db_manager.get_dismissed_title_quality()] == [("heat|1995", "4K", 1)]


class TestDownloadHistory:

    def test_add_and_check_history(self, db_manager):
        url = "http://example.com/movie1"
        assert not db_manager.is_in_history(url)
        db_manager.add_to_history(url, "Movie 1")
        assert db_manager.is_in_history(url)

    def test_history_count_increments(self, db_manager):
        assert db_manager.get_history_count() == 0
        db_manager.add_to_history("http://a.com", "A")
        assert db_manager.get_history_count() == 1
        db_manager.add_to_history("http://b.com", "B")
        assert db_manager.get_history_count() == 2

    def test_duplicate_url_replaces(self, db_manager):
        """INSERT OR REPLACE should keep count at 1 for same URL."""
        db_manager.add_to_history("http://a.com", "A")
        db_manager.add_to_history("http://a.com", "A v2")
        assert db_manager.get_history_count() == 1

    def test_clear_history(self, db_manager):
        db_manager.add_to_history("http://a.com", "A")
        db_manager.add_to_history("http://b.com", "B")
        assert db_manager.get_history_count() == 2
        db_manager.clear_history()
        assert db_manager.get_history_count() == 0

    def test_add_with_metadata(self, db_manager):
        db_manager.add_to_history(
            "http://x.com", "X", normalized_title="x",
            season=2, resolution="4K", size="15 GB",
        )
        assert db_manager.is_in_history("http://x.com")
        rows = db_manager.get_downloaded_titles()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["normalized_title"] == "x"
        assert row["season"] == 2
        assert row["resolution"] == "4K"
        assert row["size"] == "15 GB"

    def test_add_with_none_metadata(self, db_manager):
        """None optional fields should still succeed."""
        result = db_manager.add_to_history("http://n.com", "N")
        assert result is True
        assert db_manager.is_in_history("http://n.com")

    def test_is_in_history_false_for_missing(self, db_manager):
        assert not db_manager.is_in_history("http://nonexistent.com")

    def test_empty_history_count(self, db_manager):
        assert db_manager.get_history_count() == 0


class TestDownloadResults:

    def test_empty_results(self, db_manager):
        assert db_manager.get_download_results() == []

    def test_upsert_and_get(self, db_manager):
        db_manager.upsert_download_result(
            name="pkg1", title="Movie 1 [4K]", host="rapidgator.net",
            bytes_total=1000, bytes_loaded=500, downloaded=0,
            extraction="na", state="downloading", error=None,
        )
        rows = db_manager.get_download_results()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "pkg1"
        assert row["title"] == "Movie 1 [4K]"
        assert row["host"] == "rapidgator.net"
        assert row["bytes_total"] == 1000
        assert row["bytes_loaded"] == 500
        assert row["downloaded"] == 0
        assert row["extraction"] == "na"
        assert row["state"] == "downloading"
        assert row["error"] is None

    def test_upsert_same_name_updates_in_place(self, db_manager):
        db_manager.upsert_download_result(name="pkg1", state="downloading", bytes_loaded=500)
        db_manager.upsert_download_result(name="pkg1", state="downloaded", bytes_loaded=1000, downloaded=1)
        rows = db_manager.get_download_results()
        assert len(rows) == 1
        assert rows[0]["state"] == "downloaded"
        assert rows[0]["bytes_loaded"] == 1000
        assert rows[0]["downloaded"] == 1

    def test_get_results_returns_all_tracked_packages(self, db_manager):
        db_manager.upsert_download_result(name="pkg-old", state="downloaded")
        db_manager.upsert_download_result(name="pkg-new", state="downloading")
        names = {r["name"] for r in db_manager.get_download_results()}
        assert names == {"pkg-old", "pkg-new"}

    def test_get_results_respects_limit(self, db_manager):
        for i in range(5):
            db_manager.upsert_download_result(name=f"pkg{i}", state="queued")
        rows = db_manager.get_download_results(limit=2)
        assert len(rows) == 2

    def test_clear_results(self, db_manager):
        db_manager.upsert_download_result(name="pkg1", state="downloaded")
        db_manager.upsert_download_result(name="pkg2", state="downloaded")
        assert len(db_manager.get_download_results()) == 2
        db_manager.clear_download_results()
        assert db_manager.get_download_results() == []

    def test_upsert_with_error(self, db_manager):
        db_manager.upsert_download_result(name="pkg1", state="failed", error="Extraction error")
        rows = db_manager.get_download_results()
        assert rows[0]["error"] == "Extraction error"

    def test_delete_download_result_removes_only_named_row(self, db_manager):
        foo_id = db_manager.upsert_download_result(
            name="Foo [1080p]", title="Foo", host="rg.net",
            bytes_total=100, bytes_loaded=50, downloaded=0,
            extraction="na", state="downloading", error=None)
        db_manager.upsert_download_result(name="Bar [4K]", title="Bar", host="rg.net",
                                          bytes_total=100, bytes_loaded=100, downloaded=1,
                                          extraction="success", state="extracted", error=None)
        n = db_manager.delete_download_result(foo_id)
        assert n == 1
        names = {r["name"] for r in db_manager.get_download_results()}
        assert names == {"Bar [4K]"}

    def test_delete_download_result_missing_is_noop(self, db_manager):
        assert db_manager.delete_download_result(999999) == 0

    def test_upsert_two_same_name_uuids_coexist(self, db_manager):
        a = db_manager.upsert_download_result("Foo", package_uuid="111", state="downloading")
        b = db_manager.upsert_download_result("Foo", package_uuid="222", state="downloading")
        assert a != b
        rows = db_manager.get_download_results()
        assert {r["package_uuid"] for r in rows} == {"111", "222"}

    def test_upsert_update_by_uuid_and_name_change(self, db_manager):
        i = db_manager.upsert_download_result("Foo", package_uuid="111")
        j = db_manager.upsert_download_result("Foo RENAMED", package_uuid="111")
        assert i == j
        row = [r for r in db_manager.get_download_results() if r["id"] == i][0]
        assert row["name"] == "Foo RENAMED"

    def test_upsert_adopts_legacy_null_uuid_row(self, db_manager):
        # Legacy row (no uuid), then a live poll of the same name with a uuid -> adopts it.
        legacy = db_manager.upsert_download_result("Foo", package_uuid=None)
        adopted = db_manager.upsert_download_result("Foo", package_uuid="111")
        assert adopted == legacy
        rows = db_manager.get_download_results()
        assert len(rows) == 1 and rows[0]["package_uuid"] == "111"

    def test_unique_uuid_index_rejects_second_row(self, db_manager):
        db_manager.upsert_download_result("A", package_uuid="111")
        # A direct duplicate-uuid insert must be rejected by the partial unique index.
        with pytest.raises(sqlite3.IntegrityError):
            db_manager.get_connection().execute(
                "INSERT INTO download_results (package_uuid, name) VALUES ('111','B')")

    def test_delete_by_id_removes_one_of_two_same_name(self, db_manager):
        a = db_manager.upsert_download_result("Foo", package_uuid="111")
        db_manager.upsert_download_result("Foo", package_uuid="222")
        assert db_manager.delete_download_result(a) == 1
        assert {r["package_uuid"] for r in db_manager.get_download_results()} == {"222"}


class TestDownloadResultsSchemaMigration:
    """download_results: name-PK -> surrogate-id rebuild (crash-safe, once)."""

    def test_migration_preserves_legacy_rows(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        # Seed an OLD-shape download_results table + rows, then open DatabaseManager.
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE download_results (name TEXT PRIMARY KEY, title TEXT, "
                     "host TEXT, bytes_total INTEGER, bytes_loaded INTEGER, downloaded INTEGER, "
                     "extraction TEXT, state TEXT, error TEXT, updated_at TIMESTAMP)")
        conn.execute("INSERT INTO download_results (name,title,host,bytes_total,bytes_loaded,"
                     "downloaded,extraction,state,error) VALUES "
                     "('Foo [1080p]','Foo','rapidgator',100,100,1,'success','finished',NULL)")
        conn.commit(); conn.close()
        db = DatabaseManager(db_path=db_path)
        cols = {r[1] for r in db.get_connection().execute("PRAGMA table_info(download_results)")}
        assert "id" in cols and "package_uuid" in cols
        rows = db.get_download_results()
        assert len(rows) == 1
        assert rows[0]["name"] == "Foo [1080p]" and rows[0]["package_uuid"] is None
        assert isinstance(rows[0]["id"], int)
        db.close()

    def test_migration_idempotent_and_indexes_on_fresh_db(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))  # fresh -> new schema directly
        idx = {r[1] for r in db.get_connection().execute("PRAGMA index_list(download_results)")}
        assert "idx_download_results_uuid" in idx
        # second open is a no-op (idempotent)
        db.close(); db2 = DatabaseManager(db_path=str(tmp_path / "t.db")); db2.close()

    def test_orphan_new_table_does_not_break_rebuild(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE download_results (name TEXT PRIMARY KEY, title TEXT, host TEXT, "
                     "bytes_total INTEGER, bytes_loaded INTEGER, downloaded INTEGER, extraction TEXT, "
                     "state TEXT, error TEXT, updated_at TIMESTAMP)")
        conn.execute("CREATE TABLE download_results_new (x INTEGER)")  # planted orphan
        conn.commit(); conn.close()
        db = DatabaseManager(db_path=db_path)  # must not raise
        assert "id" in {r[1] for r in db.get_connection().execute("PRAGMA table_info(download_results)")}
        db.close()


class TestLegacyMigration:

    def test_migrate_history_json_imports_rows_and_backs_up(self, db_manager, tmp_path):
        history_file = tmp_path / "download_history.json"
        history_file.write_text(
            json.dumps(["http://example.com/a", "http://example.com/b"]),
            encoding="utf-8",
        )

        migrated_history, migrated_cache = db_manager.migrate_json_data(
            str(history_file),
            str(tmp_path / "cache.json"),
        )

        assert migrated_history == 2
        assert migrated_cache == 0
        assert db_manager.get_history_count() == 2
        assert not history_file.exists()
        assert os.path.exists(str(history_file) + ".bak")

    def test_migrate_history_json_is_idempotent(self, db_manager, tmp_path):
        history_file = tmp_path / "download_history.json"
        payload = ["http://example.com/a", "http://example.com/b"]
        history_file.write_text(json.dumps(payload), encoding="utf-8")

        db_manager.migrate_json_data(str(history_file), str(tmp_path / "cache.json"))
        assert db_manager.get_history_count() == 2

        history_file.write_text(json.dumps(payload), encoding="utf-8")
        migrated_history, _ = db_manager.migrate_json_data(
            str(history_file),
            str(tmp_path / "cache.json"),
        )

        assert migrated_history == 2
        assert db_manager.get_history_count() == 2
        assert os.path.exists(str(history_file) + ".bak")


# ---------------------------------------------------------------------------
# 4. Plex cache save / load with boolean conversion
# ---------------------------------------------------------------------------

class TestPlexCache:

    @pytest.fixture
    def sample_movie_items(self):
        return [
            {
                "clean_title": "the matrix",
                "original_title": "The Matrix",
                "year": 1999,
                "res": "1080p",
                "size": 15.0,
                "imdb_id": "tt0133093",
                "rating_key": "1001",
                "media_id": "m1001",
                "dovi": True,
                "hdr": False,
            },
            {
                "clean_title": "inception",
                "original_title": "Inception",
                "year": 2010,
                "res": "4K",
                "size": 55.0,
                "imdb_id": "tt1375666",
                "rating_key": "1002",
                "media_id": "m1002",
                "dovi": False,
                "hdr": True,
            },
        ]

    @pytest.fixture
    def sample_tv_items(self):
        return [
            {
                "clean_title": "breaking bad",
                "original_title": "Breaking Bad",
                "year": 2008,
                "res": "1080p",
                "size": 45.0,
                "imdb_id": "tt0903747",
                "rating_key": "2001",
                "season": 1,
                "episode_count": 7,
                "dovi": False,
                "hdr": False,
            },
        ]

    def test_save_and_load_movies(self, db_manager, sample_movie_items):
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        loaded = db_manager.load_plex_cache("Movies")
        assert len(loaded) == 2
        titles = {item["clean_title"] for item in loaded}
        assert titles == {"the matrix", "inception"}

    def test_boolean_conversion_on_load(self, db_manager, sample_movie_items):
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        loaded = db_manager.load_plex_cache("Movies")
        matrix = [i for i in loaded if i["clean_title"] == "the matrix"][0]
        inception = [i for i in loaded if i["clean_title"] == "inception"][0]

        assert matrix["dovi"] is True
        assert matrix["hdr"] is False
        assert inception["dovi"] is False
        assert inception["hdr"] is True
        # is_tv should be False for movies
        assert matrix["is_tv"] is False

    def test_clean_title_mapping(self, db_manager, sample_movie_items):
        """DB stores 'title', but load maps it back to 'clean_title'."""
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        loaded = db_manager.load_plex_cache("Movies")
        for item in loaded:
            assert "clean_title" in item

    def test_save_tv_shows(self, db_manager, sample_tv_items):
        db_manager.save_plex_cache(sample_tv_items, "TV Shows")
        loaded = db_manager.load_plex_cache("TV Shows")
        assert len(loaded) == 1
        assert loaded[0]["is_tv"] is True
        assert loaded[0]["season"] == 1

    def test_load_empty_cache(self, db_manager):
        loaded = db_manager.load_plex_cache("Movies")
        assert loaded == []

    def test_clear_plex_cache(self, db_manager, sample_movie_items):
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        db_manager.clear_plex_cache()
        assert db_manager.load_plex_cache("Movies") == []

    def test_modes_are_independent(self, db_manager, sample_movie_items,
                                    sample_tv_items):
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        db_manager.save_plex_cache(sample_tv_items, "TV Shows")
        assert len(db_manager.load_plex_cache("Movies")) == 2
        assert len(db_manager.load_plex_cache("TV Shows")) == 1

    def test_save_empty_list_is_noop(self, db_manager):
        """Passing an empty list should not raise or crash."""
        db_manager.save_plex_cache([], "Movies")
        assert db_manager.load_plex_cache("Movies") == []

    def test_upsert_replaces_existing(self, db_manager, sample_movie_items):
        """Saving the same items twice should upsert, not duplicate."""
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        db_manager.save_plex_cache(sample_movie_items, "Movies")
        loaded = db_manager.load_plex_cache("Movies")
        assert len(loaded) == 2

    def test_multi_part_media_rows_both_survive(self, db_manager):
        """Regression (Task-2 review finding): a single Plex Media with TWO
        Parts (e.g. a two-file DVD rip / split file) is emitted by
        _extract_movie_data as two dicts sharing the same rating_key and
        media_id, distinguished only by a per-part 'key'. Without a per-part
        key, both rows collide on the fallback f"{rating_key}_{media_id}"
        primary key and INSERT OR REPLACE silently drops one — only the
        last part's row survives. This test asserts both rows persist
        across a save + reload cycle.
        """
        items = [
            {
                "clean_title": "two part movie",
                "original_title": "Two Part Movie",
                "year": 2001,
                "res": "1080p",
                "size": 4.5,
                "imdb_id": "tt9999999",
                "rating_key": "5001",
                "media_id": "m5001",
                "dovi": False,
                "hdr": False,
                "file": "Y:/Movies/Two Part Movie/cd1.mkv",
                "key": "5001_m5001_0",
            },
            {
                "clean_title": "two part movie",
                "original_title": "Two Part Movie",
                "year": 2001,
                "res": "1080p",
                "size": 4.3,
                "imdb_id": "tt9999999",
                "rating_key": "5001",
                "media_id": "m5001",
                "dovi": False,
                "hdr": False,
                "file": "Y:/Movies/Two Part Movie/cd2.mkv",
                "key": "5001_m5001_1",
            },
        ]

        db_manager.save_plex_cache(items, "Movies")
        loaded = db_manager.load_plex_cache("Movies")

        matches = [i for i in loaded if i["rating_key"] == "5001"]
        assert len(matches) == 2, (
            f"expected both multi-part rows to survive, got {len(matches)}: {matches}"
        )
        sizes = {i["size"] for i in matches}
        assert sizes == {4.5, 4.3}


# ---------------------------------------------------------------------------
# 5. Scan history
# ---------------------------------------------------------------------------

class TestScanHistory:

    @pytest.fixture
    def scan_data(self):
        return {
            "timestamp": "2025-01-15T10:30:00",
            "scan_type": "Full Scan",
            "items_scanned": 100,
            "missing_count": 10,
            "upgrade_count": 5,
            "dv_upgrade_count": 2,
            "in_library_count": 83,
            "duration_seconds": 45.5,
            "sources_scanned": "source1,source2",
            "plex_items_cached": 500,
        }

    def test_save_and_retrieve(self, db_manager, scan_data):
        db_manager.save_scan_history(scan_data)
        history = db_manager.get_scan_history()
        assert len(history) == 1
        h = history[0]
        assert h["scan_type"] == "Full Scan"
        assert h["items_scanned"] == 100
        assert h["missing_count"] == 10

    def test_get_scan_history_limit(self, db_manager, scan_data):
        for i in range(5):
            data = dict(scan_data)
            data["timestamp"] = f"2025-01-{15+i:02d}T10:30:00"
            db_manager.save_scan_history(data)
        assert len(db_manager.get_scan_history(limit=3)) == 3
        assert len(db_manager.get_scan_history(limit=50)) == 5

    def test_get_scan_history_ordered_desc(self, db_manager, scan_data):
        for i in range(3):
            data = dict(scan_data)
            data["timestamp"] = f"2025-01-{15+i:02d}T10:30:00"
            db_manager.save_scan_history(data)
        history = db_manager.get_scan_history()
        timestamps = [h["timestamp"] for h in history]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_scan_stats(self, db_manager, scan_data):
        db_manager.save_scan_history(scan_data)
        second = dict(scan_data)
        second["items_scanned"] = 200
        second["missing_count"] = 20
        second["upgrade_count"] = 10
        second["duration_seconds"] = 60.0
        second["timestamp"] = "2025-01-16T10:30:00"
        db_manager.save_scan_history(second)

        stats = db_manager.get_scan_stats()
        assert stats["total_scans"] == 2
        assert stats["total_items_scanned"] == 300
        assert stats["total_missing"] == 30
        assert stats["total_upgrades"] == 15
        assert stats["avg_duration"] == 52.75  # (45.5 + 60) / 2
        assert stats["last_scan"] == "2025-01-16T10:30:00"

    def test_get_scan_stats_empty(self, db_manager):
        stats = db_manager.get_scan_stats()
        assert stats["total_scans"] == 0
        assert stats["avg_duration"] == 0

    def test_clear_scan_history(self, db_manager, scan_data):
        db_manager.save_scan_history(scan_data)
        db_manager.clear_scan_history()
        assert db_manager.get_scan_history() == []

    def test_scan_data_defaults(self, db_manager):
        """Missing keys should fall back to defaults."""
        db_manager.save_scan_history({"timestamp": "2025-01-01T00:00:00"})
        history = db_manager.get_scan_history()
        assert len(history) == 1
        h = history[0]
        assert h["scan_type"] == "Full Scan"
        assert h["items_scanned"] == 0
        assert h["missing_count"] == 0


# ---------------------------------------------------------------------------
# 6. Scanned URLs tracking
# ---------------------------------------------------------------------------

class TestScannedUrls:

    def test_add_and_check(self, db_manager):
        url = "http://example.com/page1"
        assert not db_manager.is_url_scanned(url)
        db_manager.add_scanned_url(url, title="Page 1", source="test")
        assert db_manager.is_url_scanned(url)

    def test_get_scanned_urls(self, db_manager):
        db_manager.add_scanned_url("http://a.com", "A", "src")
        db_manager.add_scanned_url("http://b.com", "B", "src")
        urls = db_manager.get_scanned_urls()
        assert isinstance(urls, set)
        assert urls == {"http://a.com", "http://b.com"}

    def test_clear_scanned_urls(self, db_manager):
        db_manager.add_scanned_url("http://a.com")
        db_manager.clear_scanned_urls()
        assert not db_manager.is_url_scanned("http://a.com")
        assert db_manager.get_scanned_urls() == set()

    def test_duplicate_url_ignored(self, db_manager):
        """INSERT OR IGNORE should not raise on duplicates."""
        db_manager.add_scanned_url("http://a.com", "A", "src")
        db_manager.add_scanned_url("http://a.com", "A v2", "src2")
        assert db_manager.get_scanned_url_count() == 1

    def test_scanned_url_count(self, db_manager):
        assert db_manager.get_scanned_url_count() == 0
        db_manager.add_scanned_url("http://a.com")
        db_manager.add_scanned_url("http://b.com")
        assert db_manager.get_scanned_url_count() == 2

    def test_add_scanned_urls_batch(self, db_manager):
        batch = [
            {"url": "http://a.com", "title": "A", "source": "s"},
            {"url": "http://b.com", "title": "B", "source": "s"},
            {"url": "http://c.com", "title": "C", "source": "s"},
        ]
        result = db_manager.add_scanned_urls_batch(batch)
        assert result is True
        assert db_manager.get_scanned_url_count() == 3

    def test_add_scanned_url_with_none_fields(self, db_manager):
        result = db_manager.add_scanned_url("http://z.com", title=None, source=None)
        assert result is True
        assert db_manager.is_url_scanned("http://z.com")


class TestBackgroundCacheVersion:
    """B2: get_background_cache_version() is the cheap invalidation signal
    backend/api/routes/results.py uses to avoid re-parsing every cached
    row's JSON blob on each request."""

    def test_empty_cache(self, db_manager):
        # (count, max_last_seen_at, monotonic_rev)
        assert db_manager.get_background_cache_version() == (0, None, 0)

    def test_changes_on_insert(self, db_manager):
        before = db_manager.get_background_cache_version()
        db_manager.upsert_background_cache([
            {"url": "u/1", "title": "A", "year": 2024, "status": "missing",
             "source_category": "4k", "data": "{}"},
        ])
        after = db_manager.get_background_cache_version()
        assert after != before
        assert after[0] == 1

    def test_changes_on_reupsert_same_url_same_second(self, db_manager):
        db_manager.upsert_background_cache([
            {"url": "u/1", "title": "A", "year": 2024, "status": "missing",
             "source_category": "4k", "data": "{}"},
        ])
        first = db_manager.get_background_cache_version()
        # Re-upserting the same URL changes NO row count and (within the same
        # wall-clock second) NO last_seen_at either -- the monotonic rev must
        # still change so a same-second re-scrape invalidates the parse cache.
        # No sleep: this is exactly the sub-second window the rev counter closes.
        db_manager.upsert_background_cache([
            {"url": "u/1", "title": "A2", "year": 2024, "status": "missing",
             "source_category": "4k", "data": "{}"},
        ])
        second = db_manager.get_background_cache_version()
        assert second[0] == first[0] == 1  # same count
        assert second != first             # but version changed (via rev)
        assert second[2] > first[2]        # monotonic rev bumped

    def test_changes_on_clear(self, db_manager):
        db_manager.upsert_background_cache([
            {"url": "u/1", "title": "A", "year": 2024, "status": "missing",
             "source_category": "4k", "data": "{}"},
        ])
        before = db_manager.get_background_cache_version()
        db_manager.clear_background_cache()
        after = db_manager.get_background_cache_version()
        assert after != before
        assert after[2] > before[2]


# ---------------------------------------------------------------------------
# 7. Transaction context manager
# ---------------------------------------------------------------------------

class TestTransaction:

    def test_transaction_yields_connection(self, db_manager):
        with db_manager.transaction() as conn:
            assert conn is not None

    def test_transaction_allows_direct_sql(self, db_manager):
        db_manager.add_to_history("http://x.com", "X")
        with db_manager.transaction() as conn:
            row = conn.execute(
                "SELECT title FROM downloads WHERE url = ?",
                ("http://x.com",),
            ).fetchone()
            assert row is not None
            assert row[0] == "X"

    def test_transaction_write_and_commit(self, db_manager):
        with db_manager.transaction() as conn:
            conn.execute(
                "INSERT INTO downloads (url, title) VALUES (?, ?)",
                ("http://tx.com", "TX"),
            )
            conn.commit()
        assert db_manager.is_in_history("http://tx.com")


# ---------------------------------------------------------------------------
# 8. Thread safety (basic)
# ---------------------------------------------------------------------------

class TestThreadSafety:

    def test_concurrent_writes(self, db_manager):
        """Multiple threads adding to history should not corrupt data."""
        errors = []

        def worker(thread_id):
            try:
                for i in range(20):
                    url = f"http://thread{thread_id}.com/page{i}"
                    db_manager.add_to_history(url, f"Thread {thread_id} Page {i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert db_manager.get_history_count() == 100  # 5 threads * 20 items

    def test_concurrent_reads_and_writes(self, db_manager):
        """Mix of reads and writes should not crash."""
        errors = []

        def writer():
            try:
                for i in range(30):
                    db_manager.add_to_history(f"http://w.com/{i}", f"W{i}")
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(30):
                    db_manager.get_history_count()
                    db_manager.is_in_history("http://w.com/0")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ---------------------------------------------------------------------------
# 9. Auto-recovery for corrupt database
# ---------------------------------------------------------------------------

class TestAutoRecovery:

    def test_corrupt_db_triggers_recovery(self, tmp_path):
        """Writing garbage to the DB file, then init should rename and recreate."""
        db_path = str(tmp_path / "corrupt.db")
        # Write invalid data to simulate corruption
        with open(db_path, "wb") as f:
            f.write(b"this is not a valid sqlite database at all!!!")

        dm = DatabaseManager(db_path=db_path)
        # After recovery, a fresh working database should exist
        # The corrupt file should have been renamed
        corrupt_files = [f for f in os.listdir(tmp_path) if ".corrupt." in f]
        assert len(corrupt_files) >= 1, "Corrupt DB was not renamed"
        # The new DB should be functional
        dm.add_to_history("http://post-recovery.com", "Recovered")
        assert dm.is_in_history("http://post-recovery.com")
        dm.close()


# ---------------------------------------------------------------------------
# 10. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_history_with_special_characters_in_url(self, db_manager):
        url = "http://example.com/path?q=hello&a=world#frag"
        db_manager.add_to_history(url, "Special")
        assert db_manager.is_in_history(url)

    def test_history_with_unicode_title(self, db_manager):
        db_manager.add_to_history("http://uni.com", "Film: Les Miserables")
        assert db_manager.is_in_history("http://uni.com")

    def test_plex_cache_item_without_key_gets_fallback(self, db_manager):
        """Items without 'key' should get a generated fallback key."""
        item = {
            "clean_title": "no key movie",
            "original_title": "No Key Movie",
            "year": 2020,
            "res": "1080p",
            "size": 10.0,
            "imdb_id": "tt0000001",
            "rating_key": "9001",
            "media_id": "m9001",
            "dovi": False,
            "hdr": False,
        }
        db_manager.save_plex_cache([item], "Movies")
        loaded = db_manager.load_plex_cache("Movies")
        assert len(loaded) == 1
        assert loaded[0]["clean_title"] == "no key movie"

    def test_get_scan_stats_returns_dict_on_empty(self, db_manager):
        stats = db_manager.get_scan_stats()
        assert isinstance(stats, dict)

    def test_close_idempotent(self, db_manager):
        """Closing twice should not raise."""
        db_manager.close()
        db_manager.close()

    def test_operations_after_close_reconnect(self, db_manager):
        """DB should auto-reconnect if used after close."""
        db_manager.add_to_history("http://before.com", "Before")
        db_manager.close()
        # Operations should still work because get_connection reconnects
        db_manager.add_to_history("http://after.com", "After")
        assert db_manager.is_in_history("http://after.com")

