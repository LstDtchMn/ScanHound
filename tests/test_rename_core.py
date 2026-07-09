"""Unit tests for the ported rename core: confidence, naming, fileops."""
import os
import pytest

from backend.rename import confidence, naming, fileops


class TestConfidence:
    def test_identical_titles_high(self):
        assert confidence.match_confidence("The Matrix", "The Matrix") >= 95

    def test_year_match_boosts_score(self):
        assert confidence.match_confidence("Dune", "Dune", 2021, 2021) >= \
            confidence.match_confidence("Dune", "Dune")

    def test_conflicting_year_penalized(self):
        same = confidence.match_confidence("Dune", "Dune", 2021, 2021)
        diff = confidence.match_confidence("Dune", "Dune", 2021, 1984)
        assert diff < same

    def test_unrelated_titles_low(self):
        assert confidence.match_confidence("The Matrix", "Frozen") < 50

    def test_similarity_bounds(self):
        assert confidence.dice_similarity("", "x") == 0.0
        assert 0.0 <= confidence.title_similarity("the office us", "The Office") <= 1.0


class TestNaming:
    def test_movie_plex_name(self):
        fname, dest = naming.build_target(
            {"media_type": "movie", "title": "Dune", "year": 2021,
             "resolution": "2160p", "original_filename": "dune.2021.2160p.mkv"},
            movie_root="/movies")
        assert fname == "Dune (2021) [2160p].mkv"
        assert dest == os.path.join("/movies", "Dune (2021)")

    def test_tv_plex_name(self):
        fname, dest = naming.build_target(
            {"media_type": "tv", "title": "The Office", "year": 2005,
             "season": 2, "episode": 5, "episode_title": "Halloween",
             "original_filename": "the.office.s02e05.mkv"},
            tv_root="/tv")
        assert fname == "The Office (2005) - S02E05 - Halloween.mkv"
        assert dest == os.path.join("/tv", "The Office (2005)", "Season 02")

    def test_template_substitution_with_default(self):
        out = naming.render_template("{{title}} ({{year}})", {"title": "Heat", "year": "1995"})
        assert out == "Heat (1995)"

    def test_template_drops_empty_conditional_section(self):
        out = naming.render_template("{{title}}[ - {{episode_title}}]",
                                     {"title": "Movie", "episode_title": ""})
        assert out == "Movie"

    def test_template_keeps_section_when_present(self):
        out = naming.render_template("{{title}}[ - {{episode_title}}]",
                                     {"title": "Show", "episode_title": "Pilot"})
        assert out == "Show - Pilot"

    def test_template_strips_path_separators(self):
        out = naming.render_template("{{title}}", {"title": "a/b\\c"})
        assert "/" not in out and "\\" not in out

    def test_unknown_extension_defaults_to_mkv(self):
        fname, _ = naming.build_target(
            {"media_type": "movie", "title": "X", "year": 2020,
             "original_filename": "x.iso"}, movie_root="/m")
        assert fname.endswith(".mkv")


