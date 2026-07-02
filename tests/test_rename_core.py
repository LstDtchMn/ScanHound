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
