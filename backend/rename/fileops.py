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

import datetime
import errno
import hashlib
import json
import logging
import os
import shutil
from typing import Callable, Optional

from backend.config import _DATA_DIR

logger = logging.getLogger(__name__)

MOVE_METHODS = ("move", "copy", "hardlink", "symlink")

# Trash root — overridable by tests via monkeypatch.
_TRASH_ROOT = os.path.join(_DATA_DIR, "trash")

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


def _copy_verify_atomic(src: str, dst: str,
                        progress_cb: Optional[Callable[[int, int], None]] = None) -> None:
    """Crash-safe verified copy of ``src`` → ``dst``.

    Streams into a ``dst + '.part'`` sidecar on the *destination* volume, flushes
    to disk (fsync), hash-verifies (blake2b, matching :func:`_hash_file`), then
    atomically renames it into place with :func:`os.replace`. Consequences:

    * A crash/power-loss mid-copy never leaves a partial file at the real
      destination — only a ``.part`` temp, which the next apply truncates and
      reuses. ``dst`` appears only once every byte is on disk and verified.
    * The source is untouched here, so it is always recoverable; the caller
      disposes of it only after this returns (for a 'move').

    ``progress_cb(bytes_done, bytes_total)`` is called as bytes are written
    (best-effort — exceptions from it are swallowed).
    """
    part = dst + ".part"
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
        # Verify from the PHYSICAL disk (cold read), not the write cache, so a
        # latent bad write can't slip through and become the destination.
        if src_h.hexdigest() != _hash_file(part, cold=True):
            raise OSError("Copy verification failed (hash mismatch — "
                          "possible disk/transfer corruption; source kept)")
        os.replace(part, dst)  # atomic on the destination volume
    except BaseException:
        # Never leave a stray .part behind on failure (it would be reused on the
        # next attempt anyway, but clean up eagerly).
        try:
            if os.path.exists(part):
                os.remove(part)
        except Exception:
            pass
        raise


def _trash_bucket_name() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _record_trash_manifest(bucket: str, trashed_name: str, original_path: str) -> None:
    """Append a restore record to ``<bucket>/manifest.json`` (read-modify-write).

    Best-effort: any failure (disk full, permissions, corrupt existing JSON) is
    logged as a warning and swallowed — losing the ability to restore a file
    via the manifest is acceptable, but it must never turn a successful trash
    disposal into a raised exception.
    """
    manifest_path = os.path.join(bucket, "manifest.json")
    try:
        records = []
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    records = json.load(f)
                if not isinstance(records, list):
                    records = []
            except (OSError, ValueError):
                records = []
        records.append({
            "trashed_name": trashed_name,
            "original_path": os.path.abspath(original_path),
            "trashed_at": datetime.datetime.now().isoformat(),
        })
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
    except OSError:
        logger.warning("Failed to update trash manifest at %s (non-fatal)", manifest_path,
                       exc_info=True)


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


def _trash(path: str) -> str:
    """Move ``path`` into ``<source volume>/.scanhound-trash/<YYYYMMDD-HHMMSS>/<name>``.

    Used instead of a hard delete wherever a source file must be disposed of
    after being safely and verifiably placed elsewhere. The trash bucket is
    sited on the source's own volume (see :func:`_trash_root_for`) so
    disposal is an instant same-volume rename — never a byte copy into
    app-data. Handles filename collisions within the same timestamp bucket
    with a numeric suffix, and falls back to the app-data ``_TRASH_ROOT`` +
    ``shutil.move`` only as a last resort (e.g. the source volume's trash
    root can't be created), so ``_trash`` never raises in a way that would
    lose the source.
    """
    root = _trash_root_for(path)
    bucket = os.path.join(root, _trash_bucket_name())
    try:
        os.makedirs(bucket, exist_ok=True)
    except OSError:
        root = _TRASH_ROOT
        bucket = os.path.join(root, _trash_bucket_name())
        os.makedirs(bucket, exist_ok=True)
    name = os.path.basename(path)
    base, ext = os.path.splitext(name)
    dst = os.path.join(bucket, name)
    n = 1
    while os.path.lexists(dst):
        dst = os.path.join(bucket, f"{base} ({n}){ext}")
        n += 1
    try:
        os.rename(path, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(path, dst)
    logger.info("trash  | %s -> %s", path, dst)
    _record_trash_manifest(bucket, os.path.basename(dst), path)
    return dst


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
    manifest_path = os.path.join(bucket_path, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


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
                    "restorable": bool(rec),
                })
    return entries


