"""Tests for the SCANHOUND_DB_DIR contract: configurable SQLite DB directory
plus the one-time migration-copy from the legacy _DATA_DIR location.

backend.config resolves DB_PATH/CACHE_FILE at *import time*, so these tests
reload the module under a controlled environment (monkeypatched env vars and
_DATA_DIR/_BASE_DIR) rather than relying on the already-imported instance.
"""
import importlib
import os
import sqlite3
import sys

import pytest


def _legacy_data_dir(base_dir: str) -> str:
    """The actual _DATA_DIR backend.config resolves for a given base env dir
    (it appends a 'ScanHound' / 'scanhound' subfolder — see _get_data_dir)."""
    return os.path.join(base_dir, "ScanHound" if os.name == "nt" else
                         os.path.join(".local", "share", "scanhound"))


def _reload_config(monkeypatch, *, db_dir=None, data_dir):
    """Reload backend.config with SCANHOUND_DB_DIR + _DATA_DIR overridden.

    _DATA_DIR itself is patched post-reload-prep by monkeypatching the env
    vars config.py derives it from, so the legacy path lines up with the
    caller-provided data_dir.
    """
    if db_dir is None:
        monkeypatch.delenv("SCANHOUND_DB_DIR", raising=False)
    else:
        monkeypatch.setenv("SCANHOUND_DB_DIR", db_dir)

    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", data_dir)
    else:
        monkeypatch.setenv("HOME", data_dir)

    import backend.config as cfg_mod
    importlib.reload(cfg_mod)
    return cfg_mod


@pytest.fixture(autouse=True)
def _restore_config_module():
    """Reload backend.config back to its normal (test-env) state afterwards
    so later test modules don't inherit a monkeypatched DB_PATH."""
    yield
    import backend.config as cfg_mod
    importlib.reload(cfg_mod)


class TestScanhoundDbDirUnset:
    def test_cache_file_is_legacy_path_when_unset(self, tmp_path, monkeypatch):
        cfg_mod = _reload_config(monkeypatch, db_dir=None, data_dir=str(tmp_path))
        assert cfg_mod.CACHE_FILE == cfg_mod.DB_PATH
        assert os.path.dirname(cfg_mod.CACHE_FILE) == cfg_mod._DATA_DIR
        assert cfg_mod.CACHE_FILE.endswith("crawler.db")


