"""Tests for RenameService.conflict_preview: a read-only, two-file spec
comparison (existing on-disk file vs incoming source) used by the mobile
Renames conflict-resolution UI to decide overwrite vs keep-both vs skip.

Uses the same harness as tests/test_apply_conflict_strategy.py (temp DB via
DatabaseManager + a directly-constructed RenameService) plus a TestClient for
the one route-level check.
"""
import os

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager
from backend.rename.service import RenameService
from backend.rename import service as svcmod


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
    base = {"auto_rename_enabled": True, "auto_rename_move_method": "move"}
    base.update(cfg)
    return RenameService(_Reg(base, db), tmdb_search=lambda *a, **k: [])


def _make_job(db, tmp_path, *, src_name, dst_name, create_dst, incoming_bytes=b"x"):
    """Create a rename job whose destination is dst_dir/dst_name. If
    create_dst, also writes an actual (empty-content) file there so
    os.path.lexists(dst) is True — conflict_preview checks the real
    filesystem for that, it isn't mockable via the probe_specs stub alone.
    Returns (job_id, src_path_str, dst_path_str)."""
    src_dir = tmp_path / "incoming"
    src_dir.mkdir(exist_ok=True)
    src = src_dir / src_name
    src.write_bytes(incoming_bytes)
    dst_dir = tmp_path / "lib"
    dst_dir.mkdir(exist_ok=True)
    dst = os.path.join(str(dst_dir), dst_name)
    if create_dst:
        with open(dst, "wb") as f:
            f.write(b"OLD")
    jid = db.create_rename_job({
        "original_path": str(src),
        "original_filename": src_name,
        "new_filename": dst_name,
        "destination_path": str(dst_dir),
        "status": "needs_review",
        "match_confidence": 90,
    })
    return jid, str(src), dst


# ── destination free → recommend the incoming file ──────────────────────

def test_conflict_preview_destination_free_recommends_incoming(db, tmp_path, monkeypatch):
    svc = _service(db)
    jid, src, dst = _make_job(
        db, tmp_path, src_name="New.Movie.2026.1080p.mkv",
        dst_name="Movie (2026).mkv", create_dst=False)

    specs = {src: {"present": True, "resolution": "1080p", "hdr": None,
                    "dv_layer": None, "video_codec": "HEVC"}}
    monkeypatch.setattr(svcmod, "probe_specs",
                        lambda p, **k: specs.get(p, {"present": False, "path": p}))

    before = db.get_rename_job(jid)
    out = svc.conflict_preview(jid)

    assert out["existing"]["present"] is False
    assert out["incoming"]["present"] is True
    assert out["incoming"]["resolution"] == "1080p"
    assert out["recommended"] == "incoming"

    # Read-only: no DB write happened.
    assert db.get_rename_job(jid) == before


# ── existing present with richer probed specs → recommend keeping it ────

def test_conflict_preview_existing_richer_specs_recommends_existing(db, tmp_path, monkeypatch):
    svc = _service(db)
    jid, src, dst = _make_job(
        db, tmp_path, src_name="New.Movie.2026.1080p.mkv",
        dst_name="Movie (2026).mkv", create_dst=True)

    specs = {
        src: {"present": True, "resolution": "1080p", "hdr": None,
              "dv_layer": None, "video_codec": "HEVC"},
        dst: {"present": True, "resolution": "2160p", "hdr": "Dolby Vision",
              "dv_layer": "fel", "video_codec": "HEVC"},
    }
    monkeypatch.setattr(svcmod, "probe_specs",
                        lambda p, **k: specs.get(p, {"present": False, "path": p}))

    before = db.get_rename_job(jid)
    out = svc.conflict_preview(jid)

    assert out["existing"]["present"] is True
    assert out["existing"]["resolution"] == "2160p"
    assert out["recommended"] == "existing"
    assert "2160p" in (out["reason"] or "") or "Dolby Vision" in (out["reason"] or "")

    assert db.get_rename_job(jid) == before


# ── correctness trap: Plex-named 2160p DV existing must beat a tag-rich but
#    probed-lower incoming — the recommendation judges specs, not filenames ──

