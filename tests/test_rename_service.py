"""Tests for RenameService: identify→route, apply/undo, LLM fallback, rematch.

TMDB is injected as a fake callable and the scraper/Ollama are never hit, so
these run fully offline.
"""
import os
import pytest

from backend.database import DatabaseManager
from backend.rename import llm_identify
from backend.rename.service import RenameService, compute_sort_title


@pytest.fixture(autouse=True)
def _reset_jobs():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_rename_jobs(); dm.close()
        except Exception:
            pass
    _clear(); yield; _clear()


@pytest.fixture
def db():
    dm = DatabaseManager(); yield dm; dm.close()


class _Reg:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.backend = None


def _matrix_search(title, year, media_type):
    return [{"id": 603, "title": "The Matrix", "release_date": "1999-03-30"}]


def _weak_search(title, year, media_type):
    return [{"id": 1, "title": "Completely Different Film", "release_date": "1980-01-01"}]


def _matrix_only(title, year, media_type):
    if "matrix" in (title or "").lower():
        return _matrix_search(title, year, media_type)
    return _weak_search(title, year, media_type)


def _service(db, tmdb_search, *, movie_lib="", tv_lib="", **cfg):
    base = {
        "auto_rename_enabled": True,
        "auto_rename_confidence_threshold": 70,
        "auto_rename_require_confirmation": True,
        "auto_rename_move_method": "move",
        "auto_rename_movie_library": movie_lib,
        "auto_rename_tv_library": tv_lib,
    }
    base.update(cfg)
    return RenameService(_Reg(base, db), tmdb_search=tmdb_search)


def _extracted(tmp_path, name, content="x"):
    d = tmp_path / "extracted"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(content)
    return str(d), str(f)


# ── identify → route ──────────────────────────────────────────────────

class TestProcessPackage:
    def test_high_confidence_is_matched_not_applied(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.BluRay.x264.mkv")
        ids = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib")).process_package("pkg1", save_to)
        assert len(ids) == 1
        job = db.get_rename_job(ids[0])
        assert job["status"] == "matched"
        assert job["title"] == "The Matrix"
        assert job["match_confidence"] >= 70
        assert job["new_filename"] == "The Matrix (1999) [1080p].mkv"

    def test_low_confidence_needs_review(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "Some.Obscure.Thing.2022.1080p.mkv")
        ids = _service(db, _weak_search).process_package("pkg2", save_to)
        job = db.get_rename_job(ids[0])
        assert job["status"] == "needs_review"
        assert job["match_confidence"] < 70

    def test_disabled_is_noop(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.mkv")
        ids = _service(db, _matrix_search, auto_rename_enabled=False).process_package("p", save_to)
        assert ids == []
        assert db.count_rename_jobs_by_status() == {}

    def test_dedup_by_package(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.mkv")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        first = svc.process_package("dup", save_to)
        second = svc.process_package("dup", save_to)
        assert len(first) == 1 and second == []

    def test_auto_apply_when_confirmation_not_required(self, db, tmp_path):
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        lib = str(tmp_path / "lib")
        ids = _service(db, _matrix_search, movie_lib=lib,
                       auto_rename_require_confirmation=False).process_package("pa", save_to)
        job = db.get_rename_job(ids[0])
        assert job["status"] == "applied"
        assert not os.path.exists(src)  # 'move' consumed the source
        assert os.path.isfile(os.path.join(lib, "The Matrix (1999)", "The Matrix (1999) [1080p].mkv"))


# ── apply / undo ──────────────────────────────────────────────────────

class TestApplyUndo:
    def test_apply_then_undo_round_trips(self, db, tmp_path):
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        lib = str(tmp_path / "lib")
        svc = _service(db, _matrix_search, movie_lib=lib)
        jid = svc.process_package("pkg", save_to)[0]
        assert svc.apply(jid)["ok"] is True
        job = db.get_rename_job(jid)
        assert job["status"] == "applied"
        dst = os.path.join(lib, "The Matrix (1999)", job["new_filename"])
        assert os.path.isfile(dst) and not os.path.exists(src)

        assert svc.undo(jid)["ok"] is True
        assert db.get_rename_job(jid)["status"] == "reverted"
        assert os.path.exists(src) and not os.path.exists(dst)

    def test_apply_missing_source_fails(self, db, tmp_path):
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.mkv")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        jid = svc.process_package("pkg", save_to)[0]
        os.remove(src)
        out = svc.apply(jid)
        assert out["ok"] is False
        assert db.get_rename_job(jid)["status"] == "failed"


# ── Ollama fallback ───────────────────────────────────────────────────

class TestLlmFallback:
    def test_llm_used_when_deterministic_is_weak(self, db, tmp_path, monkeypatch):
        save_to, _ = _extracted(tmp_path, "gibberish.release.name.mkv")
        monkeypatch.setattr(
            "backend.rename.llm_identify.identify",
            lambda *a, **k: {"title": "The Matrix", "year": 1999, "media_type": "movie"})
        svc = _service(db, _matrix_only, movie_lib=str(tmp_path / "lib"),
                       auto_rename_llm_enabled=True, ollama_base_url="http://x", ollama_model="m")
        job = db.get_rename_job(svc.process_package("pkg", save_to)[0])
        assert job["match_source"] == "llm"
        assert job["title"] == "The Matrix"
        assert job["status"] == "matched"

    def test_llm_not_called_when_deterministic_is_strong(self, db, tmp_path, monkeypatch):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return None
        monkeypatch.setattr("backend.rename.llm_identify.identify", _spy)
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"),
                       auto_rename_llm_enabled=True)
        svc.process_package("pkg", save_to)
        assert called["n"] == 0


# ── small units ───────────────────────────────────────────────────────

class TestUnits:
    def test_compute_sort_title(self):
        assert compute_sort_title("The Matrix") == "Matrix, The"
        assert compute_sort_title("An Education") == "Education, An"
        assert compute_sort_title("Inception") == "Inception"
        assert compute_sort_title(None) is None

    def test_llm_normalize_rejects_garbage(self):
        assert llm_identify._normalize("not a dict") is None
        assert llm_identify._normalize({"title": ""}) is None
        out = llm_identify._normalize({"title": "Dune", "year": "2021", "type": "movie",
                                       "season": None, "episode": "x"})
        assert out == {"title": "Dune", "year": 2021, "media_type": "movie",
                       "season": None, "episode": None}

    def test_llm_identify_handles_bad_response(self, monkeypatch):
        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"message": {"content": "not json"}}
        monkeypatch.setattr("backend.rename.llm_identify.requests.post",
                            lambda *a, **k: _Resp())
        assert llm_identify.identify("x.mkv", base_url="http://x", model="m") is None