def restore_trash_entry(bucket: str, name: str, roots) -> dict:
    """Restore a manifest-backed trash entry to its recorded original_path.

    Safety:
      - ``bucket``/``name`` are validated as single path components (no
        separators, no ``..``) so nothing outside the trash roots is ever
        reachable, regardless of what a caller supplies.
      - Refuses (never overwrites) if the destination is already occupied.
      - Refuses if the entry or its manifest record can't be found.

    Returns ``{"ok": True, "restored_path": ...}`` or
    ``{"ok": False, "error": ...}``. Never raises for expected failure modes.
    """
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
        manifest = _load_manifest(bucket_path)
        rec = next((r for r in manifest if r.get("trashed_name") == name), None)
        if not rec or not rec.get("original_path"):
            return {"ok": False, "error": "No manifest record for this entry — cannot restore safely"}
        original_path = rec["original_path"]
        if os.path.lexists(original_path):
            return {"ok": False, "error": f"Destination already exists: {original_path}"}
        try:
            os.makedirs(os.path.dirname(original_path) or ".", exist_ok=True)
            os.rename(fpath, original_path)
        except OSError as e:
            if e.errno != errno.EXDEV:
                return {"ok": False, "error": f"Restore failed: {e}"}
            try:
                shutil.move(fpath, original_path)
            except OSError as e2:
                return {"ok": False, "error": f"Restore failed: {e2}"}
        remaining = [r for r in manifest if r is not rec]
        try:
            _save_manifest(bucket_path, remaining)
        except OSError:
            logger.warning("Failed to update manifest after restore at %s (non-fatal)",
                           bucket_path, exc_info=True)
        logger.info("restore | %s -> %s", fpath, original_path)
        return {"ok": True, "restored_path": original_path}

    return {"ok": False, "error": "Trash entry not found"}


def all_trash_roots() -> list:
    """All trash roots worth scanning/sweeping: the app-data fallback root
    (``_TRASH_ROOT``) plus every per-volume ``<volume>/.scanhound-trash`` root
    implied by ``_trash_root_for`` for each drive letter currently known to
    Windows (or ``/`` on POSIX).

    Single source of truth shared by the ``/rename/trash`` list/restore
    endpoints and the maintenance-pass retention sweep — a disposal on any
    volume must be reachable (and eventually swept) by both. Scanning every
    drive's candidate root is cheap (a single ``os.path.isdir`` each); a
    per-volume ``.scanhound-trash`` directory only exists if a disposal
    actually created it, so this can't return anything a real trash disposal
    didn't put there.
    """
    roots = {os.path.abspath(_TRASH_ROOT)}
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.isdir(drive):
                roots.add(_trash_root_for(drive))
    else:
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
    """Delete trash buckets older than ``retention_days``; remove emptied buckets.

    Only ever touches files strictly under the given trash roots (defaults to
    :func:`all_trash_roots` — every per-volume ``.scanhound-trash`` root plus
    the app-data fallback — so a real disposal on any drive is eventually
    swept even if the caller doesn't pass ``roots`` explicitly). Never follows symlinks —
    a symlink found inside an old bucket is unlinked (removing the link
    itself), never resolved and deleted at its target. Logs a per-run summary
    and is fail-safe: per-file/per-bucket errors are logged and skipped
    rather than aborting the whole sweep.

    Returns ``{"files_deleted": int, "bytes_freed": int, "buckets_removed": int}``.
    """
    if roots is None:
        roots = all_trash_roots()
    files_deleted = 0
    bytes_freed = 0
    buckets_removed = 0

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        try:
            bucket_names = os.listdir(root)
        except OSError:
            continue
        for bucket_name in bucket_names:
            bucket_path = os.path.join(root, bucket_name)
            if os.path.islink(bucket_path) or not os.path.isdir(bucket_path):
                continue  # never descend into a symlinked "bucket"
            if _bucket_age_days(bucket_path) < retention_days:
                continue
            try:
                for entry in os.listdir(bucket_path):
                    epath = os.path.join(bucket_path, entry)
                    try:
                        if os.path.islink(epath):
                            os.unlink(epath)  # remove the link, never its target
                        elif os.path.isfile(epath):
                            size = os.path.getsize(epath)
                            os.remove(epath)
                            if entry != "manifest.json":
                                files_deleted += 1
                                bytes_freed += size
                        elif os.path.isdir(epath):
                            shutil.rmtree(epath, ignore_errors=True)
                    except OSError:
                        logger.warning("sweep_trash: failed to remove %s (skipped)",
                                       epath, exc_info=True)
                os.rmdir(bucket_path)
                buckets_removed += 1
            except OSError:
                logger.warning("sweep_trash: failed to remove bucket %s (skipped)",
                               bucket_path, exc_info=True)

    logger.info("sweep_trash: removed %d file(s), %d bucket(s), freed %d bytes (retention=%dd)",
               files_deleted, buckets_removed, bytes_freed, retention_days)
    return {"files_deleted": files_deleted, "bytes_freed": bytes_freed,
            "buckets_removed": buckets_removed}


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
            return "hardlink"
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            method = "copy"  # cross-device → fall back to a verified copy

    if method == "symlink":
        os.symlink(os.path.abspath(src), dst)
        return "symlink"

    if method == "copy":
        _copy_verify_atomic(src, dst, progress_cb)
        return "copy"

    # move: rename first (instant same-fs), else crash-safe copy + dispose source.
    try:
        os.rename(src, dst)
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
