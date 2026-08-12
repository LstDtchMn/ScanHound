"""undo_place must never destroy the last copy of a media file.

The bug these tests pin (found by the 2026-08-12 review, `fileops.py:1757`): the
hardlink/symlink/copy branch removed the destination on the UNCHECKED assumption
that "the original src still exists". When the source had since been cleaned up
(JDownloader clearing the download folder is the ordinary case), Undo deleted the
LIBRARY file and reported success:

  * hardlink is the DEFAULT for a same-volume apply, and is what an unattended
    'move' is forced down to (Guard 1 in the fileops module docstring). src and
    dst are two directory entries for ONE inode, so with src gone, dst is the
    last link and unlinking it destroys the file data.
  * copy is the cross-device fallback, where dst is the only remaining copy.

The module's own mandate is "no accidental file deletion; deletions must go
through a user's input first", so the fix fails closed and routes the removal
through the trash.
"""
import os

import pytest

from backend.rename import fileops


def _use_tmp_trash(monkeypatch, tmp_path):
    """Point the trash at a tmp root, as the existing fileops tests do."""
    trash_root = tmp_path / "trash"
    monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
    monkeypatch.setattr(fileops, "_TRASH_ROOT", str(trash_root))
    return trash_root


# ── the data-loss guard ──────────────────────────────────────────────────────

def test_hardlink_undo_refuses_when_source_is_gone(tmp_path, monkeypatch):
    """THE bug. src and dst share one inode; deleting dst would destroy the file."""
    _use_tmp_trash(monkeypatch, tmp_path)
    src = tmp_path / "dl" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("the only copy")
    dst = tmp_path / "lib" / "Movie (2020).mkv"
    assert fileops.place_file(str(src), str(dst), "hardlink") == "hardlink"

    os.remove(str(src))  # JDownloader cleaned up the download folder

    with pytest.raises(FileNotFoundError):
        fileops.undo_place(str(src), str(dst), "hardlink")

    assert dst.exists(), "undo destroyed the last copy of the library file"
    assert dst.read_text() == "the only copy"


def test_copy_undo_refuses_when_source_is_gone(tmp_path, monkeypatch):
    """Same guard on the cross-device 'copy' path, where dst is an independent copy."""
    _use_tmp_trash(monkeypatch, tmp_path)
    src = tmp_path / "dl" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("the only copy")
    dst = tmp_path / "lib" / "Movie (2020).mkv"
    assert fileops.place_file(str(src), str(dst), "copy") == "copy"

    os.remove(str(src))

    with pytest.raises(FileNotFoundError):
        fileops.undo_place(str(src), str(dst), "copy")

    assert dst.exists(), "undo destroyed the last copy of the library file"
    assert dst.read_text() == "the only copy"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_symlink_undo_still_removes_a_dangling_link(tmp_path, monkeypatch):
    """symlink is exempt: dst only POINTS at src, so removing it destroys nothing.

    The guard must not over-fire and strand dangling links."""
    _use_tmp_trash(monkeypatch, tmp_path)
    src = tmp_path / "dl" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("data")
    dst = tmp_path / "lib" / "Movie (2020).mkv"
    try:
        placed = fileops.place_file(str(src), str(dst), "symlink")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    if placed != "symlink":
        pytest.skip("placement fell back to %s" % placed)

    os.remove(str(src))

    fileops.undo_place(str(src), str(dst), "symlink")  # must NOT raise
    assert not os.path.lexists(str(dst))


# ── positive controls: the ordinary undo still works ─────────────────────────

def test_normal_hardlink_undo_unlinks_when_redundancy_is_proven(tmp_path, monkeypatch):
    """Positive control. Without this, the guard tests could pass vacuously
    (e.g. if undo raised for every input).

    For a hardlink whose src and dst are still the SAME inode, redundancy is
    proven, so dropping the directory entry is provably lossless and needs no
    trash churn."""
    trash_root = _use_tmp_trash(monkeypatch, tmp_path)
    src = tmp_path / "dl" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("data")
    dst = tmp_path / "lib" / "Movie (2020).mkv"
    assert fileops.place_file(str(src), str(dst), "hardlink") == "hardlink"
    assert os.path.samefile(str(src), str(dst))

    fileops.undo_place(str(src), str(dst), "hardlink")

    assert not dst.exists(), "the placed library file should be gone from the library"
    assert src.exists() and src.read_text() == "data", "the original must survive"
    trashed = [p for p in trash_root.rglob("*") if p.is_file() and p.name.endswith(".mkv")]
    assert not trashed, "a provably-redundant hardlink entry need not be trashed"


def test_hardlink_undo_trashes_when_src_is_a_DIFFERENT_file(tmp_path, monkeypatch):
    """Peer review Q2: a surviving src is NOT proof the bytes are duplicated.

    If src was replaced since the apply, src and dst are no longer one inode, so
    dst holds the only copy of what was placed. The removal must stay recoverable."""
    trash_root = _use_tmp_trash(monkeypatch, tmp_path)
    src = tmp_path / "dl" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("version A")
    dst = tmp_path / "lib" / "Movie (2020).mkv"
    assert fileops.place_file(str(src), str(dst), "hardlink") == "hardlink"

    os.remove(str(src))            # original cleaned up ...
    src.write_text("version B")    # ... and a DIFFERENT file re-downloaded in its place
    assert not os.path.samefile(str(src), str(dst))

    fileops.undo_place(str(src), str(dst), "hardlink")

    assert not dst.exists()
    recovered = [p for p in trash_root.rglob("*")
                 if p.is_file() and p.read_text() == "version A"]
    assert recovered, "version A was destroyed instead of trashed"


def test_copy_undo_trashes_so_a_replaced_source_cannot_lose_data(tmp_path, monkeypatch):
    """copy equivalence cannot be proven cheaply, so the removal is recoverable."""
    trash_root = _use_tmp_trash(monkeypatch, tmp_path)
    src = tmp_path / "dl" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("version A")
    dst = tmp_path / "lib" / "Movie (2020).mkv"
    assert fileops.place_file(str(src), str(dst), "copy") == "copy"

    src.write_text("version B")    # src replaced after the apply

    fileops.undo_place(str(src), str(dst), "copy")

    assert not dst.exists()
    assert src.read_text() == "version B"
    recovered = [p for p in trash_root.rglob("*")
                 if p.is_file() and p.read_text() == "version A"]
    assert recovered, "the placed version was destroyed instead of trashed"
