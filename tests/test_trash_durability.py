"""Adversarial durability tests for SH-R03 trash transactions."""

import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.database import DatabaseManager
from backend.rename import fileops
from backend.rename.service import RenameService


@pytest.fixture
def isolated_trash(tmp_path, monkeypatch):
    root = tmp_path / "trash"
    monkeypatch.setattr(fileops, "_TRASH_ROOT", str(root))
    monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(tmp_path / "trash_roots.json"))
    monkeypatch.setattr(fileops, "_TRASH_ROOTS_RUNTIME", set())
    monkeypatch.setattr(fileops, "_same_volume_trash_roots", lambda _path: [])
    monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])
    monkeypatch.setattr(fileops, "_trash_root_for", lambda _path: str(root))
    monkeypatch.setattr(fileops, "_trash_bucket_name", lambda: "20260101-000000")
    return root


def _source(tmp_path, name="movie.mkv", data=b"payload"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_corrupt_manifest_open_failure_keeps_source(tmp_path, isolated_trash, monkeypatch):
    bucket = isolated_trash / "20260101-000000"
    bucket.mkdir(parents=True)
    (bucket / "manifest.json").write_text("{broken")
    src = _source(tmp_path)

    with pytest.raises(OSError, match="unreadable or corrupt"):
        fileops._trash(str(src))

    assert src.read_bytes() == b"payload"
    assert not list(bucket.glob("*.mkv"))


def test_manifest_write_failure_keeps_source(tmp_path, isolated_trash, monkeypatch):
    src = _source(tmp_path)
    monkeypatch.setattr(
        fileops.json,
        "dump",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        fileops._trash(str(src))
    assert src.read_bytes() == b"payload"


def test_directory_fsync_failure_keeps_source(tmp_path, isolated_trash, monkeypatch):
    src = _source(tmp_path)
    monkeypatch.setattr(
        fileops,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("fsync failed")),
    )
    with pytest.raises(OSError, match="fsync failed"):
        fileops._trash(str(src))
    assert src.read_bytes() == b"payload"


def test_manifest_atomic_replace_failure_keeps_source(
        tmp_path, isolated_trash, monkeypatch):
    src = _source(tmp_path)
    real_replace = fileops.os.replace

    def fail_manifest_replace(source, destination):
        if os.path.basename(destination) == "manifest.json":
            raise OSError("replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(fileops.os, "replace", fail_manifest_replace)
    with pytest.raises(OSError, match="replace failed"):
        fileops._trash(str(src))
    assert src.read_bytes() == b"payload"


def test_move_failure_after_metadata_preparation_keeps_source_and_hides_record(
        tmp_path, isolated_trash, monkeypatch):
    src = _source(tmp_path)
    monkeypatch.setattr(
        fileops,
        "_move_no_replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EIO, "move failed")),
    )
    with pytest.raises(OSError):
        fileops._trash(str(src))
    assert src.read_bytes() == b"payload"
    assert fileops.list_trash_entries([str(isolated_trash)]) == []


def test_pre_move_crash_window_is_metadata_only_and_not_listed(
        tmp_path, isolated_trash):
    src = _source(tmp_path)
    bucket, dst, reservation_id = fileops._prepare_trash_destination(
        str(isolated_trash), "20260101-000000", str(src)
    )
    assert src.exists()
    assert not os.path.exists(dst)
    assert fileops.list_trash_entries([str(isolated_trash)]) == []
    fileops._cleanup_prepared_trash(bucket, dst, reservation_id)


def test_post_move_restart_can_list_and_restore(tmp_path, isolated_trash):
    src = _source(tmp_path)
    trashed = fileops._trash(str(src))
    fileops._TRASH_ROOTS_RUNTIME.clear()

    entries = fileops.list_trash_entries(fileops.all_trash_roots())
    entry = next(item for item in entries if item["name"] == os.path.basename(trashed))
    assert entry["restorable"] is True
    restored = fileops.restore_trash_entry(
        entry["bucket"], entry["name"], fileops.all_trash_roots())
    assert restored["ok"] is True
    assert src.read_bytes() == b"payload"


def test_two_concurrent_trash_operations_preserve_both_records(
        tmp_path, isolated_trash):
    first = _source(tmp_path, "one/movie.mkv", b"one")
    second = _source(tmp_path, "two/movie.mkv", b"two")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fileops._trash, (str(first), str(second))))

    assert len(set(results)) == 2
    entries = fileops.list_trash_entries([str(isolated_trash)])
    assert len(entries) == 2
    assert {open(path, "rb").read() for path in results} == {b"one", b"two"}
    manifest = json.loads(
        (isolated_trash / "20260101-000000" / "manifest.json").read_text()
    )
    assert len(manifest) == 2
    assert len({record["reservation_id"] for record in manifest}) == 2


