"""Process-lifetime single-writer protection for ScanHound state.

The lock file is persistent metadata only. Ownership is determined exclusively by
an OS-backed advisory lock held on an open file descriptor, so process death
releases ownership automatically and stale PID text is never authoritative.
"""
from __future__ import annotations

import contextlib
import datetime as _datetime
import json
import os
import platform
import socket
import threading
from pathlib import Path
from typing import BinaryIO, Iterator, Optional


class RuntimeWriterLockError(RuntimeError):
    """Raised when shared ScanHound state cannot be mutated safely."""

    PUBLIC_MESSAGE = (
        "Another ScanHound instance is already using this data directory. "
        "Close it before starting a second writer."
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        lock_path: str | None = None,
        owner: Optional[dict] = None,
    ) -> None:
        super().__init__(message or self.PUBLIC_MESSAGE)
        self.lock_path = lock_path
        self.owner = owner or {}


_STATE_LOCK = threading.RLock()
_ACTIVE_LOCKS: dict[str, "RuntimeWriterLock"] = {}
_TEST_BYPASS_DEPTH = 0


def normalize_database_path(db_path: str | os.PathLike[str]) -> str:
    """Return one canonical identity for aliases of the same database path."""
    raw = os.path.abspath(os.fspath(db_path))
    return os.path.normcase(os.path.realpath(raw))


def writer_lock_path(db_path: str | os.PathLike[str]) -> str:
    return normalize_database_path(db_path) + ".writer.lock"


def _read_owner(handle: BinaryIO) -> dict:
    try:
        handle.seek(0)
        raw = handle.read(16 * 1024)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _lock_posix(handle: BinaryIO) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_posix(handle: BinaryIO) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_windows(handle: BinaryIO) -> None:
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_windows(handle: BinaryIO) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class RuntimeWriterLock:
    """Exclusive writer ownership for one resolved ScanHound database path."""

    def __init__(self, db_path: str | os.PathLike[str], *, app_version: str = "") -> None:
        self.db_path = normalize_database_path(db_path)
        self.lock_path = writer_lock_path(self.db_path)
        self.app_version = app_version
        self._handle: Optional[BinaryIO] = None
        self._owner_pid: Optional[int] = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None and self._owner_pid == os.getpid()

    def acquire(self) -> "RuntimeWriterLock":
        if self.acquired:
            return self

        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")

        with _STATE_LOCK:
            existing = _ACTIVE_LOCKS.get(self.db_path)
            if existing is not None and existing.acquired and existing is not self:
                handle.close()
                raise RuntimeWriterLockError(
                    lock_path=self.lock_path,
                    owner=existing.owner_metadata(),
                )

            try:
                if os.name == "nt":
                    _lock_windows(handle)
                else:
                    _lock_posix(handle)
            except (BlockingIOError, OSError) as exc:
                owner = _read_owner(handle)
                handle.close()
                raise RuntimeWriterLockError(
                    lock_path=self.lock_path,
                    owner=owner,
                ) from exc

            self._handle = handle
            self._owner_pid = os.getpid()
            _ACTIVE_LOCKS[self.db_path] = self

            try:
                metadata = self.owner_metadata()
                encoded = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8")
                handle.seek(0)
                handle.truncate()
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                self.release()
                raise
            return self

    def owner_metadata(self) -> dict:
        return {
            "pid": self._owner_pid or os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": _datetime.datetime.now(
                _datetime.timezone.utc
            ).isoformat(),
            "database_path": self.db_path,
            "app_version": self.app_version,
            "platform": platform.platform(),
        }

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        with _STATE_LOCK:
            try:
                if os.name == "nt":
                    _unlock_windows(handle)
                else:
                    _unlock_posix(handle)
            finally:
                try:
                    handle.close()
                finally:
                    if _ACTIVE_LOCKS.get(self.db_path) is self:
                        _ACTIVE_LOCKS.pop(self.db_path, None)
                    self._handle = None
                    self._owner_pid = None

    def __enter__(self) -> "RuntimeWriterLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def is_writer_lock_held() -> bool:
    with _STATE_LOCK:
        return any(lock.acquired for lock in _ACTIVE_LOCKS.values())


def require_writer_lock() -> None:
    """Refuse any shared-state mutation outside the process lifetime lock."""
    with _STATE_LOCK:
        if _TEST_BYPASS_DEPTH > 0:
            return
        if any(lock.acquired for lock in _ACTIVE_LOCKS.values()):
            return
    raise RuntimeWriterLockError(
        "ScanHound writer lock is not held; refusing filesystem mutation."
    )


@contextlib.contextmanager
def _unlocked_fileops_for_tests() -> Iterator[None]:
    """Narrow compatibility helper for direct fileops unit tests only."""
    global _TEST_BYPASS_DEPTH
    with _STATE_LOCK:
        _TEST_BYPASS_DEPTH += 1
    try:
        yield
    finally:
        with _STATE_LOCK:
            _TEST_BYPASS_DEPTH -= 1