class TestScanhoundDbDirSet:
    def test_cache_file_points_to_configured_dir(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        db_dir = tmp_path / "db_volume"
        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        assert cfg_mod.CACHE_FILE == os.path.join(str(db_dir), "crawler.db")
        assert os.path.isdir(str(db_dir))

    def test_db_dir_is_created_if_missing(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        db_dir = tmp_path / "does" / "not" / "exist" / "yet"
        assert not db_dir.exists()
        _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        assert db_dir.is_dir()


class TestMigrationCopy:
    def _make_legacy_db(self, data_dir, rows=(("a.mkv",), ("b.mkv",))):
        """Create a legacy crawler.db (with a WAL present) containing rows,
        at the real resolved _DATA_DIR (base dir + ScanHound subfolder)."""
        legacy_dir = _legacy_data_dir(str(data_dir))
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_path = os.path.join(legacy_dir, "crawler.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (name TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", rows)
        conn.commit()
        conn.close()
        return legacy_path

    def test_migrates_rows_from_legacy_location(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_legacy_db(data_dir)

        db_dir = tmp_path / "db_volume"
        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))

        new_path = cfg_mod.CACHE_FILE
        assert os.path.exists(new_path)
        conn = sqlite3.connect(new_path)
        try:
            rows = conn.execute("SELECT name FROM t ORDER BY name").fetchall()
            assert rows == [("a.mkv",), ("b.mkv",)]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            assert integrity == "ok"
        finally:
            conn.close()

    def test_migration_does_not_copy_wal_shm_sidecars(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_legacy_db(data_dir)

        db_dir = tmp_path / "db_volume"
        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))

        assert not os.path.exists(cfg_mod.CACHE_FILE + "-wal")
        assert not os.path.exists(cfg_mod.CACHE_FILE + "-shm")

    def test_running_twice_does_not_reclobber(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_legacy_db(data_dir)

        db_dir = tmp_path / "db_volume"
        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        new_path = cfg_mod.CACHE_FILE

        # Simulate new data written post-migration at the new location.
        conn = sqlite3.connect(new_path)
        conn.execute("INSERT INTO t VALUES ('c.mkv')")
        conn.commit()
        conn.close()

        # Reload again — new_path already exists, so migration must be a no-op
        # and must NOT overwrite the row added above.
        cfg_mod2 = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        conn = sqlite3.connect(cfg_mod2.CACHE_FILE)
        try:
            rows = conn.execute("SELECT name FROM t ORDER BY name").fetchall()
            assert rows == [("a.mkv",), ("b.mkv",), ("c.mkv",)]
        finally:
            conn.close()

    def test_no_legacy_db_is_noop(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        # No legacy DB created.
        db_dir = tmp_path / "db_volume"
        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        assert not os.path.exists(cfg_mod.CACHE_FILE)
        assert cfg_mod.CACHE_FILE == os.path.join(str(db_dir), "crawler.db")

    def test_new_location_already_exists_is_noop(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_legacy_db(data_dir, rows=(("legacy_only.mkv",),))

        db_dir = tmp_path / "db_volume"
        db_dir.mkdir()
        # Pre-seed the new location with different content before any reload.
        preseeded_path = os.path.join(str(db_dir), "crawler.db")
        conn = sqlite3.connect(preseeded_path)
        conn.execute("CREATE TABLE t (name TEXT)")
        conn.execute("INSERT INTO t VALUES ('already_here.mkv')")
        conn.commit()
        conn.close()

        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        conn = sqlite3.connect(cfg_mod.CACHE_FILE)
        try:
            rows = conn.execute("SELECT name FROM t").fetchall()
            assert rows == [("already_here.mkv",)]
        finally:
            conn.close()

    def test_migration_failure_falls_back_to_legacy_path(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_legacy_db(data_dir)

        db_dir = tmp_path / "db_volume"

        import shutil
        def _boom(*a, **kw):
            raise OSError("simulated copy failure")
        monkeypatch.setattr(shutil, "copy2", _boom)

        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))
        # Falls back to the legacy path rather than crashing or pointing at a
        # half-migrated new location.
        assert cfg_mod.CACHE_FILE == os.path.join(_legacy_data_dir(str(data_dir)), "crawler.db")
        assert os.path.exists(cfg_mod.CACHE_FILE)

    def test_crash_between_temp_copy_and_replace_leaves_no_partial_file(self, tmp_path, monkeypatch):
        """Simulate a crash AFTER the temp file is fully copied but BEFORE
        os.replace() swaps it into place (e.g. process killed mid-syscall).

        The atomic-copy fix must guarantee that no partial/truncated file is
        ever visible at the FINAL new_path — either the whole migrated file
        is there, or nothing is. A half-written file at new_path would trip
        the `os.path.exists(new_path)` idempotency guard on the next boot and
        make it look like migration already happened (skipping it forever),
        even though the file is actually truncated/broken.
        """
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_legacy_db(data_dir, rows=(("only_legacy.mkv",),))

        db_dir = tmp_path / "db_volume"

        real_replace = os.replace

        def _boom(*a, **kw):
            raise OSError("simulated crash between temp copy and replace")

        monkeypatch.setattr(os, "replace", _boom)

        cfg_mod = _reload_config(monkeypatch, db_dir=str(db_dir), data_dir=str(data_dir))

        # Migration failed (replace never happened) -> falls back to legacy path.
        assert cfg_mod.CACHE_FILE == os.path.join(_legacy_data_dir(str(data_dir)), "crawler.db")

        # No partial/truncated file left behind at the FINAL new_path.
        final_new_path = os.path.join(str(db_dir), "crawler.db")
        assert not os.path.exists(final_new_path), (
            "a partial file at new_path would fool the next boot's "
            "idempotency guard into skipping migration forever")

        # The temp sibling may still exist on disk (replace() raised before
        # swapping it in) — that's fine, it's never mistaken for the real DB
        # since _resolve_db_path only ever checks os.path.exists(new_path).
        # What matters is it's a DIFFERENT filename, not the final new_path.
        temp_path = final_new_path + ".migrating"
        if os.path.exists(temp_path):
            assert temp_path != final_new_path

        # Legacy DB is intact and untouched.
        legacy_path = os.path.join(_legacy_data_dir(str(data_dir)), "crawler.db")
        assert os.path.exists(legacy_path)
        conn = sqlite3.connect(legacy_path)
        try:
            rows = conn.execute("SELECT name FROM t").fetchall()
            assert rows == [("only_legacy.mkv",)]
        finally:
            conn.close()

        monkeypatch.setattr(os, "replace", real_replace)