def test_required_deeper_root_index_failure_never_moves_source(
        tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    src = _source(library)
    deeper = library / ".scanhound-trash"
    fallback = tmp_path / "blocked-fallback"
    index = tmp_path / "trash_roots.json"
    monkeypatch.setattr(fileops, "_TRASH_ROOT", str(fallback))
    monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(index))
    monkeypatch.setattr(fileops, "_TRASH_ROOTS_RUNTIME", set())
    monkeypatch.setattr(fileops, "_same_volume_trash_roots", lambda _p: [str(deeper)])
    monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])
    monkeypatch.setattr(fileops, "_trash_root_for", lambda _p: str(fallback))

    real_replace = fileops.os.replace
    real_makedirs = fileops.os.makedirs

    def fail_index_replace(source, destination):
        if os.path.abspath(destination) == os.path.abspath(index):
            raise OSError("index unavailable")
        return real_replace(source, destination)

    def fail_fallback_create(path, *args, **kwargs):
        if os.path.commonpath([str(fallback), os.path.abspath(path)]) == str(fallback):
            raise PermissionError("fallback unavailable")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(fileops.os, "replace", fail_index_replace)
    monkeypatch.setattr(fileops.os, "makedirs", fail_fallback_create)

    with pytest.raises(OSError):
        fileops._trash(str(src))
    assert src.read_bytes() == b"payload"
    assert not list(deeper.rglob("*.mkv")) if deeper.exists() else True


class _FakeDb:
    def __init__(self, job, *, archive_error=None):
        self.job = job
        self.archive_error = archive_error
        self.archived = False
        self.updated = []

    def get_rename_job(self, _job_id):
        return dict(self.job)

    def archive_rename_jobs(self, _ids):
        if self.archive_error:
            raise self.archive_error
        self.archived = True
        return True

    def update_rename_job(self, _job_id, **kwargs):
        self.updated.append(kwargs)
        self.job.update(kwargs)
        return True


def _bare_service(db):
    class _Registry:
        def __init__(self, database):
            self.db = database
            self.config = {
                "auto_rename_move_method": "move",
                "deletions_require_confirmation": True,
            }

    service = RenameService(
        _Registry(db),
        tmdb_search=lambda *_args, **_kwargs: [],
    )
    service._broadcast = lambda *_args, **_kwargs: None
    return service


def test_resolve_keep_plex_trash_failure_is_not_success(tmp_path, monkeypatch):
    src = _source(tmp_path)
    db = _FakeDb({"id": 1, "original_path": str(src), "status": "needs_review"})
    service = _bare_service(db)
    monkeypatch.setattr(
        fileops,
        "_trash",
        lambda _path: (_ for _ in ()).throw(OSError("manifest unavailable")),
    )

    result = service.resolve_keep_plex(1)
    assert result["ok"] is False
    assert db.archived is False
    assert src.read_bytes() == b"payload"


def test_resolve_keep_plex_archive_failure_restores_download(
        tmp_path, isolated_trash):
    src = _source(tmp_path)
    db = _FakeDb(
        {"id": 1, "original_path": str(src), "status": "needs_review"},
        archive_error=OSError("db unavailable"),
    )
    service = _bare_service(db)

    result = service.resolve_keep_plex(1)
    assert result["ok"] is False
    assert src.read_bytes() == b"payload"
    assert fileops.list_trash_entries([str(isolated_trash)]) == []


def test_overwrite_trash_preparation_failure_changes_neither_file(
        tmp_path, monkeypatch):
    db = DatabaseManager()
    try:
        src = _source(tmp_path, "incoming.mkv", b"incoming")
        dest_dir = tmp_path / "library"
        dest_dir.mkdir()
        dst = dest_dir / "movie.mkv"
        dst.write_bytes(b"existing")
        job_id = db.create_rename_job({
            "original_path": str(src),
            "original_filename": src.name,
            "new_filename": dst.name,
            "destination_path": str(dest_dir),
            "status": "matched",
            "match_confidence": 100,
        })
        service = _bare_service(db)
        monkeypatch.setattr(
            fileops,
            "_trash",
            lambda _path: (_ for _ in ()).throw(OSError("manifest unavailable")),
        )

        result = service.apply(job_id, conflict_strategy="overwrite")
        assert result["ok"] is False
        assert src.read_bytes() == b"incoming"
        assert dst.read_bytes() == b"existing"
    finally:
        db.close()
