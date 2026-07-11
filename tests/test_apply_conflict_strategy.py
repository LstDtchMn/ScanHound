"""Tests for RenameService.apply(conflict_strategy=...): overwrite / keep_both
/ skip / default(None) resolution of a destination collision.

Uses the same harness as tests/test_rename_service.py (temp DB via
DatabaseManager + a directly-constructed RenameService), not the illustrative
``svc``/``db`` fixtures sketched in the planning doc (which don't exist in
this codebase).
"""
import os
import pytest

from backend.database import DatabaseManager
from backend.rename.service import RenameService
from backend.rename import fileops


@pytest.fixture(autouse=True)
def _reset():
    def _c():
        try:
            dm = DatabaseManager(); dm.clear_rename_jobs(); dm.clear_dv_scans(); dm.close()
        except Exception:
            pass
    _c(); yield; _c()


@pytest.fixture
def db():
    dm = DatabaseManager(); yield dm; dm.close()


class _Reg:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.backend = None


def _service(db, **cfg):
    base = {"auto_rename_enabled": True, "auto_rename_move_method": "move",
            "auto_rename_require_confirmation": True}
    base.update(cfg)
    return RenameService(_Reg(base, db), tmdb_search=lambda *a, **k: [])


def _make_conflict(db, tmp_path):
    """A source file whose rename target collides with an existing library file."""
    src = tmp_path / "incoming" / "New.2160p.DV.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"NEW")
    dst_dir = tmp_path / "lib"
    dst_dir.mkdir()
    existing = dst_dir / "Movie (2024).mkv"
    existing.write_bytes(b"OLD")
    jid = db.create_rename_job({
        "original_path": str(src),
        "original_filename": "New.2160p.DV.mkv",
        "new_filename": "Movie (2024).mkv",
        "destination_path": str(dst_dir),
        "status": "needs_review",
        "match_confidence": 100,
        "package_name": "pkg",
    })
    return jid, src, existing


# ── overwrite: trash the occupant, never delete ─────────────────────────

class TestOverwrite:
    def test_overwrite_trashes_existing_then_places(self, db, tmp_path, monkeypatch):
        # Site the trash on a known, inspectable path (mirrors
        # tests/test_rename_core.py's pattern) instead of relying on the
        # real per-volume .scanhound-trash root.
        trash_root = tmp_path / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))

        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        out = svc.apply(jid, conflict_strategy="overwrite")

        assert out["ok"] is True
        job = db.get_rename_job(jid)
        assert job["status"] == "applied"
        # dst now holds the incoming bytes.
        assert os.path.exists(str(existing))
        assert open(existing, "rb").read() == b"NEW"
        # The source was consumed by the (non-automatic) move.
        assert not os.path.exists(str(src))
        # The OLD file is recoverable in trash, never deleted.
        entries = fileops.list_trash_entries([str(trash_root)])
        assert len(entries) == 1
        assert entries[0]["original_path"] == str(existing)
        assert entries[0]["restorable"] is True
        trashed_file = os.path.join(trash_root, entries[0]["bucket"], entries[0]["name"])
        assert os.path.isfile(trashed_file)
        assert open(trashed_file, "rb").read() == b"OLD"

    def test_undo_of_overwrite_restores_original(self, db, tmp_path, monkeypatch):
        """undo() of an overwrite-applied job must be symmetric: it frees
        dst (moving the placed NEW file back to src) AND restores the OLD
        file that the overwrite displaced into trash — never stranding it
        there."""
        trash_root = tmp_path / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))

        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        svc.apply(jid, conflict_strategy="overwrite")
        assert open(existing, "rb").read() == b"NEW"

        out = svc.undo(jid)

        assert out["ok"] is True
        assert open(existing, "rb").read() == b"OLD"  # original restored from trash
        assert os.path.exists(str(src))  # placed file moved back to src
        assert db.get_rename_job(jid)["status"] == "reverted"
        # Trash bucket is empty again (nothing left stranded).
        assert fileops.list_trash_entries([str(trash_root)]) == []
        # FIX 6: a successful restore reports no warning.
        assert out.get("restore_warning") is None

    def test_undo_of_overwrite_surfaces_restore_failure(self, db, tmp_path, monkeypatch):
        """FIX 6 attack: if the trash-restore of the displaced original fails,
        undo() must still report ok:True (the NEW file was reverted — that
        part genuinely succeeded) but must surface the restore failure via
        restore_warning instead of only a server-log warning that the caller
        never sees."""
        trash_root = tmp_path / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))

        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        svc.apply(jid, conflict_strategy="overwrite")
        assert open(existing, "rb").read() == b"NEW"

        monkeypatch.setattr(
            fileops, "restore_trash_entry",
            lambda *a, **k: {"ok": False, "error": "simulated restore failure"})

        out = svc.undo(jid)

        assert out["ok"] is True  # the placed file was still reverted
        assert os.path.exists(str(src))
        assert db.get_rename_job(jid)["status"] == "reverted"
        # The failure is now surfaced, not silent.
        assert out.get("restore_warning")
        assert "simulated restore failure" in out["restore_warning"]

    def test_overwrite_same_inode_reapply_is_noop_not_trashed(self, db, tmp_path, monkeypatch):
        """Re-applying a job whose src/dst are already the same file (e.g. a
        prior hardlink apply) must be treated as a no-op success — never
        trashed onto itself."""
        trash_root = tmp_path / "trash"
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        svc = _service(db)

        f = tmp_path / "same.mkv"
        f.write_bytes(b"SAME")
        jid = db.create_rename_job({
            "original_path": str(f),
            "original_filename": "same.mkv",
            "new_filename": os.path.basename(str(f)),
            "destination_path": str(f.parent),
            "status": "needs_review",
            "match_confidence": 100,
            "package_name": "pkg",
        })

        out = svc.apply(jid, conflict_strategy="overwrite")

        assert out["ok"] is True
        assert out.get("already") is True
        assert db.get_rename_job(jid)["status"] == "applied"
        assert open(f, "rb").read() == b"SAME"
        assert not os.path.isdir(trash_root)  # nothing was ever trashed


