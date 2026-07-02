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
import logging
import os
import shutil

from backend.config import _DATA_DIR

logger = logging.getLogger(__name__)

MOVE_METHODS = ("move", "copy", "hardlink", "symlink")

# Trash root — overridable by tests via monkeypatch.
_TRASH_ROOT = os.path.join(_DATA_DIR, "trash")


def _hash_file(path: str) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _trash_bucket_name() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _trash_root_for(path: str) -> str:
    """Return the trash root that lives on ``path``'s own volume.

    Disposal must never require a cross-device copy, so the trash bucket is
    sited on the SOURCE's own drive/UNC share (``<anchor>/.scanhound-trash``)
    rather than under the app-data dir — otherwise every cross-device 'move'
    would EXDEV into ``_TRASH_ROOT`` and ``shutil.move`` would silently copy
    the full media file into (often OneDrive-synced) app-data.

    Falls back to the module-level ``_TRASH_ROOT`` if ``path`` has no drive
    anchor (e.g. a relative path with no drive component on this platform).
    """
    anchor, _ = os.path.splitdrive(os.path.abspath(path))
    if not anchor:
        return _TRASH_ROOT
    return os.path.join(anchor + os.sep, ".scanhound-trash")


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
    return dst


def place_file(src: str, dst: str, method: str = "hardlink", *,
               automatic: bool = False,
               deletions_require_confirmation: bool = True) -> str:
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
        shutil.copy2(src, dst)
        if _hash_file(src) != _hash_file(dst):
            os.remove(dst)
            raise OSError("Copy verification failed (hash mismatch)")
        return "copy"

    # move: rename first (instant same-fs), else copy + verify + dispose of source.
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)
        if _hash_file(src) != _hash_file(dst):
            os.remove(dst)
            raise OSError("Cross-device move verification failed (hash mismatch)")
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
