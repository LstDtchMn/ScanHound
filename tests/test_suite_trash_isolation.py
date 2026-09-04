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


def test_default_root_mutators_never_reach_a_real_posix_mount(monkeypatch):
    """R8-TST1-1 regression.

    Before the fix, all_trash_roots() built "<mount>/.scanhound-trash" for
    every _posix_mount_points() entry DIRECTLY, bypassing the redirected
    _trash_root_for -- so repair_trash_transactions()/sweep_trash()/
    empty_trash(), called with their DEFAULT roots=None, could reach a real
    per-mount trash root on a POSIX host. Simulate POSIX with a sentinel
    mount OUTSIDE tmp_path and prove: (1) the isolated all_trash_roots()
    itself never surfaces it, and (2) none of the three mutators, called with
    defaults, ever probes that path.

    repair_trash_transactions()/sweep_trash() call os.path.abspath(root)
    BEFORE os.path.isdir(root), so on Windows the probed path is the
    abspath'd form (e.g. "C:\\sentinel-real-mount\\.scanhound-trash"), never
    equal to the bare sentinel_root -- both forms are checked here.
    """
    sentinel_mount = "/sentinel-real-mount"
    sentinel_root = os.path.normpath(os.path.join(sentinel_mount, ".scanhound-trash"))
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [sentinel_mount])

    roots = fileops.all_trash_roots()
    assert sentinel_root not in {os.path.normpath(r) for r in roots}

    real_isdir = os.path.isdir
    probed = []

    def _recording_isdir(path):
        probed.append(path)
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _recording_isdir)

    fileops.repair_trash_transactions()
    fileops.sweep_trash(1)
    fileops.empty_trash()

    assert probed, "the spy was never exercised"
    sentinel_probes = {
        os.path.normpath(sentinel_root),
        os.path.normpath(os.path.abspath(sentinel_root)),
    }
    assert not any(os.path.normpath(p) in sentinel_probes for p in probed)


@pytest.mark.real_trash_root
def test_the_real_derivation_would_have_surfaced_the_sentinel_mount(monkeypatch):
    """Proves the previous test's isolation is doing real work: WITHOUT the
    redirect (opted out via the marker), the real all_trash_roots() DOES add
    the sentinel mount's trash root. Reads only; calls no mutator."""
    sentinel_mount = "/sentinel-real-mount"
    expected = os.path.normpath(os.path.join(sentinel_mount, ".scanhound-trash"))
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [sentinel_mount])

    roots = fileops.all_trash_roots()
    assert expected in {os.path.normpath(r) for r in roots}


@pytest.mark.real_trash_root
def test_a_marked_test_that_calls_a_trash_mutator_raises(tmp_path):
    """R8-TST1-3: a real_trash_root test is derivation-only by default; if it
    calls a mutating entry point anyway, that call raises instead of quietly
    touching a real root.

    The argument is a throwaway tmp file on purpose. If the raise were ever
    missing, the real derivation would move the argument into the REAL volume
    root (the guard then names this test); an earlier version passed
    ``__file__`` and, under exactly that mutant, trashed its own module."""
    doomed = tmp_path / "doomed.txt"
    doomed.write_text("x")
    with pytest.raises(RuntimeError, match="derivation-only"):
        fileops._trash(str(doomed))
    assert doomed.exists()


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

    def test_an_in_place_rewrite_with_a_different_size_is_caught(self, tmp_path):
        """R8-TST1-2: the snapshot records (name, type, size) one level inside
        a bucket, so an in-place rewrite of a trashed file that changes its
        size is caught even though the file's NAME never changed."""
        root = tmp_path / ".scanhound-trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "movie.mkv").write_text("short")
        before = conftest._snapshot_trash_roots([str(root)])
        (bucket / "movie.mkv").write_text("a much longer replacement payload")
        after = conftest._snapshot_trash_roots([str(root)])
        assert "modified=['20260101-000000']" in conftest._describe_trash_root_changes(before, after)

    def test_a_manifest_rewrite_with_the_same_size_is_caught_by_digest(self, tmp_path):
        """R8-TST1-2: manifest.json additionally carries a sha256 digest, so a
        same-size rewrite that changes its bytes is still caught -- the one
        case (name, type, size) alone would miss."""
        root = tmp_path / ".scanhound-trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        original = '{"a": 1}'
        replacement = '{"a": 2}'
        assert len(original) == len(replacement)
        (bucket / "manifest.json").write_text(original)
        before = conftest._snapshot_trash_roots([str(root)])
        (bucket / "manifest.json").write_text(replacement)
        after = conftest._snapshot_trash_roots([str(root)])
        assert "modified=['20260101-000000']" in conftest._describe_trash_root_changes(before, after)

    def test_a_same_size_same_bytes_media_rewrite_is_not_caught_documented_limit(self, tmp_path):
        """R8-TST1-2's documented, deliberate limit: an ordinary trashed file
        (not manifest.json) is recorded as (name, type, size) only -- no
        content hash. A rewrite that keeps the exact same bytes and size is
        therefore invisible to the guard. Only manifest.json is hashed."""
        root = tmp_path / ".scanhound-trash"
        bucket = root / "20260101-000000"
        bucket.mkdir(parents=True)
        (bucket / "movie.mkv").write_bytes(b"same-payload")
        before = conftest._snapshot_trash_roots([str(root)])
        (bucket / "movie.mkv").write_bytes(b"same-payload")
        after = conftest._snapshot_trash_roots([str(root)])
        assert conftest._describe_trash_root_changes(before, after) == ""