# ── keep_both: dedupe alongside, existing untouched ─────────────────────

class TestKeepBoth:
    def test_keep_both_dedupes_and_rewrites_new_filename(self, db, tmp_path):
        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        out = svc.apply(jid, conflict_strategy="keep_both")

        assert out["ok"] is True
        # Original untouched.
        assert os.path.exists(str(existing))
        assert open(existing, "rb").read() == b"OLD"
        job = db.get_rename_job(jid)
        assert job["status"] == "applied"
        assert job["new_filename"] == "Movie (2024) (1).mkv"
        deduped = os.path.join(job["destination_path"], job["new_filename"])
        assert os.path.isfile(deduped)
        assert open(deduped, "rb").read() == b"NEW"
        assert not os.path.exists(str(src))


# ── skip: leave everything exactly as-is, job back to needs_review ──────

class TestSkip:
    def test_skip_leaves_job_unplaced(self, db, tmp_path):
        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        out = svc.apply(jid, conflict_strategy="skip")

        assert out["ok"] is False
        assert open(existing, "rb").read() == b"OLD"
        assert os.path.exists(str(src))  # source not consumed
        assert db.get_rename_job(jid)["status"] == "needs_review"

    def test_skip_via_queue_apply_ends_needs_review_not_stuck_applying(self, db, tmp_path):
        """queue_apply marks the job 'applying' before the worker thread runs
        apply(); the skip branch must restore 'needs_review', not leave the
        job stuck in the transient 'applying' state."""
        import time

        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        out = svc.queue_apply([jid], conflict_strategy="skip")
        assert out["ok"] is True
        assert out["queued"] == 1

        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = db.get_rename_job(jid)["status"]
            if status != "applying":
                break
            time.sleep(0.05)

        assert status == "needs_review"
        assert open(existing, "rb").read() == b"OLD"


# ── default (None): today's hold-for-review behavior, unchanged ─────────

class TestDefaultNone:
    def test_default_none_holds_for_review(self, db, tmp_path):
        svc = _service(db)
        jid, src, existing = _make_conflict(db, tmp_path)

        out = svc.apply(jid)  # no strategy → today's behavior

        assert out["ok"] is False
        assert "already in the library" in out["error"].lower()
        assert db.get_rename_job(jid)["status"] == "needs_review"
        assert open(existing, "rb").read() == b"OLD"
        assert os.path.exists(str(src))
