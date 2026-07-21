"""Safe file placement (move/copy/hardlink/symlink) + reversible undo.

Ported/adapted from Nomen's ``file_manager_io._move_file``, decoupled from its
app/logger/progress machinery. Each placement is collision-safe (never
overwrites) and verifiable, and records enough to be undone.

Deletion safety (owner mandate: no accidental file deletion; deletions must
go through a user's input first):
  - Guard 1: automatic (unattended) applies never consume the source — a
    configured 'move' is forced down to 'hardlink' (falling back to 'copy'
    if hardlink itself isn't possible, e.g. cross-device).
  - Guard 2/3: even a user-initiated cross-device 'move' does not hard-delete
    the source by default — the verified-copied source is sent to a
    timestamped trash folder instead of ``os.remove``. Only an explicit
    ``deletions_require_confirmation=False`` opt-out restores the old
    hard-delete behavior.
"""
from __future__ import annotations

import ctypes
import datetime
import errno
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import uuid
from typing import Callable, Optional

from backend.config import _DATA_DIR
from backend.runtime_lock import require_writer_lock

logger = logging.getLogger(__name__)

MOVE_METHODS = ("move", "copy", "hardlink", "symlink")

# Trash root — overridable by tests via monkeypatch.
_TRASH_ROOT = os.path.join(_DATA_DIR, "trash")
# Persistent discovery index for non-mount-root trash locations chosen
# when an unprivileged process cannot create <mount>/.scanhound-trash.
_TRASH_ROOTS_INDEX = os.path.join(_DATA_DIR, "trash_roots.json")
_TRASH_ROOTS_INDEX_LOCK = threading.RLock()
# Same-process safety net when persistence is temporarily unavailable.
_TRASH_ROOTS_RUNTIME = set()
# Serialize every manifest reservation/update.  The lock is process-wide
# because a timestamp bucket may be shared by unrelated worker threads.
_TRASH_MANIFEST_LOCK = threading.RLock()

# Streaming-copy chunk size. Big enough to keep USB HDD sequential throughput
# high; small enough for smooth progress reporting.
_COPY_CHUNK = 8 * 1024 * 1024


def _hash_file(path: str, *, cold: bool = False) -> str:
    """blake2b digest of a file's bytes.

    ``cold=True`` drops the file's page cache first (fsync + POSIX_FADV_DONTNEED)
    so the hash reads from the physical device instead of the write-back cache.
    This is what makes the post-copy verify catch a *latent bad disk write* — a
    plain read-back right after writing would be served the still-correct bytes
    from RAM and miss on-disk corruption. No-op on platforms without
    posix_fadvise (e.g. the Windows desktop build), where it degrades to a
    normal cached hash."""
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        if cold:
            try:
                os.fsync(f.fileno())
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()



class UnsupportedFilesystemSafetyError(OSError):
    """The filesystem cannot provide the safety guarantee this operation needs."""

    PUBLIC_MESSAGE = (
        "This filesystem cannot safely complete the requested file operation. "
        "The source was kept unchanged."
    )

    def __init__(self, operation: str, path: str, *, reason: str):
        super().__init__(errno.ENOTSUP, self.PUBLIC_MESSAGE, path)
        self.operation = operation
        self.path = path
        self.reason = reason


_UNSUPPORTED_DIRSYNC_ERRNOS = {
    errno.ENOSYS,
    errno.EINVAL,
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    getattr(errno, "ENOTSUP", errno.EINVAL),
}