def test_conflict_preview_recommends_existing_dv_over_tag_rich_lower(db, tmp_path, monkeypatch):
    svc = _service(db)
    jid, src, dst = _make_job(
        db, tmp_path,
        src_name="Movie.2024.1080p.BluRay.REMUX.Atmos.mkv",  # tag-rich name…
        dst_name="Movie (2024).mkv",  # …vs. a stripped Plex-clean existing name
        create_dst=True)

    specs = {
        # …but the incoming file's REAL probed spec is only 1080p/no HDR.
        src: {"present": True, "resolution": "1080p", "hdr": None,
              "dv_layer": None, "video_codec": "HEVC"},
        # The existing library file's probed spec is 2160p DV FEL.
        dst: {"present": True, "resolution": "2160p", "hdr": "Dolby Vision",
              "dv_layer": "fel", "video_codec": "HEVC"},
    }
    monkeypatch.setattr(svcmod, "probe_specs",
                        lambda p, **k: specs.get(p, {"present": False, "path": p}))

    before = db.get_rename_job(jid)
    out = svc.conflict_preview(jid)

    assert out["recommended"] == "existing"
    assert db.get_rename_job(jid) == before


# ── FIX 5: a genuinely FAILED probe (ffprobe missing/timeout/error — not the
#    legitimate {present: False} "no file here" result) on either side must
#    not produce a confidently-wrong recommendation from filename-only data ──

def test_conflict_preview_probe_failure_on_existing_yields_no_recommendation(
        db, tmp_path, monkeypatch):
    svc = _service(db)
    jid, src, dst = _make_job(
        db, tmp_path, src_name="New.Movie.2026.1080p.mkv",
        dst_name="Movie (2026).mkv", create_dst=True)

    # incoming probes fine; existing (dst) probe FAILS (returns None, as
    # probe_specs does on a missing ffprobe binary / timeout / parse error).
    specs = {src: {"present": True, "resolution": "1080p", "hdr": None,
                    "dv_layer": None, "video_codec": "HEVC"}}
    monkeypatch.setattr(svcmod, "probe_specs", lambda p, **k: specs.get(p))

    out = svc.conflict_preview(jid)

    assert out["recommended"] is None
    assert out["reason"] is None
    # Degraded specs are still returned so the UI can show what it has.
    assert out["existing"] is not None
    assert out["existing"]["present"] is True
    assert out["incoming"] is not None
    assert out["incoming"]["present"] is True


def test_conflict_preview_probe_failure_on_incoming_yields_no_recommendation(
        db, tmp_path, monkeypatch):
    svc = _service(db)
    jid, src, dst = _make_job(
        db, tmp_path, src_name="New.Movie.2026.1080p.mkv",
        dst_name="Movie (2026).mkv", create_dst=False)

    # incoming (src) probe FAILS; destination doesn't exist (legit, not a
    # failure) so `existing` stays a clean {present: False}.
    monkeypatch.setattr(svcmod, "probe_specs", lambda p, **k: None)

    out = svc.conflict_preview(jid)

    assert out["recommended"] is None
    assert out["reason"] is None
    assert out["existing"]["present"] is False


# ── job not found: clean dict, never raises ──────────────────────────────

def test_conflict_preview_job_not_found(db):
    svc = _service(db)
    out = svc.conflict_preview(999999)
    assert out == {"existing": None, "incoming": None,
                   "recommended": None, "reason": "Job not found"}


# ── route-level: the endpoint wires through to the service, bodyless POST ──

@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def test_conflict_preview_route_returns_service_output(client, db, tmp_path, monkeypatch):
    jid, src, dst = _make_job(
        db, tmp_path, src_name="New.Movie.2026.1080p.mkv",
        dst_name="Movie (2026).mkv", create_dst=False)

    specs = {src: {"present": True, "resolution": "1080p", "hdr": None,
                    "dv_layer": None, "video_codec": "HEVC"}}
    monkeypatch.setattr(svcmod, "probe_specs",
                        lambda p, **k: specs.get(p, {"present": False, "path": p}))

    r = client.post(f"/rename/jobs/{jid}/conflict-preview")

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"existing", "incoming", "recommended", "reason"}
    assert body["recommended"] == "incoming"


def test_conflict_preview_route_job_not_found_clean_200(client):
    r = client.post("/rename/jobs/999999/conflict-preview")
    assert r.status_code == 200
    assert r.json()["reason"] == "Job not found"
