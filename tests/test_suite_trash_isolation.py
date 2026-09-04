"""TST-1 (round-7 review): the suite never writes into a REAL volume trash root.

The production derivation sites a trash bucket at the root of the source's own
volume, so a test that trashed a file under tmp_path wrote into the host's real
C:\\.scanhound-trash. conftest now redirects the derivation into tmp_path for
every test and guards every real root before/after each test and the session.
These tests pin the redirect and the guard's discrimination. The guard's own
failure was shown on a copy with the redirect removed (see the PR): the test
that trashes below then errors at teardown, naming itself and the real root.
"""
import os

import pytest

import tests.conftest as conftest
from backend.rename import fileops


def test_a_trashed_tmp_file_lands_under_tmp_path_not_the_real_volume_root(tmp_path):
    src = tmp_path / "library" / "doomed.mkv"
    src.parent.mkdir()
    src.write_text("bye")
    real_root = conftest._REAL_TRASH_ROOT_FOR(str(src))

    trashed = fileops._trash(str(src))

    assert not src.exists()
    assert os.path.isfile(trashed)
    assert os.path.commonpath([str(tmp_path), trashed]) == str(tmp_path)
    assert os.path.commonpath([real_root, trashed]) != real_root
    # the real root is a volume root candidate, never anything under tmp_path
    assert os.path.commonpath([str(tmp_path), real_root]) != str(tmp_path)


def test_the_appdata_fallback_root_is_redirected_too(tmp_path):
    assert os.path.commonpath([str(tmp_path), fileops._TRASH_ROOT]) == str(tmp_path)


def test_every_real_volume_root_is_watched():
    roots = conftest._real_volume_trash_roots()
    assert roots, "no volume roots found to guard"
    for root in roots:
        assert os.path.basename(root) == ".scanhound-trash"
        assert os.path.isabs(root)
        # never anything the redirect could have produced
        assert "pytest-of-" not in root
        if os.name == "nt":
            # the drive-anchor derivation is dirname-insensitive; round-trip it.
            # (The POSIX derivation starts its walk one level ABOVE the path it
            # is given, so a round-trip through a mount point is not defined.)
            assert root == conftest._REAL_TRASH_ROOT_FOR(os.path.dirname(root))


@pytest.mark.real_trash_root
def test_the_real_ancestor_walk_is_still_covered_derivation_only(tmp_path):
    """Under the redirect _same_volume_trash_roots is pinned, so the real walk
    keeps its coverage here: primary is the volume root, and every ancestor of
    the source down to tmp_path contributes a deeper candidate. Nothing is
    created."""
    src = tmp_path / "library" / "movie.mkv"
    src.parent.mkdir()
    src.write_text("x")
    roots = fileops._same_volume_trash_roots(str(src))
    assert roots[0] == conftest._REAL_TRASH_ROOT_FOR(str(src))
    assert str(src.parent / ".scanhound-trash") in roots
    assert str(tmp_path / ".scanhound-trash") in roots
    assert all(os.path.basename(r) == ".scanhound-trash" for r in roots)


@pytest.mark.real_trash_root
def test_the_opt_out_marker_restores_the_real_derivation(tmp_path):
    src = tmp_path / "movie.mkv"
    assert fileops._trash_root_for(str(src)) == conftest._REAL_TRASH_ROOT_FOR(str(src))


class TestGuardDiscrimination:
    def test_no_change_is_silent(self, tmp_path):
        root = tmp_path / ".scanhound-trash"
        (root / "20260101-000000").mkdir(parents=True)
        before = conftest._snapshot_trash_roots([str(root)])
        after = conftest._snapshot_trash_roots([str(root)])
        assert conftest._describe_trash_root_changes(before, after) == ""

    def test_a_new_bucket_is_named(self, tmp_path):
        root = tmp_path / ".scanhound-trash"
        root.mkdir()
        before = conftest._snapshot_trash_roots([str(root)])
        (root / "20260903-221500").mkdir()
        after = conftest._snapshot_trash_roots([str(root)])
        diff = conftest._describe_trash_root_changes(before, after)
        assert "20260903-221500" in diff and str(root) in diff

    def test_a_file_added_inside_an_existing_bucket_is_caught(self, tmp_path):
        """Two disposals in one second share a bucket, so a write INTO an
        existing bucket is a real case. The snapshot lists one level inside
        each bucket; the bucket's mtime is not relied on (it does not move for
        this on the Windows development host)."""
        root = tmp_path / ".scanhound-trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "early.mkv").write_text("x")
        before = conftest._snapshot_trash_roots([str(root)])
        (bucket / "late.mkv").write_text("x")
        after = conftest._snapshot_trash_roots([str(root)])
        assert "modified=['20260101-000000']" in conftest._describe_trash_root_changes(before, after)

    def test_a_file_removed_from_a_bucket_is_caught(self, tmp_path):
        root = tmp_path / ".scanhound-trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "gone.mkv").write_text("x")
        before = conftest._snapshot_trash_roots([str(root)])
        (bucket / "gone.mkv").unlink()
        after = conftest._snapshot_trash_roots([str(root)])
        assert "modified=['20260101-000000']" in conftest._describe_trash_root_changes(before, after)

    def test_a_root_that_appears_is_caught(self, tmp_path):
        root = tmp_path / ".scanhound-trash"
        before = conftest._snapshot_trash_roots([str(root)])
        root.mkdir()
        after = conftest._snapshot_trash_roots([str(root)])
        assert "None -> {}" in conftest._describe_trash_root_changes(before, after)
