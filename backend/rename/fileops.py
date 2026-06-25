"""Safe file placement (move/copy/hardlink/symlink) + reversible undo.

Ported/adapted from Nomen's ``file_manager_io._move_file``, decoupled from its
app/logger/progress machinery. Each placement is collision-safe (never
overwrites) and verifiable, and records enough to be undone.
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil

MOVE_METHODS = ("move", "copy", "hardlink", "symlink")


def _hash_file(path: str) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def place_file(src: str, dst: str, method: str = "hardlink") -> str:
    """Place ``src`` at ``dst`` using ``method``; return the method used.

    Collision-safe: refuses to overwrite an existing destination. Verifies
    copies by hash. Raises on failure so the caller can record an error.
    """
    if method not in MOVE_METHODS:
        method = "hardlink"
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

    # move: rename first (instant same-fs), else copy + verify + delete source.
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)
        if _hash_file(src) != _hash_file(dst):
            os.remove(dst)
            raise OSError("Cross-device move verification failed (hash mismatch)")
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
