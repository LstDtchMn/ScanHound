"""Unit tests for the ported rename core: confidence, naming, fileops."""
import errno
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
        monkeypatch.setattr(fileops, "_move_no_replace", _boom)

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
        # First publication reports EXDEV; the verified-copy publication then
        # crashes. Source bytes must remain recoverable throughout.
        calls = 0

        def _exdev_then_crash(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                import errno as _e
                raise OSError(_e.EXDEV, "cross-device")
            raise OSError("crash")

        monkeypatch.setattr(fileops, "_move_no_replace", _exdev_then_crash)
        with pytest.raises(OSError):
            fileops.place_file(str(src), str(dst), "move")
        assert src.exists() and src.read_bytes() == b"q" * 8192   # NEVER lost
        assert not dst.exists()

    def test_refuses_to_overwrite(self, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("a")
        dst = tmp_path / "out.mkv"; dst.write_text("existing")
        with pytest.raises(FileExistsError):
            fileops.place_file(str(src), str(dst), "move")



    def test_move_publish_race_preserves_competing_destination(
            self, tmp_path, monkeypatch):
        """A destination created after the precheck must never be replaced."""
        src = tmp_path / "src.mkv"
        src.write_bytes(b"source")
        dst = tmp_path / "lib" / "out.mkv"
        real_publish = fileops._move_no_replace

        def competing_publish(source, destination):
            dst.write_bytes(b"victim")
            return real_publish(source, destination)

        monkeypatch.setattr(fileops, "_move_no_replace", competing_publish)

        with pytest.raises(FileExistsError):
            fileops.place_file(str(src), str(dst), "move")

        assert src.read_bytes() == b"source"
        assert dst.read_bytes() == b"victim"


    def test_copy_publish_race_preserves_competing_destination(
            self, tmp_path, monkeypatch):
        """Verified-copy publication is no-replace, not os.replace."""
        src = tmp_path / "src.mkv"
        src.write_bytes(b"source" * 4096)
        dst = tmp_path / "lib" / "out.mkv"
        real_publish = fileops._move_no_replace

        def competing_publish(source, destination):
            dst.write_bytes(b"victim")
            return real_publish(source, destination)

        monkeypatch.setattr(fileops, "_move_no_replace", competing_publish)

        with pytest.raises(FileExistsError):
            fileops.place_file(str(src), str(dst), "copy")

        assert src.read_bytes() == b"source" * 4096
        assert dst.read_bytes() == b"victim"
        assert not list(dst.parent.glob(f".{dst.name}.part.*"))


    def test_cross_device_hardlink_fallback_cannot_replace_racing_destination(
            self, tmp_path, monkeypatch):
        """The default hardlink-to-copy path keeps a concurrent victim intact."""
        import errno as _errno

        src = tmp_path / "src.mkv"
        src.write_bytes(b"source" * 4096)
        dst = tmp_path / "lib" / "out.mkv"
        real_link = fileops.os.link
        real_publish = fileops._move_no_replace

        def source_link_is_cross_device(source, destination, *args, **kwargs):
            if os.fspath(source) == str(src):
                raise OSError(_errno.EXDEV, "cross-device")
            return real_link(source, destination, *args, **kwargs)

        def competing_publish(source, destination):
            dst.write_bytes(b"victim")
            return real_publish(source, destination)

        monkeypatch.setattr(fileops.os, "link", source_link_is_cross_device)
        monkeypatch.setattr(fileops, "_move_no_replace", competing_publish)

        with pytest.raises(FileExistsError):
            fileops.place_file(str(src), str(dst), "hardlink")

        assert src.read_bytes() == b"source" * 4096
        assert dst.read_bytes() == b"victim"
        assert not list(dst.parent.glob(f".{dst.name}.part.*"))

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

        # The corrected Linux move path no longer calls os.rename.  Inject
        # EXDEV at the publication abstraction for the first (move) attempt,
        # then allow the verified-copy publication to use the real primitive.
        real_publish = fileops._move_no_replace
        publish_calls = 0

        def _first_publish_exdev_then_real(src_path, dst_path):
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 1:
                raise OSError(_errno.EXDEV, "Invalid cross-device link")
            return real_publish(src_path, dst_path)

        monkeypatch.setattr(
            fileops, "_move_no_replace", _first_publish_exdev_then_real
        )
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

        # The corrected Linux move path no longer calls os.rename.  Inject
        # EXDEV at the publication abstraction for the first (move) attempt,
        # then allow the verified-copy publication to use the real primitive.
        real_publish = fileops._move_no_replace
        publish_calls = 0

        def _first_publish_exdev_then_real(src_path, dst_path):
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 1:
                raise OSError(_errno.EXDEV, "Invalid cross-device link")
            return real_publish(src_path, dst_path)

        monkeypatch.setattr(
            fileops, "_move_no_replace", _first_publish_exdev_then_real
        )
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

    def test_trash_root_for_posix_sites_bucket_on_source_device(self, tmp_path):
        """On POSIX (no drive anchor — the Docker deployment) the trash root
        must sit on the SOURCE's own volume so disposal is an instant same-device
        rename, not an EXDEV copy of the whole media file into /data. We assert
        the bucket's parent shares the source's st_dev."""
        if os.name == "nt":
            pytest.skip("POSIX mount-walk behaviour; drive-anchor path covered above")
        src = tmp_path / "movie.mkv"; src.write_text("x")
        root = fileops._trash_root_for(str(src))
        assert os.path.basename(root) == ".scanhound-trash"
        # The bucket's mount point is on the same device as the source.
        assert os.stat(os.path.dirname(root)).st_dev == os.stat(str(src)).st_dev

    def test_trash_root_for_falls_back_when_source_unstattable(self, monkeypatch):
        """If the source's device can't be determined (stat fails), fall back to
        the module-level _TRASH_ROOT rather than raising."""
        monkeypatch.setattr(fileops.os.path, "splitdrive", lambda p: ("", p))
        monkeypatch.setattr(fileops.os, "stat",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no dev")))
        assert fileops._trash_root_for("/nonexistent/movie.mkv") == fileops._TRASH_ROOT

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
        # Landed under one of the implementation's same-volume roots, not
        # app-data. A non-root process may be unable to create the mount-root
        # candidate and must then use a writable ancestor on the same device.
        same_volume_roots = fileops._same_volume_trash_roots(str(f))
        actual_root = next(
            root for root in same_volume_roots
            if os.path.commonpath([root, trashed_path]) == root
        )
        assert os.path.basename(actual_root) == ".scanhound-trash"
        assert os.stat(os.path.dirname(actual_root)).st_dev == os.stat(trashed_path).st_dev
        assert actual_root != fileops._TRASH_ROOT
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

    def test_trash_uses_writable_same_volume_ancestor_before_appdata(
            self, tmp_path, monkeypatch):
        """A denied volume-root candidate must not force a cross-device copy."""
        library = tmp_path / "library"
        library.mkdir()
        source = library / "movie.mkv"
        source.write_text("x")

        blocked = tmp_path / "blocked" / ".scanhound-trash"
        writable = library / ".scanhound-trash"
        monkeypatch.setattr(
            fileops,
            "_same_volume_trash_roots",
            lambda _path: [str(blocked), str(writable)],
        )

        real_makedirs = fileops.os.makedirs

        def guarded_makedirs(path, *args, **kwargs):
            if os.path.commonpath([str(blocked), str(path)]) == str(blocked):
                raise PermissionError("volume root denied")
            return real_makedirs(path, *args, **kwargs)

        cross_device_moves = []
        monkeypatch.setattr(fileops.os, "makedirs", guarded_makedirs)
        monkeypatch.setattr(
            fileops.shutil,
            "move",
            lambda *args, **kwargs: cross_device_moves.append((args, kwargs)),
        )

        trashed = fileops._trash(str(source))

        assert os.path.commonpath([str(writable), trashed]) == str(writable)
        assert os.path.isfile(trashed)
        assert not cross_device_moves

    def test_trash_exdev_never_cascades_to_appdata(
            self, tmp_path, monkeypatch):
        """EXDEV may require copying, but the copy stays on the selected volume."""
        import errno
        source = tmp_path / "movie.mkv"
        source.write_text("x")
        # SH-R03 only accepts non-intrinsic roots that can be safely persisted
        # and rediscovered. Use the real root shape rather than an arbitrary
        # test-only directory name.
        same_volume = tmp_path / ".scanhound-trash"
        appdata = tmp_path / "appdata-trash"
        index = tmp_path / "trash_roots.json"

        monkeypatch.setattr(
            fileops,
            "_same_volume_trash_roots",
            lambda _path: [str(same_volume)],
        )
        monkeypatch.setattr(fileops, "_TRASH_ROOT", str(appdata))
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(index))
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_RUNTIME", set())
        monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])

        real_move_no_replace = fileops._move_no_replace
        calls = 0

        def first_exdev_then_publish(source_path, destination_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EXDEV, "mount boundary")
            return real_move_no_replace(source_path, destination_path)

        monkeypatch.setattr(
            fileops,
            "_move_no_replace",
            first_exdev_then_publish,
        )

        trashed = fileops._trash(str(source))

        assert calls >= 2  # direct move failed; verified-copy publication ran
        assert os.path.commonpath([str(same_volume), trashed]) == str(same_volume)
        assert not os.path.exists(source)
        assert os.path.isfile(trashed)
        assert not appdata.exists()

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

    def test_trash_manifest_write_failure_leaves_source_in_place(
            self, tmp_path, monkeypatch):
        """SH-R03: restore metadata failure must abort before source movement."""
        trash_root = tmp_path / "appdata" / "trash"
        monkeypatch.setattr(fileops, "_TRASH_ROOT", str(trash_root))
        monkeypatch.setattr(fileops, "_same_volume_trash_roots", lambda _path: [])
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_RUNTIME", set())
        f = tmp_path / "movie.mkv"
        f.write_text("bye")

        monkeypatch.setattr(
            fileops.json,
            "dump",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            fileops._trash(str(f))
        assert f.read_text() == "bye"
        assert fileops.list_trash_entries([str(trash_root)]) == []


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

    def test_delete_removes_file_and_manifest_record(self, tmp_path, monkeypatch):
        root = tmp_path / "trash"
        trashed_path, original = self._trash_one(
            tmp_path, monkeypatch, root, "20260101-000000", "movie.mkv", content="xyz")

        result = fileops.delete_trash_entry("20260101-000000", "movie.mkv", [str(root)])
        assert result["ok"] is True
        assert result["bytes_freed"] == 3
        assert not os.path.exists(trashed_path)
        # The original stays gone — delete is the opposite of restore.
        assert not original.exists()
        assert fileops.list_trash_entries([str(root)]) == []
        # Last file gone -> the whole dated bucket goes too, manifest included.
        assert not os.path.isdir(os.path.dirname(trashed_path))

    def test_delete_keeps_bucket_while_siblings_remain(self, tmp_path, monkeypatch):
        root = tmp_path / "trash"
        self._trash_one(tmp_path, monkeypatch, root, "20260101-000000", "a.mkv")
        trashed_b, _ = self._trash_one(
            tmp_path, monkeypatch, root, "20260101-000000", "b.mkv")

        assert fileops.delete_trash_entry("20260101-000000", "a.mkv", [str(root)])["ok"]
        remaining = fileops.list_trash_entries([str(root)])
        assert [e["name"] for e in remaining] == ["b.mkv"]
        # The surviving sibling keeps its restorable manifest record.
        assert remaining[0]["restorable"] is True
        assert os.path.isfile(trashed_b)

    def test_delete_works_on_manifest_less_entry(self, tmp_path):
        # restore_trash_entry refuses these (nowhere safe to put them back) —
        # delete must NOT, since an unrestorable entry is exactly the one a
        # user needs a way to get rid of.
        root = tmp_path / "trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "orphan.mkv").write_text("orphan")

        result = fileops.delete_trash_entry("20260101-000000", "orphan.mkv", [str(root)])
        assert result["ok"] is True
        assert fileops.list_trash_entries([str(root)]) == []

    def test_delete_rejects_path_traversal(self, tmp_path):
        root = tmp_path / "trash"
        root.mkdir()
        outsider = tmp_path / "keepme.mkv"
        outsider.write_text("keep")

        result = fileops.delete_trash_entry("..", "keepme.mkv", [str(root)])
        assert result["ok"] is False
        assert "traversal" in result["error"].lower()
        assert outsider.exists()

    def test_delete_missing_entry_reports_not_found(self, tmp_path):
        root = tmp_path / "trash"
        root.mkdir()
        result = fileops.delete_trash_entry("20260101-999999", "ghost.mkv", [str(root)])
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_empty_trash_removes_everything_regardless_of_age(self, tmp_path, monkeypatch):
        # The bucket name is today's date, so a normal 30-day sweep would skip
        # it entirely — empty_trash must take it anyway.
        root = tmp_path / "trash"
        import datetime as _dt
        fresh = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._trash_one(tmp_path, monkeypatch, root, fresh, "new.mkv", content="ab")

        assert fileops.sweep_trash(30, roots=[str(root)])["files_deleted"] == 0

        summary = fileops.empty_trash(roots=[str(root)])
        assert summary["files_deleted"] == 1
        assert summary["bytes_freed"] == 2
        assert summary["buckets_removed"] == 1
        assert fileops.list_trash_entries([str(root)]) == []

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


class TestAllTrashRoots:
    """all_trash_roots() (POSIX) must enumerate every mount point, not just /."""

    def test_includes_scanhound_trash_for_every_mount_point(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(
            fileops, "_posix_mount_points",
            lambda: ["/", "/library/movies", "/library/tv"],
        )
        roots = fileops.all_trash_roots()
        assert os.path.join("/library/movies", ".scanhound-trash") in roots
        assert os.path.join("/library/tv", ".scanhound-trash") in roots
        assert os.path.join("/", ".scanhound-trash") in roots
        assert os.path.abspath(fileops._TRASH_ROOT) in roots

    def test_malformed_or_empty_mount_source_never_raises(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])
        roots = fileops.all_trash_roots()
        assert os.path.abspath(fileops._TRASH_ROOT) in roots
        assert os.path.join("/", ".scanhound-trash") in roots

    def test_wiring_makes_trash_on_a_reported_mount_discoverable(self, tmp_path, monkeypatch):
        """Regression for SH-H05: a file trashed under a mount point that
        isn't '/' must be findable via list_trash_entries(all_trash_roots())."""
        mount = tmp_path / "library_movies"
        mount.mkdir()
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [str(mount)])
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(mount / ".scanhound-trash"))

        src = mount / "movie.mkv"
        src.write_text("x")
        fileops._trash(str(src))

        entries = fileops.list_trash_entries(fileops.all_trash_roots())
        names = {e["name"] for e in entries}
        assert "movie.mkv" in names


    def test_deeper_fallback_root_is_globally_discoverable_and_restorable(
            self, tmp_path, monkeypatch):
        """A non-mount-root placement must be visible to path-independent APIs."""
        import json as _json

        library = tmp_path / "library"
        library.mkdir()
        source = library / "movie.mkv"
        source.write_text("payload")

        deeper_root = library / ".scanhound-trash"
        fallback_root = tmp_path / "appdata-trash"
        index_path = tmp_path / "trash_roots.json"
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(index_path))
        monkeypatch.setattr(fileops, "_TRASH_ROOT", str(fallback_root))
        # Give this test a private process-local registry so root/UID test runs
        # and pre-existing /.scanhound-trash state cannot leak into the result.
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_RUNTIME", set())
        monkeypatch.setattr(
            fileops,
            "_same_volume_trash_roots",
            lambda _path: [str(deeper_root)],
        )
        monkeypatch.setattr(
            fileops,
            "_trash_bucket_name",
            lambda: "20260101-000000",
        )
        monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])
        monkeypatch.setattr(
            fileops, "_trash_root_for", lambda _path: str(fallback_root)
        )

        trashed = fileops._trash(str(source))

        assert index_path.is_file()
        payload = _json.loads(index_path.read_text())
        assert str(deeper_root) in payload["roots"]

        # Simulate a restart by dropping the process-local safety set.
        fileops._TRASH_ROOTS_RUNTIME.clear()
        global_roots = fileops.all_trash_roots()
        assert str(deeper_root) in global_roots
        entries = fileops.list_trash_entries(global_roots)
        assert [entry["name"] for entry in entries] == ["movie.mkv"]

        restored = fileops.restore_trash_entry(
            "20260101-000000",
            "movie.mkv",
            global_roots,
        )
        assert restored["ok"] is True
        assert source.read_text() == "payload"
        assert not os.path.exists(trashed)

    def test_registry_write_failure_keeps_same_process_restore_visible(
            self, tmp_path, monkeypatch):
        """A transient index failure cannot break immediate overwrite rollback."""
        deeper_root = tmp_path / "library" / ".scanhound-trash"
        index_path = tmp_path / "trash_roots.json"
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(index_path))
        monkeypatch.setattr(
            fileops.os,
            "replace",
            lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(OSError("index unavailable")),
        )
        monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])

        fileops._record_trash_root(str(deeper_root))

        assert str(deeper_root) in fileops.all_trash_roots()

    def test_registered_root_index_rejects_arbitrary_paths(
            self, tmp_path, monkeypatch):
        """A corrupt index cannot turn global trash operations into arbitrary I/O."""
        import json as _json

        safe_root = tmp_path / "library" / ".scanhound-trash"
        unsafe_root = tmp_path / "ordinary-directory"
        index_path = tmp_path / "trash_roots.json"
        index_path.write_text(_json.dumps({
            "version": 1,
            "roots": [str(safe_root), str(unsafe_root), "", None],
        }))
        monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(index_path))
        monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])

        roots = fileops.all_trash_roots()

        assert str(safe_root) in roots
        assert str(unsafe_root) not in roots


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