def _windows_move_no_replace(src: str, dst: str) -> None:
    """No-replace Windows move with write-through durability semantics."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    move_file_ex.restype = ctypes.c_int

    movefile_write_through = 0x00000008
    if move_file_ex(src, dst, movefile_write_through):
        return

    err = ctypes.get_last_error()
    if err in (80, 183):
        raise FileExistsError(errno.EEXIST, f"Destination already exists: {dst}", dst)
    if err == 17:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), dst)
    raise OSError(err, ctypes.FormatError(err), dst)


def _fsync_directory(path: str) -> None:
    """Persist directory-entry changes or report the guarantee unavailable.

    Windows source-consuming moves use MoveFileExW(MOVEFILE_WRITE_THROUGH), so
    there is no separate directory-fsync primitive to invoke here.
    """
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_directory_durability(path: str, *, operation: str) -> None:
    """Preflight durability before a source-consuming operation starts."""
    try:
        _fsync_directory(path)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRSYNC_ERRNOS:
            raise UnsupportedFilesystemSafetyError(
                operation,
                path,
                reason=f"directory fsync unavailable (errno={exc.errno})",
            ) from exc
        raise


def _sync_directories(paths) -> None:
    seen = set()
    for path in paths:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        _fsync_directory(path)


def _move_no_replace_durable(src: str, dst: str) -> None:
    """Move without replacement and persist the directory-entry transition."""
    src_dir = os.path.dirname(src) or os.curdir
    dst_dir = os.path.dirname(dst) or os.curdir
    directories = [dst_dir, src_dir]

    if os.name != "nt":
        for directory in directories:
            _require_directory_durability(directory, operation="move")

    _move_no_replace(src, dst)
    try:
        _sync_directories(directories)
    except BaseException:
        try:
            _move_no_replace(dst, src)
            try:
                _sync_directories(directories)
            except OSError:
                logger.critical(
                    "Move rollback restored the source name but directory sync "
                    "still failed: %s <- %s",
                    src,
                    dst,
                    exc_info=True,
                )
        except BaseException:
            logger.critical(
                "Durable move failed after publication and rollback also failed: "
                "%s -> %s",
                src,
                dst,
                exc_info=True,
            )
        raise


def filesystem_safety_status(path: str) -> dict:
    """Return a diagnostic capability snapshot without mutating user files."""
    directory = path if os.path.isdir(path) else (os.path.dirname(path) or os.curdir)
    if os.name == "nt":
        return {
            "no_replace": True,
            "source_consuming_move_durability": "movefile_write_through",
            "directory_fsync": False,
        }
    try:
        _require_directory_durability(directory, operation="probe")
    except UnsupportedFilesystemSafetyError as exc:
        return {
            "no_replace": "renameat2_or_hardlink",
            "source_consuming_move_durability": False,
            "directory_fsync": False,
            "reason": exc.reason,
        }
    return {
        "no_replace": "renameat2_or_hardlink",
        "source_consuming_move_durability": True,
        "directory_fsync": True,
    }


def _linux_rename_noreplace(src: str, dst: str) -> bool:
    """Atomically rename without replacement through Linux renameat2.

    Returns False only when the running libc/filesystem cannot provide the
    primitive, allowing the caller to use the hard-link fallback. Every other
    error, including EEXIST and EXDEV, is propagated.
    """
    if not sys.platform.startswith("linux"):
        return False

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False

    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int

    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(src),
        at_fdcwd,
        os.fsencode(dst),
        rename_noreplace,
    )
    if result == 0:
        return True

    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(
            errno.EEXIST,
            f"Destination already exists: {dst}",
            dst,
        )

    unsupported = {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        getattr(errno, "ENOTSUP", errno.EINVAL),
    }
    if err in unsupported:
        return False
    raise OSError(err, os.strerror(err), dst)


def _move_no_replace(src: str, dst: str) -> None:
    """Publish/move one regular file at an absent destination atomically.

    Windows `os.rename` already refuses an existing destination. Linux uses
    `renameat2(RENAME_NOREPLACE)` when available. Other POSIX systems, and
    Linux filesystems without renameat2 support, use an atomic hard-link
    creation followed by source unlink.

    The function never falls back to overwrite-capable `os.rename` or
    `os.replace`. Unsupported filesystems fail safely with the source intact.
    """
    if os.name == "nt":
        _windows_move_no_replace(src, dst)
        return

    if _linux_rename_noreplace(src, dst):
        return

    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
        raise OSError(
            exc.errno,
            "Destination filesystem cannot atomically publish without "
            "replacement; source kept",
            dst,
        ) from exc

    try:
        os.unlink(src)
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            logger.critical(
                "Atomic no-replace publication left both names after source "
                "unlink and rollback failed: %s -> %s",
                src,
                dst,
                exc_info=True,
            )
        raise



def _copy_verify_atomic(
    src: str,
    dst: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Crash-safe verified copy with atomic no-replace publication.

    Bytes are written to a unique temporary file on the destination volume,
    fsynced, metadata-copied, and hash-verified. `_move_no_replace` then
    publishes the verified inode only if `dst` is still absent. A competing
    writer therefore wins with its bytes intact; this operation raises
    FileExistsError and keeps the source.
    """
    directory = os.path.dirname(dst) or os.curdir
    prefix = f".{os.path.basename(dst)}.part."
    fd, part = tempfile.mkstemp(prefix=prefix, dir=directory)
    os.close(fd)

    total = os.path.getsize(src) or 0
    src_h = hashlib.blake2b()
    done = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                chunk = fi.read(_COPY_CHUNK)
                if not chunk:
                    break
                fo.write(chunk)
                src_h.update(chunk)
                done += len(chunk)
                if progress_cb:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass
            fo.flush()
            os.fsync(fo.fileno())

        shutil.copystat(src, part)
        if src_h.hexdigest() != _hash_file(part, cold=True):
            raise OSError(
                "Copy verification failed (hash mismatch — possible "
                "disk/transfer corruption; source kept)"
            )

        _move_no_replace(part, dst)
        try:
            _fsync_directory(directory)
        except BaseException:
            try:
                if os.path.lexists(dst):
                    os.unlink(dst)
            except OSError:
                logger.critical(
                    "Copy publication was not durably synced and cleanup "
                    "failed: %s",
                    dst,
                    exc_info=True,
                )
            raise
    except BaseException:
        try:
            if os.path.lexists(part):
                os.remove(part)
        except Exception:
            pass
        raise

def _trash_bucket_name() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _casefold_lexists(path: str) -> bool:
    """Return whether a directory entry collides with ``path`` by casefold.

    Keep-both output may move between Linux containers, Windows volumes, and
    NAS mounts with different case semantics. Treat case-only differences as a
    collision everywhere so a name chosen on one filesystem cannot overwrite
    or alias another file when used on a case-insensitive destination later.
    ``os.scandir`` also sees broken symlinks, matching ``lexists`` semantics.
    """
    directory = os.path.dirname(path) or os.curdir
    target = os.path.basename(path).casefold()
    try:
        with os.scandir(directory) as entries:
            return any(entry.name.casefold() == target for entry in entries)
    except OSError:
        # Preserve the old best-effort behavior when the directory cannot be
        # enumerated (permissions, transient mount failure, missing parent).
        return os.path.lexists(path)


def dedupe_dest(dst: str) -> str:
    """Return a cross-platform collision-free Keep-both destination.

    Exact and case-only filename matches collide on every filesystem. The next
    available ``"{base} ({n}){ext}"`` name preserves the requested spelling and
    extension while remaining safe if the file later crosses onto a
    case-insensitive volume.
    """
    if not _casefold_lexists(dst):
        return dst
    directory = os.path.dirname(dst)
    base, ext = os.path.splitext(os.path.basename(dst))
    n = 1
    while True:
        candidate = os.path.join(directory, f"{base} ({n}){ext}")
        if not _casefold_lexists(candidate):
            return candidate
        n += 1


