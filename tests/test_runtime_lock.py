"""Tests for the process-lifetime single-writer runtime lock."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

import backend.app_service as app_service
import backend.runtime_lock as runtime_lock_module
from backend.runtime_lock import (
    RuntimeWriterLock,
    RuntimeWriterLockError,
    require_writer_lock,
    writer_lock_path,
)


def _python(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        cwd=os.getcwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_require_writer_lock_rejects_unowned_mutation():
    # The root conftest intentionally bypasses guards for direct fileops tests.
    # This focused enforcement test temporarily restores production semantics.
    with runtime_lock_module._STATE_LOCK:
        previous_depth = runtime_lock_module._TEST_BYPASS_DEPTH
        runtime_lock_module._TEST_BYPASS_DEPTH = 0
    try:
        with pytest.raises(RuntimeWriterLockError, match="writer lock is not held"):
            require_writer_lock()
    finally:
        with runtime_lock_module._STATE_LOCK:
            runtime_lock_module._TEST_BYPASS_DEPTH = previous_depth


def test_lock_allows_guarded_mutation(tmp_path):
    lock = RuntimeWriterLock(tmp_path / "crawler.db").acquire()
    try:
        require_writer_lock()
    finally:
        lock.release()


def test_second_lock_same_process_is_rejected(tmp_path):
    first = RuntimeWriterLock(tmp_path / "crawler.db").acquire()
    try:
        with pytest.raises(RuntimeWriterLockError, match="Another ScanHound instance"):
            RuntimeWriterLock(tmp_path / "crawler.db").acquire()
    finally:
        first.release()


def test_second_process_is_rejected(tmp_path):
    db_path = str(tmp_path / "crawler.db")
    first = RuntimeWriterLock(db_path).acquire()
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "from backend.runtime_lock import RuntimeWriterLock, RuntimeWriterLockError\n"
                    "try:\n"
                    "    RuntimeWriterLock(sys.argv[1]).acquire()\n"
                    "except RuntimeWriterLockError as exc:\n"
                    "    print(str(exc))\n"
                    "    raise SystemExit(23)\n"
                    "raise SystemExit(0)\n"
                ),
                db_path,
            ],
            cwd=os.getcwd(),
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 23
        assert "Another ScanHound instance" in proc.stdout
    finally:
        first.release()


def test_process_death_releases_kernel_lock(tmp_path):
    db_path = str(tmp_path / "crawler.db")
    proc = _python(
        (
            "import sys, time\n"
            "from backend.runtime_lock import RuntimeWriterLock\n"
            "lock = RuntimeWriterLock(sys.argv[1]).acquire()\n"
            "print('READY', flush=True)\n"
            "time.sleep(60)\n"
        ),
        db_path,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        proc.kill()
        proc.wait(timeout=5)

        replacement = RuntimeWriterLock(db_path).acquire()
        replacement.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stale_metadata_is_not_authoritative(tmp_path):
    db_path = str(tmp_path / "crawler.db")
    first = RuntimeWriterLock(db_path, app_version="test").acquire()
    first.release()

    metadata_path = writer_lock_path(db_path)
    assert os.path.isfile(metadata_path)
    with open(metadata_path, "r", encoding="utf-8") as handle:
        assert json.load(handle)["app_version"] == "test"

    second = RuntimeWriterLock(db_path, app_version="next").acquire()
    second.release()


def test_distinct_database_paths_do_not_contend(tmp_path):
    first = RuntimeWriterLock(tmp_path / "one.db").acquire()
    second = RuntimeWriterLock(tmp_path / "two.db").acquire()
    second.release()
    first.release()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliably available")
def test_resolved_aliases_contend(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    db_path = real_dir / "crawler.db"
    db_path.touch()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(real_dir, target_is_directory=True)

    first = RuntimeWriterLock(db_path).acquire()
    try:
        with pytest.raises(RuntimeWriterLockError):
            RuntimeWriterLock(alias_dir / "crawler.db").acquire()
    finally:
        first.release()


def test_app_service_acquires_before_database(monkeypatch, tmp_path):
    events = []

    class FakeLock:
        def __init__(self, path, *, app_version=""):
            events.append(("lock_init", path))
        def acquire(self):
            events.append("lock_acquire")
            return self
        def release(self):
            events.append("lock_release")

    class FakeDB:
        def __init__(self):
            events.append("db_init")
        def get_history_count(self):
            return 0
        def close(self):
            events.append("db_close")

    monkeypatch.setattr(app_service, "CACHE_FILE", str(tmp_path / "crawler.db"))
    monkeypatch.setattr(app_service, "RuntimeWriterLock", FakeLock)
    monkeypatch.setattr(app_service, "DatabaseManager", FakeDB)
    monkeypatch.setattr(app_service.AppService, "load_config", lambda self: {})
    monkeypatch.setattr(app_service, "setup_logging", lambda **kwargs: app_service.logger)
    monkeypatch.setattr(app_service.AppService, "_migrate_legacy_persistence", lambda self: (0, 0))
    monkeypatch.setattr(app_service, "PlexManager", lambda: object())
    monkeypatch.setattr(app_service.AppService, "_init_optional_subsystems", lambda self: None)

    service = app_service.AppService()
    assert service.startup() == []
    assert events.index("lock_acquire") < events.index("db_init")
    service.shutdown()
    assert events.index("db_close") < events.index("lock_release")


def test_lock_failure_prevents_database_initialization(monkeypatch, tmp_path):
    calls = []

    class FailingLock:
        def __init__(self, path, *, app_version=""):
            pass
        def acquire(self):
            raise RuntimeWriterLockError()

    class UnexpectedDB:
        def __init__(self):
            calls.append("db")

    monkeypatch.setattr(app_service, "CACHE_FILE", str(tmp_path / "crawler.db"))
    monkeypatch.setattr(app_service, "RuntimeWriterLock", FailingLock)
    monkeypatch.setattr(app_service, "DatabaseManager", UnexpectedDB)
    monkeypatch.setattr(app_service.AppService, "load_config", lambda self: {})
    monkeypatch.setattr(app_service, "setup_logging", lambda **kwargs: app_service.logger)

    service = app_service.AppService()
    with pytest.raises(RuntimeWriterLockError):
        service.startup()
    assert calls == []
