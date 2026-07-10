"""Tests for RenameService.scan_conflict_dv — on-demand DV FEL/MEL scan of a
single conflict's two files (incoming source + existing destination).
"""
from unittest.mock import patch

import pytest

from backend.database import DatabaseManager
from backend.rename.service import RenameService


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
    base = {"auto_rename_enabled": True}
    base.update(cfg)
    return RenameService(_Reg(base, db), tmdb_search=lambda *a, **k: [])


@pytest.fixture
def svc(db):
    return _service(db)


def _conflict_job(db, tmp_path):
    """Create a rename job whose incoming source AND resolved destination both
    exist on disk, so scan_conflict_dv has two real files to scan."""
    src = tmp_path / "incoming" / "Movie.2020.1080p.mkv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("incoming-bytes")

    dest_dir = tmp_path / "library"
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_filename = "Movie (2020).mkv"
    (dest_dir / new_filename).write_text("existing-bytes")

    jid = db.create_rename_job({
        "original_path": str(src),
        "original_filename": src.name,
        "status": "needs_review",
        "destination_path": str(dest_dir),
        "new_filename": new_filename,
    })
    return jid, str(src), str(dest_dir / new_filename)


class TestScanConflictDv:
    def test_detects_and_upserts_both_files(self, svc, db, tmp_path):
        jid, src_path, dst_path = _conflict_job(db, tmp_path)
        with patch("backend.rename.dv_detect.available", return_value=True), \
             patch("backend.rename.dv_detect.detect_layer",
                   return_value={"layer": "fel", "tool": True, "error": None}):
            out = svc.scan_conflict_dv(jid)

        assert out["scanned"] >= 1
        assert db.get_dv_scan(src_path)["dv_layer"] == "fel"
        assert db.get_dv_scan(dst_path)["dv_layer"] == "fel"
        assert out["job_id"] == jid

    def test_missing_job_returns_error(self, svc, db):
        out = svc.scan_conflict_dv(999999)
        assert out["scanned"] == 0
        assert out.get("error")

    def test_dovi_tool_unavailable_returns_error(self, svc, db, tmp_path):
        jid, _src_path, _dst_path = _conflict_job(db, tmp_path)
        with patch("backend.rename.dv_detect.available", return_value=False):
            out = svc.scan_conflict_dv(jid)
        assert out["scanned"] == 0
        assert "dovi_tool" in out.get("error", "")

    def test_skips_nonexistent_destination(self, svc, db, tmp_path):
        # Only the incoming source exists on disk; destination_path/new_filename
        # point at a file that was never written — must be skipped, not error.
        src = tmp_path / "incoming2" / "Movie.2021.1080p.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("x")
        jid = db.create_rename_job({
            "original_path": str(src),
            "original_filename": src.name,
            "status": "needs_review",
            "destination_path": str(tmp_path / "nolib"),
            "new_filename": "Movie (2021).mkv",
        })
        with patch("backend.rename.dv_detect.available", return_value=True), \
             patch("backend.rename.dv_detect.detect_layer",
                   return_value={"layer": "mel", "tool": True, "error": None}):
            out = svc.scan_conflict_dv(jid)
        assert out["scanned"] == 1
        assert db.get_dv_scan(str(src))["dv_layer"] == "mel"

    def test_second_call_skips_unchanged_files(self, svc, db, tmp_path):
        jid, src_path, dst_path = _conflict_job(db, tmp_path)
        with patch("backend.rename.dv_detect.available", return_value=True), \
             patch("backend.rename.dv_detect.detect_layer",
                   return_value={"layer": "fel", "tool": True, "error": None}):
            first = svc.scan_conflict_dv(jid)
            second = svc.scan_conflict_dv(jid)
        assert first["scanned"] == 2
        assert second["scanned"] == 0

    def test_failed_detect_recorded_as_unknown(self, svc, db, tmp_path):
        # Mirrors test_rename_service.py::TestDvFolderScan::
        # test_every_file_accounted_including_failures — a detect_layer failure
        # on one of the two files must still be recorded (as 'unknown'), not
        # silently dropped, so the file stays visible in the inventory.
        jid, src_path, dst_path = _conflict_job(db, tmp_path)

        def fake_detect(path):
            if path == src_path:
                return {"layer": "fel", "tool": True, "error": None}
            raise OSError("boom")  # destination file fails detection

        with patch("backend.rename.dv_detect.available", return_value=True), \
             patch("backend.rename.dv_detect.detect_layer", side_effect=fake_detect):
            out = svc.scan_conflict_dv(jid)

        assert out["scanned"] == 2  # both files accounted for, none dropped
        assert db.get_dv_scan(src_path)["dv_layer"] == "fel"
        assert db.get_dv_scan(dst_path)["dv_layer"] == "unknown"