class TestFilesystemFailSafe:
    def test_unsupported_filesystem_refuses_move_before_source_consumption(
            self, tmp_path, monkeypatch):
        src = tmp_path / "source" / "src.mkv"
        src.parent.mkdir()
        src.write_bytes(b"source-bytes")
        dst = tmp_path / "library" / "dst.mkv"

        monkeypatch.setattr(fileops, "_linux_rename_noreplace", lambda *_: False)
        monkeypatch.setattr(
            fileops.os,
            "link",
            lambda *_a, **_k: (_ for _ in ()).throw(
                OSError(errno.EXDEV, "simulated cross-device hardlink")
            ),
        )
        monkeypatch.setattr(
            fileops,
            "_fsync_directory",
            lambda *_: (_ for _ in ()).throw(
                OSError(errno.EINVAL, "simulated unsupported directory fsync")
            ),
        )

        with pytest.raises(fileops.UnsupportedFilesystemSafetyError) as caught:
            fileops.place_file(str(src), str(dst), "move")

        assert caught.value.reason.startswith("directory fsync unavailable")
        assert src.read_bytes() == b"source-bytes"
        assert not dst.exists()

    def test_post_publish_directory_sync_failure_rolls_back_move(
            self, tmp_path, monkeypatch):
        src = tmp_path / "source" / "src.mkv"
        src.parent.mkdir()
        src.write_bytes(b"source-bytes")
        dst = tmp_path / "library" / "dst.mkv"
        dst.parent.mkdir()

        real_sync = fileops._fsync_directory
        calls = 0

        def fail_after_preflight(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(errno.EIO, "simulated post-publication sync failure")
            return real_sync(path)

        monkeypatch.setattr(fileops, "_fsync_directory", fail_after_preflight)

        with pytest.raises(OSError, match="post-publication"):
            fileops.place_file(str(src), str(dst), "move")

        assert src.read_bytes() == b"source-bytes"
        assert not dst.exists()

    def test_copy_directory_sync_failure_removes_destination_and_keeps_source(
            self, tmp_path, monkeypatch):
        src = tmp_path / "src.mkv"
        src.write_bytes(b"copy-source")
        dst = tmp_path / "library" / "dst.mkv"
        dst.parent.mkdir()

        monkeypatch.setattr(
            fileops,
            "_fsync_directory",
            lambda *_: (_ for _ in ()).throw(
                OSError(errno.EINVAL, "simulated unsupported directory fsync")
            ),
        )

        with pytest.raises(OSError, match="directory fsync"):
            fileops.place_file(str(src), str(dst), "copy")

        assert src.read_bytes() == b"copy-source"
        assert not dst.exists()

    def test_filesystem_safety_status_reports_unsupported_directory_sync(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            fileops,
            "_fsync_directory",
            lambda *_: (_ for _ in ()).throw(
                OSError(errno.ENOTSUP, "simulated unsupported")
            ),
        )
        status = fileops.filesystem_safety_status(str(tmp_path))
        assert status["directory_fsync"] is False
        assert status["source_consuming_move_durability"] is False
