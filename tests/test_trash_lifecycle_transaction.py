"""Adversarial tests for SH-R04 restore/delete/sweep recovery."""
import errno
import json
import os

import pytest

import backend.app_service as app_service
from backend.rename import fileops


@pytest.fixture
def trash_env(tmp_path, monkeypatch):
    root = tmp_path / "trash"
    bucket = root / "20260101-000000"
    bucket.mkdir(parents=True)
    monkeypatch.setattr(fileops, "_TRASH_ROOT", str(root))
    monkeypatch.setattr(fileops, "_TRASH_ROOTS_INDEX", str(tmp_path / "roots.json"))
    monkeypatch.setattr(fileops, "_TRASH_ROOTS_RUNTIME", set())
    monkeypatch.setattr(fileops, "_posix_mount_points", lambda: [])
    return root, bucket


def _entry(bucket, tmp_path, *, name="movie.mkv", original=True):
    fpath = bucket / name
    fpath.write_bytes(b"payload")
    records = []
    original_path = str(tmp_path / "library" / name) if original else None
    if original:
        records.append({
            "reservation_id": "reservation",
            "trashed_name": name,
            "original_path": original_path,
            "trashed_at": "2026-01-01T00:00:00",
        })
    (bucket / "manifest.json").write_text(json.dumps(records), encoding="utf-8")
    return fpath, original_path


def test_restore_completion_failure_is_not_false_success(
        tmp_path, trash_env, monkeypatch):
    root, bucket = trash_env
    fpath, original_path = _entry(bucket, tmp_path)
    real_complete = fileops._complete_trash_operation
    monkeypatch.setattr(
        fileops,
        "_complete_trash_operation",
        lambda *_: (_ for _ in ()).throw(OSError("manifest unavailable")),
    )

    result = fileops.restore_trash_entry(
        bucket.name, fpath.name, [str(root)]
    )

    assert result["ok"] is False
    assert result["repair_required"] is True
    assert result["bytes_restored"] is True
    assert os.path.isfile(original_path)
    assert not fpath.exists()

    monkeypatch.setattr(fileops, "_complete_trash_operation", real_complete)
    repaired = fileops.repair_trash_transactions([str(root)])
    assert repaired["completed"] == 1
    assert repaired["repair_required"] == 0


def test_delete_completion_failure_is_not_false_success(
        tmp_path, trash_env, monkeypatch):
    root, bucket = trash_env
    fpath, _ = _entry(bucket, tmp_path)
    real_complete = fileops._complete_trash_operation
    monkeypatch.setattr(
        fileops,
        "_complete_trash_operation",
        lambda *_: (_ for _ in ()).throw(OSError("manifest unavailable")),
    )

    result = fileops.delete_trash_entry(bucket.name, fpath.name, [str(root)])

    assert result["ok"] is False
    assert result["repair_required"] is True
    assert result["file_deleted"] is True
    assert not fpath.exists()

    monkeypatch.setattr(fileops, "_complete_trash_operation", real_complete)
    repaired = fileops.repair_trash_transactions([str(root)])
    assert repaired["completed"] == 1
    assert fileops.list_trash_entries([str(root)]) == []


def test_repair_clears_restore_intent_when_move_never_started(
        tmp_path, trash_env):
    root, bucket = trash_env
    fpath, original_path = _entry(bucket, tmp_path)
    fileops._begin_trash_operation(
        str(bucket), fpath.name, "restore", original_path=original_path
    )

    repaired = fileops.repair_trash_transactions([str(root)])

    assert repaired["rolled_back"] == 1
    manifest = json.loads((bucket / "manifest.json").read_text())
    assert "pending_operation" not in manifest[0]
    assert fpath.exists()


def test_repair_removes_synthetic_delete_intent_when_unlink_never_started(
        tmp_path, trash_env):
    root, bucket = trash_env
    fpath, _ = _entry(bucket, tmp_path, original=False)
    fileops._begin_trash_operation(str(bucket), fpath.name, "delete")

    repaired = fileops.repair_trash_transactions([str(root)])

    assert repaired["rolled_back"] == 1
    assert fpath.exists()
    assert json.loads((bucket / "manifest.json").read_text()) == []


def test_restore_race_keeps_trash_and_competing_destination(
        tmp_path, trash_env, monkeypatch):
    root, bucket = trash_env
    fpath, original_path = _entry(bucket, tmp_path)

    def competing_destination(_src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as handle:
            handle.write(b"competitor")
        raise FileExistsError(errno.EEXIST, "exists", dst)

    monkeypatch.setattr(fileops, "_restore_no_replace", competing_destination)
    result = fileops.restore_trash_entry(
        bucket.name, fpath.name, [str(root)]
    )

    assert result["ok"] is False
    assert fpath.read_bytes() == b"payload"
    assert open(original_path, "rb").read() == b"competitor"
    manifest = json.loads((bucket / "manifest.json").read_text())
    assert "pending_operation" not in manifest[0]


def test_ambiguous_restore_state_remains_visible(tmp_path, trash_env):
    root, bucket = trash_env
    fpath, original_path = _entry(bucket, tmp_path)
    fileops._begin_trash_operation(
        str(bucket), fpath.name, "restore", original_path=original_path
    )
    os.makedirs(os.path.dirname(original_path), exist_ok=True)
    with open(original_path, "wb") as handle:
        handle.write(b"other")

    repaired = fileops.repair_trash_transactions([str(root)])

    assert repaired["repair_required"] == 1
    manifest = json.loads((bucket / "manifest.json").read_text())
    assert manifest[0]["operation_error"] == "both trash and destination exist"


def test_sweep_uses_transactional_delete(tmp_path, trash_env, monkeypatch):
    root, bucket = trash_env
    fpath, _ = _entry(bucket, tmp_path)
    monkeypatch.setattr(fileops, "_bucket_age_days", lambda _path: 99)
    real_complete = fileops._complete_trash_operation
    monkeypatch.setattr(
        fileops,
        "_complete_trash_operation",
        lambda *_: (_ for _ in ()).throw(OSError("manifest unavailable")),
    )

    result = fileops.sweep_trash(30, roots=[str(root)])

    assert result["files_deleted"] == 1
    assert result["repair_required"] == 1
    assert result["errors"]
    assert not fpath.exists()

    monkeypatch.setattr(fileops, "_complete_trash_operation", real_complete)
    repaired = fileops.repair_trash_transactions([str(root)])
    assert repaired["completed"] == 1


def test_maintenance_repairs_before_sweep(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fileops,
        "all_trash_roots",
        lambda: ["/synthetic-trash"],
    )
    monkeypatch.setattr(
        fileops,
        "repair_trash_transactions",
        lambda roots: calls.append(("repair", roots)) or {
            "intents_seen": 0,
            "completed": 0,
            "rolled_back": 0,
            "repair_required": 0,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        fileops,
        "sweep_trash",
        lambda days, roots: calls.append(("sweep", days, roots)) or {
            "files_deleted": 0,
            "bytes_freed": 0,
            "buckets_removed": 0,
            "repair_required": 0,
            "errors": [],
        },
    )

    service = app_service.AppService.__new__(app_service.AppService)
    service.config = {
        "trash_retention_days": 30,
        "pipeline_reconcile_enabled": False,
        "rename_detect_moved_files_enabled": False,
        "dv_auto_sync_enabled": False,
    }
    service.db = None
    service._run_maintenance_pass()

    assert calls == [
        ("repair", ["/synthetic-trash"]),
        ("sweep", 30, ["/synthetic-trash"]),
    ]