class TestFileOps:
    def test_hardlink_and_undo(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("data")
        dst = tmp_path / "lib" / "out.mkv"
        assert fileops.place_file(str(src), str(dst), "hardlink") == "hardlink"
        assert dst.exists() and src.exists()
        fileops.undo_place(str(src), str(dst), "hardlink")
        assert not dst.exists() and src.exists()

    def test_move_and_undo(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("data")
        dst = tmp_path / "lib" / "out.mkv"
        assert fileops.place_file(str(src), str(dst), "move") == "move"
        assert dst.exists() and not src.exists()
        fileops.undo_place(str(src), str(dst), "move")
        assert src.exists() and not dst.exists()

    def test_copy_verifies_and_keeps_source(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_bytes(b"hello world")
        dst = tmp_path / "out.mkv"
        assert fileops.place_file(str(src), str(dst), "copy") == "copy"
        assert dst.read_bytes() == b"hello world" and src.exists()

    def test_copy_leaves_no_part_file_on_success(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_bytes(b"x" * 5000)
        dst = tmp_path / "lib" / "out.mkv"
        fileops.place_file(str(src), str(dst), "copy")
        assert dst.exists()
        assert not (tmp_path / "lib" / "out.mkv.part").exists()

    def test_copy_reports_progress(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_bytes(b"y" * (20 * 1024 * 1024 + 7))
        dst = tmp_path / "out.mkv"
        seen = []
        fileops.place_file(str(src), str(dst), "copy",
                           progress_cb=lambda d, t: seen.append((d, t)))
        assert seen, "progress_cb never called"
        assert seen[-1][0] == seen[-1][1] == src.stat().st_size  # ends at 100%
        assert all(t == src.stat().st_size for _, t in seen)     # total is stable

    def test_corrupted_copy_is_rejected_source_kept(self, tmp_path, monkeypatch):
        # Simulate the destination bytes being corrupt on disk: the cold-read
        # verify hash won't match the source, so the copy is rejected, the real
        # destination is never created, and the source is preserved.
        src = tmp_path / "src.mkv"; src.write_bytes(b"good" * 4096)
        dst = tmp_path / "lib" / "out.mkv"
        monkeypatch.setattr(fileops, "_hash_file",
                            lambda p, **kw: "deadbeef")  # verify read returns garbage
        with pytest.raises(OSError, match="verification failed"):
            fileops.place_file(str(src), str(dst), "copy")
        assert not dst.exists()
        assert not (tmp_path / "lib" / "out.mkv.part").exists()
        assert src.read_bytes() == b"good" * 4096

    def test_crash_before_atomic_rename_leaves_no_partial_dst(self, tmp_path, monkeypatch):
        # Simulate a crash at the worst moment: bytes written to .part, but the
        # process dies just before the atomic rename. The real destination must
        # NOT exist (no partial file), and the source is untouched.
        src = tmp_path / "src.mkv"; src.write_bytes(b"z" * 4096)
        dst = tmp_path / "lib" / "out.mkv"

        def _boom(*a, **k):
            raise OSError("simulated power loss")
        monkeypatch.setattr(fileops.os, "replace", _boom)

        with pytest.raises(OSError):
            fileops.place_file(str(src), str(dst), "copy")
        assert not dst.exists(), "a partial file was left at the real destination!"
        assert not (tmp_path / "lib" / "out.mkv.part").exists()  # .part cleaned up
        assert src.read_bytes() == b"z" * 4096                    # source intact

    def test_crash_mid_move_keeps_source_recoverable(self, tmp_path, monkeypatch):
        # Cross-device 'move' that crashes mid-copy: source must survive so the
        # file is never lost, and no partial appears at the destination.
        src = tmp_path / "src.mkv"; src.write_bytes(b"q" * 8192)
        dst = tmp_path / "lib" / "out.mkv"
        # Force the EXDEV (cross-device) branch, then crash the atomic rename.
        real_rename = fileops.os.rename
        def _exdev(a, b):
            import errno as _e
            raise OSError(_e.EXDEV, "cross-device")
        monkeypatch.setattr(fileops.os, "rename", _exdev)
        monkeypatch.setattr(fileops.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError):
            fileops.place_file(str(src), str(dst), "move")
        assert src.exists() and src.read_bytes() == b"q" * 8192   # NEVER lost
        assert not dst.exists()

    def test_refuses_to_overwrite(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("a")
        dst = tmp_path / "out.mkv"; dst.write_text("existing")
        with pytest.raises(FileExistsError):
            fileops.place_file(str(src), str(dst), "move")

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            fileops.place_file(str(tmp_path / "nope.mkv"), str(tmp_path / "o.mkv"), "move")

    def test_hardlink_falls_back_to_copy_across_filesystems(self, tmp_path, monkeypatch):
        """A cross-device hardlink (EXDEV) must degrade to a verified copy.

        Covers the real JD-output-vs-Plex-library-on-different-volumes case,
        which can't be reproduced with a single tmp_path, by simulating the
        kernel's EXDEV at os.link.
        """
        import errno as _errno

        def _exdev(*_a, **_k):
            raise OSError(_errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(fileops.os, "link", _exdev)
        src = tmp_path / "src.mkv"; src.write_bytes(b"payload")
        dst = tmp_path / "lib" / "out.mkv"
        # Falls back to copy: returns "copy", keeps the source, verifies content.
        assert fileops.place_file(str(src), str(dst), "hardlink") == "copy"
        assert dst.read_bytes() == b"payload" and src.exists()
        # Undo of the copy-fallback drops the destination, leaves the source.
        fileops.undo_place(str(src), str(dst), "copy")
        assert not dst.exists() and src.exists()

    def test_non_exdev_link_error_propagates(self, tmp_path, monkeypatch):
        """A hardlink failure that ISN'T cross-device must not be swallowed."""
        import errno as _errno

        def _eperm(*_a, **_k):
            raise OSError(_errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(fileops.os, "link", _eperm)
        src = tmp_path / "src.mkv"; src.write_text("x")
        dst = tmp_path / "out.mkv"
        with pytest.raises(OSError):
            fileops.place_file(str(src), str(dst), "hardlink")

    # ── Guard 1: automatic applies never consume the source ────────────

    def test_automatic_move_forces_hardlink(self, tmp_path):
        """automatic=True + method='move' must not consume the source —
        it degrades to hardlink (never a bare rename/remove)."""
        src = tmp_path / "src.mkv"; src.write_text("data")
        dst = tmp_path / "lib" / "out.mkv"
        used = fileops.place_file(str(src), str(dst), "move", automatic=True)
        assert used == "hardlink"
        assert dst.exists() and src.exists()

    def test_automatic_move_falls_back_to_copy_when_hardlink_impossible(
            self, tmp_path, monkeypatch):
        """automatic=True forces hardlink; if hardlink itself can't be done
        (cross-device), it must fall back to a verified copy — never delete
        the source."""
        import errno as _errno

        def _exdev(*_a, **_k):
            raise OSError(_errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(fileops.os, "link", _exdev)
        src = tmp_path / "src.mkv"; src.write_bytes(b"payload")
        dst = tmp_path / "lib" / "out.mkv"
        used = fileops.place_file(str(src), str(dst), "move", automatic=True)
        assert used == "copy"
        assert dst.read_bytes() == b"payload" and src.exists()

    def test_user_initiated_move_still_consumes_source(self, tmp_path):
        """Non-automatic (user-initiated) applies keep the configured
        'move' behavior — this is the control case for Guard 1."""
        src = tmp_path / "src.mkv"; src.write_text("data")
        dst = tmp_path / "lib" / "out.mkv"
        used = fileops.place_file(str(src), str(dst), "move", automatic=False)
        assert used == "move"
        assert dst.exists() and not src.exists()

    # ── Guard 3: trash instead of hard delete ───────────────────────────

    def test_trash_moves_file_into_timestamped_dir(self, tmp_path, monkeypatch):
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        f = tmp_path / "doomed.mkv"; f.write_text("bye")
        trashed_path = fileops._trash(str(f))
        assert not f.exists()
        assert os.path.isfile(trashed_path)
        assert os.path.basename(trashed_path) == "doomed.mkv"
        # Landed under trash_root, in a timestamped subdirectory.
        assert os.path.commonpath([str(trash_root), trashed_path]) == str(trash_root)

    def test_trash_handles_name_collision(self, tmp_path, monkeypatch):
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        f1 = tmp_path / "dupe.mkv"; f1.write_text("one")
        f2 = tmp_path / "sub" / "dupe.mkv"; f2.parent.mkdir(); f2.write_text("two")
        # Force both into the *same* timestamp bucket to guarantee collision.
        monkeypatch.setattr(fileops, "_trash_bucket_name", lambda: "20260101-000000")
        p1 = fileops._trash(str(f1))
        p2 = fileops._trash(str(f2))
        assert p1 != p2
        assert os.path.isfile(p1) and os.path.isfile(p2)
        assert open(p1).read() == "one" and open(p2).read() == "two"

    def test_trash_falls_back_to_shutil_move_cross_device(self, tmp_path, monkeypatch):
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        import errno as _errno

        real_rename = fileops.os.rename

        def _exdev_once(src, dst):
            raise OSError(_errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(fileops.os, "rename", _exdev_once)
        f = tmp_path / "doomed.mkv"; f.write_text("bye")
        trashed_path = fileops._trash(str(f))
        assert not f.exists()
        assert os.path.isfile(trashed_path)

    def test_cross_device_move_trashes_source_by_default(self, tmp_path, monkeypatch):
        """place_file's cross-device move branch: with deletions_require_confirmation
        (the default gate), the source goes to trash, not os.remove."""
        import errno as _errno
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))

        def _exdev(*_a, **_k):
            raise OSError(_errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(fileops.os, "link", _exdev)
        monkeypatch.setattr(fileops.os, "rename", _exdev)
        src = tmp_path / "src.mkv"; src.write_bytes(b"payload")
        dst = tmp_path / "lib" / "out.mkv"
        used = fileops.place_file(str(src), str(dst), "move",
                                  deletions_require_confirmation=True)
        assert used == "move"
        assert dst.read_bytes() == b"payload"
        assert not src.exists()
        # Source ended up in trash, not permanently deleted.
        trashed = list(trash_root.rglob("src.mkv"))
        assert len(trashed) == 1

    def test_cross_device_move_hard_deletes_when_confirmation_disabled(
            self, tmp_path, monkeypatch):
        """Explicit opt-out (deletions_require_confirmation=False) restores
        the old hard os.remove behavior."""
        import errno as _errno
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))

        def _exdev(*_a, **_k):
            raise OSError(_errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(fileops.os, "link", _exdev)
        monkeypatch.setattr(fileops.os, "rename", _exdev)
        src = tmp_path / "src.mkv"; src.write_bytes(b"payload")
        dst = tmp_path / "lib" / "out.mkv"
        used = fileops.place_file(str(src), str(dst), "move",
                                  deletions_require_confirmation=False)
        assert used == "move"
        assert not src.exists()
        # Nothing in trash — hard-deleted as before.
        assert not trash_root.exists() or not list(trash_root.rglob("src.mkv"))

    # ── M1: trash sited on the source's own volume, not app-data ────────

    def test_trash_root_for_derives_from_source_anchor_not_data_dir(self, tmp_path):
        """_trash_root_for(path) must be rooted at the SOURCE's own volume
        anchor (drive letter / UNC share), never under the app's _DATA_DIR —
        otherwise a cross-device disposal copies media bytes into app-data."""
        src = tmp_path / "movie.mkv"; src.write_text("x")
        anchor, _ = os.path.splitdrive(os.path.abspath(str(src)))
        root = fileops._trash_root_for(str(src))
        assert root == os.path.join(anchor + os.sep, ".scanhound-trash")
        assert os.path.commonpath([root, fileops._DATA_DIR]) != \
            os.path.commonpath([root, root])  # root is not nested under _DATA_DIR
        assert not root.startswith(fileops._DATA_DIR)

    def test_trash_root_for_falls_back_to_trash_root_when_no_drive(self, monkeypatch):
        """If splitdrive yields no anchor (relative path with no drive), fall
        back to the module-level _TRASH_ROOT rather than raising."""
        monkeypatch.setattr(fileops.os.path, "splitdrive", lambda p: ("", p))
        assert fileops._trash_root_for("whatever") == fileops._TRASH_ROOT

    def test_trash_moves_into_source_volume_bucket_without_data_dir_copy(self, tmp_path):
        """End-to-end (no _TRASH_ROOT monkeypatch): trashing a source file
        lands it under a `.scanhound-trash` bucket on the SOURCE's own
        volume/tmp_path, and never touches _DATA_DIR at all — proving no
        media bytes are copied cross-device into app-data."""
        f = tmp_path / "doomed.mkv"; f.write_text("bye")
        before = set()
        if os.path.isdir(fileops._DATA_DIR):
            before = {os.path.join(dp, fn) for dp, _, fns in os.walk(fileops._DATA_DIR)
                      for fn in fns}

        trashed_path = fileops._trash(str(f))

        assert not f.exists()
        assert os.path.isfile(trashed_path)
        assert os.path.basename(trashed_path) == "doomed.mkv"
        # Landed under a same-volume .scanhound-trash bucket, not _DATA_DIR.
        anchor, _ = os.path.splitdrive(os.path.abspath(str(f)))
        expected_root = os.path.join(anchor + os.sep, ".scanhound-trash")
        assert os.path.commonpath([expected_root, trashed_path]) == expected_root
        assert not trashed_path.startswith(fileops._DATA_DIR)
        # _DATA_DIR's contents are untouched — no bytes copied in.
        after = set()
        if os.path.isdir(fileops._DATA_DIR):
            after = {os.path.join(dp, fn) for dp, _, fns in os.walk(fileops._DATA_DIR)
                     for fn in fns}
        assert after == before

        # Cleanup: remove the bucket we created on the real source volume.
        shutil_bucket = os.path.dirname(trashed_path)
        if os.path.isdir(shutil_bucket):
            import shutil as _shutil
            _shutil.rmtree(shutil_bucket, ignore_errors=True)
        trash_root_dir = os.path.dirname(shutil_bucket)
        if os.path.isdir(trash_root_dir) and not os.listdir(trash_root_dir):
            os.rmdir(trash_root_dir)

    # ── Trash manifest (enables restore) ─────────────────────────────────

    def test_trash_writes_manifest_record(self, tmp_path, monkeypatch):
        import json as _json
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        monkeypatch.setattr(fileops, "_trash_bucket_name", lambda: "20260101-000000")
        f = tmp_path / "movie.mkv"; f.write_text("bye")
        trashed_path = fileops._trash(str(f))

        bucket = os.path.dirname(trashed_path)
        manifest_path = os.path.join(bucket, "manifest.json")
        assert os.path.isfile(manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as mf:
            records = _json.load(mf)
        assert len(records) == 1
        rec = records[0]
        assert rec["trashed_name"] == "movie.mkv"
        assert rec["original_path"] == os.path.abspath(str(f))
        assert "trashed_at" in rec

    def test_trash_manifest_accumulates_multiple_records(self, tmp_path, monkeypatch):
        import json as _json
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        monkeypatch.setattr(fileops, "_trash_bucket_name", lambda: "20260101-000000")
        f1 = tmp_path / "one.mkv"; f1.write_text("1")
        f2 = tmp_path / "sub" / "two.mkv"; f2.parent.mkdir(); f2.write_text("2")

        p1 = fileops._trash(str(f1))
        p2 = fileops._trash(str(f2))

        bucket = os.path.dirname(p1)
        assert bucket == os.path.dirname(p2)
        manifest_path = os.path.join(bucket, "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as mf:
            records = _json.load(mf)
        assert len(records) == 2
        originals = {r["original_path"] for r in records}
        assert originals == {os.path.abspath(str(f1)), os.path.abspath(str(f2))}

    def test_trash_manifest_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        """A manifest write failure must be logged, never propagated out of
        _trash — losing the restore record is acceptable, losing the file
        disposal guarantee is not."""
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        f = tmp_path / "movie.mkv"; f.write_text("bye")

        real_open = open

        def _boom(path, *a, **kw):
            if str(path).endswith("manifest.json"):
                raise OSError("disk full")
            return real_open(path, *a, **kw)

        monkeypatch.setattr(fileops, "open", _boom, raising=False)
        # _trash must still succeed and move the file despite the manifest failure.
        trashed_path = fileops._trash(str(f))
        assert not f.exists()
        assert os.path.isfile(trashed_path)


class TestTrashListAndRestore:
    """list_trash_entries() / restore_trash_entry() — trash browsing + undo."""

    def _trash_one(self, tmp_path, monkeypatch, root, bucket_name, filename, content="x"):
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(root))
        monkeypatch.setattr(fileops, "_trash_bucket_name", lambda: bucket_name)
        f = tmp_path / filename
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        return fileops._trash(str(f)), f

    def test_list_shows_trashed_file_with_original_path(self, tmp_path, monkeypatch):
        root = tmp_path / "trash"
        trashed_path, original = self._trash_one(
            tmp_path, monkeypatch, root, "20260101-000000", "movie.mkv")

        entries = fileops.list_trash_entries([str(root)])
        assert len(entries) == 1
        e = entries[0]
        assert e["bucket"] == "20260101-000000"
        assert e["name"] == "movie.mkv"
        assert e["original_path"] == os.path.abspath(str(original))
        assert e["restorable"] is True
        assert e["size"] == len("x")

    def test_list_entry_without_manifest_has_null_original_path(self, tmp_path):
        root = tmp_path / "trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "orphan.mkv").write_text("orphan")

        entries = fileops.list_trash_entries([str(root)])
        assert len(entries) == 1
        assert entries[0]["original_path"] is None
        assert entries[0]["restorable"] is False

    def test_restore_moves_file_back_and_removes_manifest_record(self, tmp_path, monkeypatch):
        root = tmp_path / "trash"
        trashed_path, original = self._trash_one(
            tmp_path, monkeypatch, root, "20260101-000000", "sub/movie.mkv")
        assert not original.exists()

        result = fileops.restore_trash_entry("20260101-000000", "movie.mkv", [str(root)])
        assert result["ok"] is True
        assert original.exists()
        assert original.read_text() == "x"
        assert not os.path.exists(trashed_path)

        # Manifest record removed.
        manifest_path = os.path.join(os.path.dirname(trashed_path), "manifest.json")
        import json as _json
        with open(manifest_path, "r", encoding="utf-8") as f:
            records = _json.load(f)
        assert records == []

        entries = fileops.list_trash_entries([str(root)])
        assert entries == []

    def test_restore_refuses_when_destination_occupied(self, tmp_path, monkeypatch):
        root = tmp_path / "trash"
        trashed_path, original = self._trash_one(
            tmp_path, monkeypatch, root, "20260101-000000", "movie.mkv")
        # Something now occupies the original path.
        original.write_text("occupied")

        result = fileops.restore_trash_entry("20260101-000000", "movie.mkv", [str(root)])
        assert result["ok"] is False
        assert "already exists" in result["error"].lower()
        # File stays in trash, untouched.
        assert os.path.isfile(trashed_path)
        assert original.read_text() == "occupied"

    def test_restore_missing_manifest_entry_errors(self, tmp_path):
        root = tmp_path / "trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "orphan.mkv").write_text("orphan")

        result = fileops.restore_trash_entry("20260101-000000", "orphan.mkv", [str(root)])
        assert result["ok"] is False
        assert os.path.isfile(bucket / "orphan.mkv")

    def test_restore_missing_bucket_or_file_errors(self, tmp_path):
        root = tmp_path / "trash"
        result = fileops.restore_trash_entry("20260101-999999", "ghost.mkv", [str(root)])
        assert result["ok"] is False

    @pytest.mark.parametrize("bad_bucket,bad_name", [
        ("../escape", "movie.mkv"),
        ("20260101-000000", "../escape.mkv"),
        ("20260101-000000", "sub/movie.mkv"),
        ("sub/dir", "movie.mkv"),
    ])
    def test_restore_rejects_path_traversal(self, tmp_path, bad_bucket, bad_name):
        root = tmp_path / "trash"
        root.mkdir(parents=True)
        result = fileops.restore_trash_entry(bad_bucket, bad_name, [str(root)])
        assert result["ok"] is False
        assert "invalid" in result["error"].lower() or "traversal" in result["error"].lower()

    def test_list_covers_multiple_trash_roots(self, tmp_path, monkeypatch):
        root1 = tmp_path / "trashA"
        root2 = tmp_path / "trashB"
        self._trash_one(tmp_path, monkeypatch, root1, "20260101-000000", "a.mkv")
        self._trash_one(tmp_path, monkeypatch, root2, "20260101-000001", "b.mkv")

        entries = fileops.list_trash_entries([str(root1), str(root2)])
        names = {e["name"] for e in entries}
        assert names == {"a.mkv", "b.mkv"}


class TestTrashRetentionSweep:
    """sweep_trash() — deletes only old buckets under the trash roots."""

    @staticmethod
    def _bucket_name_for_age(age_days: int) -> str:
        import datetime as _dt
        return (_dt.datetime.now() - _dt.timedelta(days=age_days)).strftime("%Y%m%d-%H%M%S")

    def _make_bucket(self, root, age_days, filename="movie.mkv"):
        bucket = root / self._bucket_name_for_age(age_days)
        bucket.mkdir(parents=True, exist_ok=True)
        f = bucket / filename
        f.write_text("x")
        return bucket, f

    def test_sweeps_bucket_older_than_retention(self, tmp_path):
        root = tmp_path / "trash"
        old_bucket, old_file = self._make_bucket(root, age_days=400)

        summary = fileops.sweep_trash(30, roots=[str(root)])

        assert not old_file.exists()
        assert not old_bucket.exists()
        assert summary["files_deleted"] == 1
        assert summary["bytes_freed"] == 1  # "x" is 1 byte

    def test_keeps_fresh_bucket(self, tmp_path):
        root = tmp_path / "trash"
        fresh_bucket, fresh_file = self._make_bucket(root, age_days=1)

        summary = fileops.sweep_trash(30, roots=[str(root)])

        assert fresh_file.exists()
        assert fresh_bucket.exists()
        assert summary["files_deleted"] == 0

    def test_never_touches_files_outside_trash_root(self, tmp_path):
        root = tmp_path / "trash"
        root.mkdir(parents=True)
        # A file that sits next to (not under) the trash root, deliberately
        # made to look "old" — sweep_trash must never reach it.
        sibling = tmp_path / "sibling.mkv"
        sibling.write_text("keep me")
        import time as _time
        old_time = _time.time() - 400 * 86400
        os.utime(str(sibling), (old_time, old_time))

        fileops.sweep_trash(30, roots=[str(root)])

        assert sibling.exists()
        assert sibling.read_text() == "keep me"

    def test_does_not_follow_symlinks(self, tmp_path):
        root = tmp_path / "trash"
        old_bucket, _ = self._make_bucket(root, age_days=400)
        # A real file elsewhere, symlinked into the old bucket.
        target = tmp_path / "outside.mkv"
        target.write_text("do not delete")
        link = old_bucket / "linked.mkv"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported in this environment")

        fileops.sweep_trash(30, roots=[str(root)])

        # The symlink target must survive even though the bucket was swept.
        assert target.exists()
        assert target.read_text() == "do not delete"
