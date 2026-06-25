"""Tests for the auto-rename API: /rename/jobs, status, apply/undo, llm/test."""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager


@pytest.fixture(autouse=True)
def _reset_jobs():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_rename_jobs(); dm.close()
        except Exception:
            pass
    _clear(); yield; _clear()


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _seed_job(**fields):
    dm = DatabaseManager()
    job = {"original_path": "/x/y.mkv", "original_filename": "y.mkv", "status": "pending"}
    job.update(fields)
    jid = dm.create_rename_job(job)
    dm.close()
    return jid


class _FakeTags:
    def raise_for_status(self): pass
    def json(self): return {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5"}]}


class TestRenameApi:
    def test_list_empty(self, client):
        body = client.get("/rename/jobs").json()
        assert body["jobs"] == [] and body["counts"] == {}

    def test_status_defaults(self, client):
        body = client.get("/rename/status").json()
        assert body["enabled"] is False
        assert body["confidence_threshold"] == 70
        assert body["counts"] == {}
        assert body["needs_review"] == 0

    def test_list_and_status_filter(self, client):
        _seed_job(status="needs_review", title="A")
        _seed_job(status="applied", title="B")
        allj = client.get("/rename/jobs").json()
        assert len(allj["jobs"]) == 2
        assert allj["counts"].get("needs_review") == 1
        nr = client.get("/rename/jobs?status=needs_review").json()
        assert len(nr["jobs"]) == 1 and nr["jobs"][0]["title"] == "A"

    def test_apply_unknown_job_is_400(self, client):
        assert client.post("/rename/jobs/99999/apply").status_code == 400

    def test_apply_then_undo_via_api(self, client, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("x")
        dest = tmp_path / "lib"
        jid = _seed_job(status="matched", title="Movie",
                        original_path=str(src), destination_path=str(dest),
                        new_filename="Movie (2020).mkv")
        # default move_method is hardlink → source remains, link created
        assert client.post(f"/rename/jobs/{jid}/apply").status_code == 200
        assert (dest / "Movie (2020).mkv").exists()
        assert client.post(f"/rename/jobs/{jid}/undo").status_code == 200
        assert not (dest / "Movie (2020).mkv").exists()
        assert src.exists()

    def test_delete_job(self, client):
        jid = _seed_job(status="needs_review", title="X")
        assert client.delete(f"/rename/jobs/{jid}").status_code == 200
        assert client.get("/rename/jobs").json()["jobs"] == []

    def test_llm_test_endpoint(self, client, monkeypatch):
        monkeypatch.setattr("backend.rename.llm_identify.requests.get",
                            lambda *a, **k: _FakeTags())
        body = client.get("/rename/llm/test").json()
        assert body["ok"] is True
        assert "llama3.1:8b" in body["models"]