def _fsync_directory(path: str) -> None:
    """Durably publish directory-entry changes on POSIX.

    Windows does not provide a portable directory-fsync equivalent through
    Python; file flush + atomic replacement remain the strongest available
    primitive there.  On POSIX an unsupported directory fsync is a hard error:
    trashing fails closed and the source stays in place.
    """
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path or os.curdir, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: str, payload) -> None:
    """Write JSON through a unique fsynced temp file and atomic replacement."""
    parent = os.path.dirname(path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.tmp.", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        _fsync_directory(parent)
    finally:
        if tmp_path and os.path.lexists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _load_manifest_strict(bucket: str) -> list:
    """Read a manifest without silently discarding corruption or I/O errors."""
    manifest_path = os.path.join(bucket, "manifest.json")
    if not os.path.lexists(manifest_path):
        return []
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            records = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise OSError(
            f"Trash manifest is unreadable or corrupt: {manifest_path}"
        ) from exc
    if not isinstance(records, list):
        raise OSError(f"Trash manifest is not a list: {manifest_path}")
    return records


def _choose_reserved_trash_name(bucket: str, requested: str, records: list) -> str:
    """Choose a case-insensitive name not occupied on disk or in reservations."""
    reserved = {
        str(record.get("trashed_name") or "").casefold()
        for record in records
        if record.get("trashed_name")
    }
    base, ext = os.path.splitext(requested)
    candidate = requested
    suffix = 0
    while candidate.casefold() in reserved or _casefold_lexists(
        os.path.join(bucket, candidate)
    ):
        suffix += 1
        candidate = f"{base} ({suffix}){ext}"
    return candidate


def _reserve_trash_record(bucket: str, requested_name: str, original_path: str):
    """Durably reserve the final trash name before the source can move."""
    with _TRASH_MANIFEST_LOCK:
        records = _load_manifest_strict(bucket)
        trashed_name = _choose_reserved_trash_name(bucket, requested_name, records)
        reservation_id = uuid.uuid4().hex
        records.append({
            "reservation_id": reservation_id,
            "trashed_name": trashed_name,
            "original_path": os.path.abspath(original_path),
            "trashed_at": datetime.datetime.now().isoformat(),
        })
        _atomic_write_json(os.path.join(bucket, "manifest.json"), records)
    return os.path.join(bucket, trashed_name), reservation_id


def _remove_reserved_trash_record(bucket: str, reservation_id: str) -> None:
    """Best-effort rollback of metadata prepared for a move that did not occur."""
    try:
        with _TRASH_MANIFEST_LOCK:
            records = _load_manifest_strict(bucket)
            remaining = [
                record for record in records
                if record.get("reservation_id") != reservation_id
            ]
            if remaining != records:
                _atomic_write_json(os.path.join(bucket, "manifest.json"), remaining)
    except OSError:
        # A metadata-only record with no file is harmless: list/restore enumerate
        # actual files.  Log loudly, but never hide the original move failure.
        logger.exception(
            "Failed to roll back prepared trash record %s in %s",
            reservation_id,
            bucket,
        )

def _normalize_registered_trash_root(root) -> Optional[str]:
    """Validate one persisted trash-root path before it can be scanned.

    Only absolute paths whose final component is exactly `.scanhound-trash`
    are accepted. Existing symlinks are rejected so a later scan cannot be
    redirected outside the intended trash directory.
    """
    try:
        candidate = os.path.abspath(os.fspath(root))
    except (TypeError, ValueError, OSError):
        return None
    if os.path.basename(os.path.normpath(candidate)).casefold() != ".scanhound-trash":
        return None
    try:
        if os.path.lexists(candidate) and os.path.islink(candidate):
            return None
    except OSError:
        return None
    return candidate


def _read_persisted_trash_roots_unlocked() -> list:
    """Read and validate the index while the caller holds the registry lock."""
    try:
        with open(_TRASH_ROOTS_INDEX, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError):
        return []

    raw_roots = payload.get("roots", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_roots, list):
        return []

    roots = []
    seen = set()
    for raw in raw_roots:
        normalized = _normalize_registered_trash_root(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            roots.append(normalized)
    return roots


def _load_registered_trash_roots() -> list:
    """Return persisted roots plus roots registered in this process."""
    with _TRASH_ROOTS_INDEX_LOCK:
        roots = set(_read_persisted_trash_roots_unlocked())
        roots.update(
            root for root in _TRASH_ROOTS_RUNTIME
            if _normalize_registered_trash_root(root) is not None
        )
    return sorted(roots)


def _read_trash_root_index_strict_unlocked() -> list:
    """Read the discovery index without replacing a corrupt index with emptiness."""
    if not os.path.lexists(_TRASH_ROOTS_INDEX):
        return []
    try:
        with open(_TRASH_ROOTS_INDEX, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise OSError(
            f"Trash-root discovery index is unreadable: {_TRASH_ROOTS_INDEX}"
        ) from exc
    raw_roots = payload.get("roots", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_roots, list):
        raise OSError("Trash-root discovery index does not contain a roots list")
    roots = []
    seen = set()
    for raw in raw_roots:
        normalized = _normalize_registered_trash_root(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            roots.append(normalized)
    return roots


def _trash_root_is_intrinsically_discoverable(root: str) -> bool:
    """Whether all_trash_roots can rediscover root without the persisted index."""
    candidate = os.path.abspath(root)
    if candidate == os.path.abspath(_TRASH_ROOT):
        return True
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.isdir(drive) and candidate == os.path.abspath(
                _trash_root_for(drive)
            ):
                return True
        return False
    intrinsic = {
        os.path.abspath(os.path.join(mp, ".scanhound-trash"))
        for mp in _posix_mount_points()
    }
    try:
        intrinsic.add(os.path.abspath(_trash_root_for("/")))
    except OSError:
        pass
    return candidate in intrinsic


def _record_trash_root(root: str, *, required: bool = False) -> bool:
    """Persist restart-safe discovery before a file is moved into a deep root.

    For legacy/intrinsic callers, an index failure retains the old same-process
    visibility behavior.  A destructive trash transaction passes required=True
    for a non-intrinsic root; persistence failure then raises before source bytes
    move.
    """
    require_writer_lock()
    normalized = _normalize_registered_trash_root(root)
    if normalized is None:
        if required:
            raise OSError(f"Unsafe trash root cannot be persisted: {root}")
        return False

    with _TRASH_ROOTS_INDEX_LOCK:
        if _trash_root_is_intrinsically_discoverable(normalized):
            _TRASH_ROOTS_RUNTIME.add(normalized)
            return True
        try:
            persisted = set(_read_trash_root_index_strict_unlocked())
            if normalized not in persisted:
                persisted.add(normalized)
                _atomic_write_json(
                    _TRASH_ROOTS_INDEX,
                    {"version": 1, "roots": sorted(persisted)},
                )
            _TRASH_ROOTS_RUNTIME.add(normalized)
            return True
        except OSError:
            if required:
                raise
            _TRASH_ROOTS_RUNTIME.add(normalized)
            logger.warning(
                "Failed to persist trash-root discovery index at %s; "
                "same-process discovery remains available",
                _TRASH_ROOTS_INDEX,
                exc_info=True,
            )
            return False

def _trash_root_for(path: str) -> str:
    """Return the trash root that lives on ``path``'s own volume.

    Disposal must never require a cross-device copy, so the trash bucket is
    sited on the SOURCE's own drive/UNC share (``<anchor>/.scanhound-trash``)
    rather than under the app-data dir — otherwise every cross-device 'move'
    would EXDEV into ``_TRASH_ROOT`` and ``shutil.move`` would silently copy
    the full media file into (often OneDrive-synced) app-data.

    Falls back to the module-level ``_TRASH_ROOT`` only if the source's volume
    can't be determined.

    Windows uses the drive anchor. POSIX (the Docker deployment) has no drive
    anchor — os.path.splitdrive always returns '' there — so we walk up to the
    source's MOUNT POINT (where st_dev changes vs the parent) and site the trash
    inside it. Each library is its own bind mount, so this keeps disposal an
    instant same-device rename instead of an EXDEV copy of the whole media file
    into the app-data /data mount.
    """
    p = os.path.abspath(path)
    anchor, _ = os.path.splitdrive(p)
    if anchor:  # Windows: the drive root is the volume root.
        return os.path.join(anchor + os.sep, ".scanhound-trash")
    # POSIX: find the mount point by walking up while st_dev is unchanged.
    try:
        base = p if os.path.exists(p) else os.path.dirname(p)
        dev = os.stat(base).st_dev
        cur = os.path.dirname(p) or "/"
        while True:
            parent = os.path.dirname(cur)
            if parent == cur:  # reached filesystem root
                break
            try:
                if os.stat(parent).st_dev != dev:
                    break  # `cur` is the mount point of the source's volume
            except OSError:
                break
            cur = parent
        return os.path.join(cur, ".scanhound-trash")
    except OSError:
        return _TRASH_ROOT


def _same_volume_trash_roots(path: str) -> list:
    """Return same-device trash roots from volume-level to source-local.

    The preferred root is the drive/mount root returned by
    :func:`_trash_root_for`. An unprivileged process may be unable to create a
    hidden directory there even though it can modify files deeper in the
    library. In that case, progressively deeper ancestors provide a writable
    same-device fallback before app-data is considered.
    """
    primary = _trash_root_for(path)
    if primary == _TRASH_ROOT:
        return []

    p = os.path.abspath(path)
    source_dir = p if os.path.isdir(p) else os.path.dirname(p)
    source_dir = source_dir or os.curdir
    try:
        source_dev = os.stat(source_dir).st_dev
    except OSError:
        return [primary]

    volume_dir = os.path.dirname(primary)
    discovered = []
    cur = source_dir
    while True:
        try:
            if os.stat(cur).st_dev != source_dev:
                break
        except OSError:
            break
        discovered.append(os.path.join(cur, ".scanhound-trash"))
        if os.path.normcase(os.path.abspath(cur)) == os.path.normcase(
                os.path.abspath(volume_dir)):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    ordered = [primary]
    for candidate in reversed(discovered):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def trash_roots(path: str) -> list:
    """Trash roots worth checking for a file trashed from ``path``'s volume.

    Include every same-device fallback that :func:`_trash` may choose, followed
    by the app-data last resort, so list/restore/delete operations can find an
    entry regardless of which writable ancestor was available when it moved.
    """
    roots = [*_same_volume_trash_roots(path), _TRASH_ROOT]
    return list(dict.fromkeys(roots))


def _finish_trash_move(path: str, bucket: str, dst: str) -> str:
    """Return a completed move whose discovery and restore record are durable."""
    logger.info("trash  | %s -> %s", path, dst)
    return dst


def _remove_empty_bucket(bucket: str) -> None:
    """Best-effort cleanup for a candidate whose move did not complete."""
    try:
        leftovers = os.listdir(bucket)
        if leftovers == ["manifest.json"]:
            manifest = _load_manifest(bucket)
            if not manifest:
                os.unlink(os.path.join(bucket, "manifest.json"))
                leftovers = []
        if not leftovers:
            os.rmdir(bucket)
    except OSError:
        pass


def _prepare_trash_destination(root: str, bucket_name: str, path: str):
    """Create durable discovery + restore metadata before consuming path."""
    os.makedirs(root, exist_ok=True)
    _fsync_directory(os.path.dirname(root) or os.curdir)
    required = not _trash_root_is_intrinsically_discoverable(root)
    _record_trash_root(root, required=required)

    bucket = os.path.join(root, bucket_name)
    os.makedirs(bucket, exist_ok=True)
    _fsync_directory(root)
    dst, reservation_id = _reserve_trash_record(
        bucket, os.path.basename(path), path
    )
    return bucket, dst, reservation_id


def _cleanup_prepared_trash(bucket: str, dst: str, reservation_id: str) -> None:
    """Rollback a transaction that did not consume the source."""
    try:
        if os.path.lexists(dst):
            os.unlink(dst)
    except OSError:
        logger.exception("Failed to remove incomplete trash destination %s", dst)
    _remove_reserved_trash_record(bucket, reservation_id)
    _remove_empty_bucket(bucket)


def _copy_then_unlink_to_trash(path: str, dst: str) -> None:
    """Cross-device verified copy; source unlink is the final destructive step."""
    _copy_verify_atomic(path, dst)
    try:
        os.unlink(path)
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            logger.critical(
                "Trash copy published but source unlink and destination rollback "
                "both failed: %s -> %s",
                path,
                dst,
                exc_info=True,
            )
        raise


def _trash(path: str) -> str:
    """Move path into recoverable trash with a durable pre-move restore record.

    No source-consuming operation begins until both restart-safe root discovery
    (when needed) and the manifest reservation are atomically persisted.  Move,
    manifest, fsync, or index failures therefore leave the source untouched.
    """
    require_writer_lock()
    bucket_name = _trash_bucket_name()
    first_exdev_root = None
    preparation_errors = []

    for root in _same_volume_trash_roots(path):
        try:
            bucket, dst, reservation_id = _prepare_trash_destination(
                root, bucket_name, path
            )
        except OSError as exc:
            preparation_errors.append(exc)
            continue
        try:
            _move_no_replace(path, dst)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                if first_exdev_root is None:
                    first_exdev_root = root
                _cleanup_prepared_trash(bucket, dst, reservation_id)
                continue
            _cleanup_prepared_trash(bucket, dst, reservation_id)
            raise
        except BaseException:
            _cleanup_prepared_trash(bucket, dst, reservation_id)
            raise
        return _finish_trash_move(path, bucket, dst)

    if first_exdev_root is not None:
        bucket, dst, reservation_id = _prepare_trash_destination(
            first_exdev_root, bucket_name, path
        )
        try:
            logger.warning(
                "Atomic trash move crossed a mount boundary for %s; using "
                "verified copy + unlink at %s",
                path,
                first_exdev_root,
            )
            _copy_then_unlink_to_trash(path, dst)
        except BaseException:
            _cleanup_prepared_trash(bucket, dst, reservation_id)
            raise
        return _finish_trash_move(path, bucket, dst)

    try:
        bucket, dst, reservation_id = _prepare_trash_destination(
            _TRASH_ROOT, bucket_name, path
        )
    except OSError:
        if preparation_errors:
            raise OSError(
                "No trash destination could durably prepare a restore record; "
                "source kept"
            ) from preparation_errors[-1]
        raise

    try:
        _move_no_replace(path, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            _cleanup_prepared_trash(bucket, dst, reservation_id)
            raise
        try:
            _copy_then_unlink_to_trash(path, dst)
        except BaseException:
            _cleanup_prepared_trash(bucket, dst, reservation_id)
            raise
    except BaseException:
        _cleanup_prepared_trash(bucket, dst, reservation_id)
        raise
    return _finish_trash_move(path, bucket, dst)

def _is_safe_component(component: str) -> bool:
    """Whether a single path component is safe to join under a trash root.

    Rejects empty strings, path separators, and any ``..`` traversal segment
    so a bucket/name supplied over the API can never escape the trash root.
    """
    if not component:
        return False
    if os.sep in component or (os.altsep and os.altsep in component):
        return False
    if component in ("..", "."):
        return False
    return True


def _load_manifest(bucket_path: str) -> list:
    """Read a bucket's manifest.json; returns [] if missing/unreadable."""
    manifest_path = os.path.join(bucket_path, "manifest.json")
    if not os.path.isfile(manifest_path):
        return []
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except (OSError, ValueError):
        return []


def _save_manifest(bucket_path: str, records: list) -> None:
    """Atomically and durably replace a bucket manifest."""
    require_writer_lock()
    with _TRASH_MANIFEST_LOCK:
        _atomic_write_json(os.path.join(bucket_path, "manifest.json"), records)

def list_trash_entries(roots) -> list:
    """List every trashed file across the given trash roots.

    Each entry: ``{bucket, name, size, trashed_at, original_path, restorable}``.
    ``original_path`` is ``None`` (and ``restorable`` False) when the bucket's
    manifest has no record for that file (e.g. it predates the manifest
    feature, or the manifest itself failed to write). Missing/unreadable
    roots are skipped silently — trash may simply not exist yet.
    """
    entries = []
    seen_roots = set()
    for root in roots:
        root = os.path.abspath(root)
        if root in seen_roots or not os.path.isdir(root):
            continue
        seen_roots.add(root)
        try:
            bucket_names = sorted(os.listdir(root))
        except OSError:
            continue
        for bucket in bucket_names:
            bucket_path = os.path.join(root, bucket)
            if not os.path.isdir(bucket_path):
                continue
            manifest = _load_manifest(bucket_path)
            manifest_by_name = {r.get("trashed_name"): r for r in manifest if r.get("trashed_name")}
            try:
                names = sorted(os.listdir(bucket_path))
            except OSError:
                continue
            for name in names:
                if name == "manifest.json":
                    continue
                fpath = os.path.join(bucket_path, name)
                if not os.path.isfile(fpath):
                    continue
                rec = manifest_by_name.get(name)
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    size = 0
                entries.append({
                    "bucket": bucket,
                    "name": name,
                    "size": size,
                    "trashed_at": rec.get("trashed_at") if rec else None,
                    "original_path": rec.get("original_path") if rec else None,
                    "restorable": bool(rec and rec.get("original_path") and not rec.get("pending_operation")),
                    "transaction_state": rec.get("pending_operation") if rec else None,
                    "repair_required": bool(rec and rec.get("operation_error")),
                })
    return entries



_OPERATION_FIELDS = (
    "pending_operation",
    "operation_id",
    "operation_started_at",
    "operation_synthetic",
    "operation_error",
)


def _clear_operation_fields(record: dict) -> dict:
    cleaned = dict(record)
    for field in _OPERATION_FIELDS:
        cleaned.pop(field, None)
    return cleaned


def _begin_trash_operation(
    bucket_path: str,
    name: str,
    operation: str,
    *,
    original_path: Optional[str] = None,
) -> tuple[str, dict]:
    """Persist one restore/delete intent before changing bytes."""
    if operation not in ("restore", "delete"):
        raise ValueError(f"Unsupported trash operation: {operation}")

    with _TRASH_MANIFEST_LOCK:
        records = _load_manifest_strict(bucket_path)
        rec = next((r for r in records if r.get("trashed_name") == name), None)
        synthetic = rec is None
        if rec is None:
            if operation != "delete":
                raise OSError("No manifest record for this entry")
            rec = {
                "reservation_id": uuid.uuid4().hex,
                "trashed_name": name,
                "original_path": original_path,
                "trashed_at": datetime.datetime.now().isoformat(),
            }
            records.append(rec)

        if rec.get("pending_operation"):
            raise OSError(
                f"Trash entry already has an incomplete "
                f"{rec.get('pending_operation')} operation"
            )

        operation_id = uuid.uuid4().hex
        updated = dict(rec)
        updated.update({
            "pending_operation": operation,
            "operation_id": operation_id,
            "operation_started_at": datetime.datetime.now().isoformat(),
            "operation_synthetic": synthetic,
        })
        index = records.index(rec)
        records[index] = updated
        _atomic_write_json(os.path.join(bucket_path, "manifest.json"), records)
        return operation_id, updated


def _clear_trash_operation(bucket_path: str, operation_id: str) -> None:
    """Roll back an intent when the byte operation did not occur."""
    with _TRASH_MANIFEST_LOCK:
        records = _load_manifest_strict(bucket_path)
        changed = False
        remaining = []
        for record in records:
            if record.get("operation_id") != operation_id:
                remaining.append(record)
                continue
            changed = True
            if record.get("operation_synthetic"):
                continue
            remaining.append(_clear_operation_fields(record))
        if changed:
            _atomic_write_json(os.path.join(bucket_path, "manifest.json"), remaining)


def _complete_trash_operation(bucket_path: str, operation_id: str) -> None:
    """Durably remove the record after restore/delete bytes are complete."""
    with _TRASH_MANIFEST_LOCK:
        records = _load_manifest_strict(bucket_path)
        remaining = [
            record for record in records
            if record.get("operation_id") != operation_id
        ]
        if remaining == records:
            raise OSError(f"Trash operation record not found: {operation_id}")
        _atomic_write_json(os.path.join(bucket_path, "manifest.json"), remaining)


def _restore_no_replace(fpath: str, original_path: str) -> None:
    """Restore bytes without replacement, including a verified EXDEV fallback."""
    try:
        _move_no_replace(fpath, original_path)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    _copy_verify_atomic(fpath, original_path)
    try:
        os.unlink(fpath)
    except BaseException:
        try:
            if os.path.lexists(original_path):
                os.unlink(original_path)
        except OSError:
            logger.critical(
                "Restore copy published but trash unlink and destination rollback "
                "both failed: %s -> %s",
                fpath,
                original_path,
                exc_info=True,
            )
        raise


def repair_trash_transactions(roots=None) -> dict:
    """Reconcile interrupted restore/delete intents without guessing.

    Safe states:
      restore intent + trash exists + destination absent -> byte move never
      occurred, so clear the intent.
      restore intent + trash absent + destination exists -> restore completed,
      so remove the manifest record.
      delete intent + trash exists -> unlink never occurred, so clear/remove the
      intent.
      delete intent + trash absent -> deletion completed, so remove the record.

    Ambiguous both/neither restore states remain visible for manual repair.
    """
    require_writer_lock()
    if roots is None:
        roots = all_trash_roots()

    intents_seen = 0
    completed = 0
    rolled_back = 0
    repair_required = 0
    errors = []

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        try:
            bucket_names = sorted(os.listdir(root))
        except OSError:
            continue
        for bucket_name in bucket_names:
            bucket_path = os.path.join(root, bucket_name)
            if os.path.islink(bucket_path) or not os.path.isdir(bucket_path):
                continue
            try:
                with _TRASH_MANIFEST_LOCK:
                    records = _load_manifest_strict(bucket_path)
                    changed = False
                    revised = []
                    for record in records:
                        operation = record.get("pending_operation")
                        operation_id = record.get("operation_id")
                        if not operation or not operation_id:
                            revised.append(record)
                            continue

                        intents_seen += 1
                        name = record.get("trashed_name") or ""
                        fpath = os.path.join(bucket_path, name)
                        trash_exists = os.path.lexists(fpath)

                        if operation == "delete":
                            changed = True
                            if trash_exists:
                                if not record.get("operation_synthetic"):
                                    revised.append(_clear_operation_fields(record))
                                rolled_back += 1
                            else:
                                completed += 1
                            continue

                        if operation != "restore":
                            changed = True
                            repair_required += 1
                            flagged = dict(record)
                            flagged["operation_error"] = "unknown operation"
                            revised.append(flagged)
                            continue

                        original_path = record.get("original_path")
                        original_exists = bool(
                            original_path and os.path.lexists(original_path)
                        )
                        if trash_exists and not original_exists:
                            changed = True
                            revised.append(_clear_operation_fields(record))
                            rolled_back += 1
                        elif not trash_exists and original_exists:
                            changed = True
                            completed += 1
                        else:
                            changed = True
                            repair_required += 1
                            state = (
                                "both trash and destination exist"
                                if trash_exists and original_exists
                                else "neither trash nor destination exists"
                            )
                            flagged = dict(record)
                            flagged["operation_error"] = state
                            revised.append(flagged)
                            errors.append({
                                "bucket": bucket_name,
                                "name": name,
                                "operation": operation,
                                "error": state,
                            })
                    if changed:
                        _atomic_write_json(
                            os.path.join(bucket_path, "manifest.json"),
                            revised,
                        )
            except OSError as exc:
                repair_required += 1
                errors.append({
                    "bucket": bucket_name,
                    "error": f"manifest repair failed: {type(exc).__name__}",
                })
                logger.exception("Trash transaction repair failed for %s", bucket_path)
                continue
            _remove_bucket_if_empty(bucket_path)

    return {
        "intents_seen": intents_seen,
        "completed": completed,
        "rolled_back": rolled_back,
        "repair_required": repair_required,
        "errors": errors,
    }

def restore_trash_entry(bucket: str, name: str, roots) -> dict:
    """Restore one manifest-backed entry through a durable intent transaction."""
    require_writer_lock()
    if not _is_safe_component(bucket) or not _is_safe_component(name):
        return {"ok": False, "error": "Invalid bucket or name (path traversal rejected)"}

    for root in roots:
        root = os.path.abspath(root)
        bucket_path = os.path.join(root, bucket)
        if not os.path.isdir(bucket_path):
            continue
        fpath = os.path.join(bucket_path, name)
        if not os.path.isfile(fpath):
            continue

        try:
            manifest = _load_manifest_strict(bucket_path)
        except OSError:
            return {"ok": False, "error": "Trash manifest is unreadable; restore refused"}

        rec = next((r for r in manifest if r.get("trashed_name") == name), None)
        if not rec or not rec.get("original_path"):
            return {
                "ok": False,
                "error": "No manifest record for this entry — cannot restore safely",
            }

        original_path = rec["original_path"]
        if os.path.lexists(original_path):
            return {"ok": False, "error": "Restore destination already exists"}

        try:
            os.makedirs(os.path.dirname(original_path) or ".", exist_ok=True)
            operation_id, _ = _begin_trash_operation(
                bucket_path,
                name,
                "restore",
                original_path=original_path,
            )
        except OSError as exc:
            logger.exception("Could not prepare trash restore intent")
            return {
                "ok": False,
                "error": "Restore could not prepare durable recovery metadata",
                "reason": type(exc).__name__,
            }

        try:
            _restore_no_replace(fpath, original_path)
        except (OSError, FileExistsError) as exc:
            try:
                _clear_trash_operation(bucket_path, operation_id)
            except OSError:
                logger.exception("Could not roll back failed restore intent")
            logger.exception("Trash restore failed")
            return {
                "ok": False,
                "error": (
                    "Restore destination already exists"
                    if isinstance(exc, FileExistsError)
                    else "Restore failed; trashed file was kept"
                ),
                "reason": type(exc).__name__,
            }

        try:
            _complete_trash_operation(bucket_path, operation_id)
        except OSError as exc:
            logger.exception(
                "Restore bytes completed but manifest completion failed; "
                "restart repair is required"
            )
            return {
                "ok": False,
                "error": "Restore completed, but bookkeeping requires repair",
                "repair_required": True,
                "bytes_restored": True,
                "operation_id": operation_id,
                "reason": type(exc).__name__,
            }

        # Keep the empty manifest after successful restore for compatibility
        # with existing restore/history readers. Explicit deletion or retention
        # maintenance may remove the empty bucket later.
        logger.info("restore | %s -> %s", fpath, original_path)
        return {"ok": True, "restored_path": original_path}

    return {"ok": False, "error": "Trash entry not found"}


def delete_trash_entry(bucket: str, name: str, roots) -> dict:
    """Permanently delete one entry through a durable intent transaction."""
    require_writer_lock()
    if not _is_safe_component(bucket) or not _is_safe_component(name):
        return {"ok": False, "error": "Invalid bucket or name (path traversal rejected)"}
    if name == "manifest.json":
        return {"ok": False, "error": "Refusing to delete a bucket manifest"}

    for root in roots:
        root = os.path.abspath(root)
        bucket_path = os.path.join(root, bucket)
        if not os.path.isdir(bucket_path):
            continue
        fpath = os.path.join(bucket_path, name)
        is_link = os.path.islink(fpath)
        if not is_link and not os.path.isfile(fpath):
            continue

        try:
            size = 0 if is_link else os.path.getsize(fpath)
            operation_id, _ = _begin_trash_operation(
                bucket_path,
                name,
                "delete",
            )
        except OSError as exc:
            logger.exception("Could not prepare trash delete intent")
            return {
                "ok": False,
                "error": "Delete could not prepare durable recovery metadata",
                "reason": type(exc).__name__,
            }

        try:
            os.unlink(fpath)
        except OSError as exc:
            try:
                _clear_trash_operation(bucket_path, operation_id)
            except OSError:
                logger.exception("Could not roll back failed delete intent")
            return {
                "ok": False,
                "error": "Delete failed; trashed file was kept",
                "reason": type(exc).__name__,
            }

        try:
            _complete_trash_operation(bucket_path, operation_id)
        except OSError as exc:
            logger.exception(
                "Delete bytes completed but manifest completion failed; "
                "restart repair is required"
            )
            return {
                "ok": False,
                "error": "Delete completed, but bookkeeping requires repair",
                "repair_required": True,
                "file_deleted": True,
                "bytes_freed": size,
                "operation_id": operation_id,
                "reason": type(exc).__name__,
            }

        _remove_bucket_if_empty(bucket_path)
        logger.info("trash delete | %s (%d bytes)", fpath, size)
        return {"ok": True, "bytes_freed": size}

    return {"ok": False, "error": "Trash entry not found"}


def _remove_bucket_if_empty(bucket_path: str) -> None:
    """Remove a bucket only when no bytes and no recovery records remain."""
    require_writer_lock()
    try:
        leftovers = os.listdir(bucket_path)
    except OSError:
        return
    if any(name != "manifest.json" for name in leftovers):
        return

    manifest_path = os.path.join(bucket_path, "manifest.json")
    if os.path.lexists(manifest_path):
        try:
            records = _load_manifest_strict(bucket_path)
        except OSError:
            logger.warning(
                "Refusing to remove bucket with unreadable manifest %s",
                bucket_path,
                exc_info=True,
            )
            return
        if records:
            return

    try:
        if os.path.lexists(manifest_path):
            os.unlink(manifest_path)
        os.rmdir(bucket_path)
    except OSError:
        logger.warning(
            "Failed to remove emptied trash bucket %s (skipped)",
            bucket_path,
            exc_info=True,
        )


def empty_trash(roots=None) -> dict:
    """Permanently delete EVERY trashed file, ignoring the retention period.

    A user-triggered "empty it now" — deliberately implemented as a sweep with
    a negative retention rather than a parallel deletion routine, so it
    inherits the sweep's symlink safety, per-file error tolerance and bucket
    cleanup exactly. Negative rather than 0 so that a bucket whose computed age
    is fractionally negative (clock stepped back between trashing and
    emptying) is still collected — "empty" must leave nothing behind. Same
    return shape as :func:`sweep_trash`.
    """
    require_writer_lock()
    return sweep_trash(-1, roots=roots)


def _posix_mount_points() -> list:
    """Best-effort list of mount points on this POSIX host.

    Reads ``/proc/self/mountinfo`` (field 5 is the mount point); falls back
    to ``/proc/mounts`` (field 2) if that's unavailable/empty. Returns
    ``["/"]`` on any error or if nothing could be parsed, so callers always
    have at least the root to fall back on.
    """
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as f:
            points = []
            for line in f:
                fields = line.split(" ")
                if len(fields) > 4 and fields[4]:
                    points.append(fields[4])
            if points:
                return points
    except OSError:
        pass
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            points = []
            for line in f:
                fields = line.split()
                if len(fields) > 1 and fields[1]:
                    points.append(fields[1])
            if points:
                return points
    except OSError:
        pass
    return ["/"]


def all_trash_roots() -> list:
    """All trash roots worth scanning/sweeping: the app-data fallback root
    (``_TRASH_ROOT``) plus every per-volume ``<volume>/.scanhound-trash`` root.
    On Windows that's a candidate per known drive letter; on POSIX it's a
    candidate per mount point from ``_posix_mount_points`` (``/proc`` mounts) —
    since ``_trash`` sites a bucket on the trashed file's OWN mount, checking
    only ``/`` used to miss trash on separately-mounted library/download
    volumes, so list/restore/sweep couldn't see it.

    Single source of truth shared by the ``/rename/trash`` list/restore
    endpoints and the maintenance-pass retention sweep — a disposal on any
    volume must be reachable (and eventually swept) by both. Scanning every
    drive's candidate root is cheap (a single ``os.path.isdir`` each); a
    per-volume ``.scanhound-trash`` directory only exists if a disposal
    actually created it, so this can't return anything a real trash disposal
    didn't put there.
    """
    roots = {os.path.abspath(_TRASH_ROOT)}
    roots.update(_load_registered_trash_roots())
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.isdir(drive):
                roots.add(_trash_root_for(drive))
    else:
        for mp in _posix_mount_points():
            try:
                roots.add(os.path.join(mp, ".scanhound-trash"))
            except (TypeError, ValueError):
                continue
        roots.add(_trash_root_for("/"))
    return sorted(roots)


def _bucket_age_days(bucket_path: str) -> float:
    """How many days old a trash bucket is.

    Prefers parsing the ``YYYYMMDD-HHMMSS`` bucket name (the authoritative
    "trashed at" moment, immune to filesystem mtime drift/preservation on
    cross-device moves); falls back to the bucket directory's own mtime for
    any bucket name that doesn't match the expected format.
    """
    name = os.path.basename(bucket_path)
    try:
        trashed_at = datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
        age = datetime.datetime.now() - trashed_at
        return age.total_seconds() / 86400.0
    except ValueError:
        try:
            mtime = os.path.getmtime(bucket_path)
        except OSError:
            return 0.0
        return (datetime.datetime.now().timestamp() - mtime) / 86400.0


def sweep_trash(retention_days: int, roots=None) -> dict:
    """Transactionally delete entries from buckets older than retention_days."""
    require_writer_lock()
    if roots is None:
        roots = all_trash_roots()

    files_deleted = 0
    bytes_freed = 0
    buckets_removed = 0
    repair_required = 0
    errors = []

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        try:
            bucket_names = sorted(os.listdir(root))
        except OSError:
            continue

        for bucket_name in bucket_names:
            bucket_path = os.path.join(root, bucket_name)
            if os.path.islink(bucket_path) or not os.path.isdir(bucket_path):
                continue
            if _bucket_age_days(bucket_path) < retention_days:
                continue

            bucket_existed = True
            try:
                names = [
                    entry for entry in os.listdir(bucket_path)
                    if entry != "manifest.json"
                ]
            except OSError:
                continue

            for entry in names:
                epath = os.path.join(bucket_path, entry)
                if not os.path.islink(epath) and not os.path.isfile(epath):
                    logger.warning(
                        "sweep_trash: refusing nested non-file entry %s",
                        epath,
                    )
                    errors.append({
                        "bucket": bucket_name,
                        "name": entry,
                        "error": "nested non-file entry refused",
                    })
                    continue

                result = delete_trash_entry(
                    bucket_name,
                    entry,
                    [root],
                )
                if result.get("ok") or result.get("file_deleted"):
                    files_deleted += 1
                    bytes_freed += int(result.get("bytes_freed") or 0)
                if result.get("repair_required"):
                    repair_required += 1
                if not result.get("ok"):
                    errors.append({
                        "bucket": bucket_name,
                        "name": entry,
                        "error": result.get("error", "delete failed"),
                    })

            _remove_bucket_if_empty(bucket_path)
            if bucket_existed and not os.path.exists(bucket_path):
                buckets_removed += 1

    logger.info(
        "sweep_trash: removed %d file(s), %d bucket(s), freed %d bytes "
        "(retention=%dd, repair_required=%d)",
        files_deleted,
        buckets_removed,
        bytes_freed,
        retention_days,
        repair_required,
    )
    return {
        "files_deleted": files_deleted,
        "bytes_freed": bytes_freed,
        "buckets_removed": buckets_removed,
        "repair_required": repair_required,
        "errors": errors,
    }


def place_file(src: str, dst: str, method: str = "hardlink", *,
               automatic: bool = False,
               deletions_require_confirmation: bool = True,
               progress_cb: Optional[Callable[[int, int], None]] = None) -> str:
    """Place ``src`` at ``dst`` using ``method``; return the method used.

    Collision-safe: refuses to overwrite an existing destination. Verifies
    copies by hash. Raises on failure so the caller can record an error.

    ``automatic`` marks an unattended (no per-item user confirmation)
    placement — e.g. auto-rename with confirmation disabled. When True and
    ``method`` is ``move``, the method is forced down to ``hardlink`` (or
    ``copy`` if hardlinking isn't possible) so the source is never consumed
    without a human in the loop.

    ``deletions_require_confirmation`` (default True) gates the cross-device
    'move' fallback: instead of hard-deleting the verified-copied source with
    ``os.remove``, it is moved to a timestamped trash folder. Pass False to
    restore the old hard-delete behavior (explicit user opt-out in settings).
    """
    require_writer_lock()
    if method not in MOVE_METHODS:
        method = "hardlink"
    if automatic and method == "move":
        method = "hardlink"  # Guard 1: unattended applies never consume the source.
    if not os.path.isfile(src):
        raise FileNotFoundError(f"Source file not found: {src}")
    if os.path.lexists(dst):
        raise FileExistsError(f"Destination already exists: {dst}")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)

    if method == "hardlink":
        try:
            os.link(src, dst)
            try:
                _fsync_directory(os.path.dirname(dst) or os.curdir)
            except BaseException:
                os.unlink(dst)
                raise
            return "hardlink"
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            method = "copy"  # cross-device → fall back to a verified copy

    if method == "symlink":
        os.symlink(os.path.abspath(src), dst)
        try:
            _fsync_directory(os.path.dirname(dst) or os.curdir)
        except BaseException:
            os.unlink(dst)
            raise
        return "symlink"

    if method == "copy":
        _copy_verify_atomic(src, dst, progress_cb)
        return "copy"

    # move: atomic no-replace publication first, else verified copy +
    # dispose source. Never use overwrite-capable rename/replace primitives.
    try:
        _move_no_replace_durable(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy_verify_atomic(src, dst, progress_cb)
        # The copy is fully on disk + verified; only now is it safe to remove
        # the source. A crash before this point loses nothing (source intact,
        # no partial at dst); a crash after leaves a harmless duplicate.
        if deletions_require_confirmation:
            _trash(src)
        else:
            os.remove(src)
    return "move"


def undo_place(src: str, dst: str, method: str) -> None:
    """Reverse a :func:`place_file`: restore ``src``, remove ``dst`` as needed."""
    require_writer_lock()
    if method in ("hardlink", "symlink", "copy"):
        # The original src still exists — just drop the link/copy.
        if os.path.lexists(dst):
            os.remove(dst)
    elif method == "move":
        # src was consumed; move dst back to it.
        if os.path.exists(src):
            raise FileExistsError(f"Original path already occupied: {src}")
        if os.path.isfile(dst):
            os.makedirs(os.path.dirname(src) or ".", exist_ok=True)
            shutil.move(dst, src)
