"""Tests for the auto-rename API: /rename/jobs, status, apply/undo, llm/test."""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager


@pytest.fixture(autouse=True)
def _reset_jobs():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_rename_jobs(); dm.clear_dv_scans(); dm.close()
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


def _seed_dv_scan(path, dv_layer):
    dm = DatabaseManager()
    dm.upsert_dv_scan(path=path, title="x", dv_layer=dv_layer,
                      sig_mtime=0.0, sig_size=0, source="test",
                      rating_key=None, imdb_id=None)
    dm.close()


class _FakeTags:
    def raise_for_status(self): pass
    def json(self): return {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5"}]}


class TestRenameApi:
    def test_list_empty(self, client):
        body = client.get("/rename/jobs").json()
        assert body["jobs"] == [] and body["counts"] == {}

    def test_health_reports_capabilities(self, client):
        body = client.get("/rename/health").json()
        assert set(body["binaries"]) == {"ffmpeg", "ffprobe", "tesseract", "dovi_tool"}
        assert set(body["capabilities"]) == {
            "runtime_check", "subtitles", "ocr_credits", "vision", "dv_detection"}
        assert "ok" in body["ollama"] and "llm_enabled" in body

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

    def test_jobs_flags_destination_conflict(self, client):
        # Two active jobs targeting the same destination file (two releases of one
        # movie) must both be flagged; an unrelated job must not be.
        dest, name = "/lib/movies", "Dup (2020) [2160p].mkv"
        _seed_job(status="matched", title="Dup", destination_path=dest, new_filename=name)
        _seed_job(status="matched", title="Dup", destination_path=dest, new_filename=name)
        _seed_job(status="matched", title="Solo", destination_path=dest,
                  new_filename="Solo (2019).mkv")
        jobs = client.get("/rename/jobs").json()["jobs"]
        dups = [j for j in jobs if j["title"] == "Dup"]
        solos = [j for j in jobs if j["title"] == "Solo"]
        assert len(dups) == 2 and all(j["destination_conflict"] for j in dups)
        assert solos and all(not j["destination_conflict"] for j in solos)

    def test_jobs_recommends_keeper_for_duplicate(self, client):
        dest, name = "/lib/movies", "Dune (2021) [2160p].mkv"
        _seed_job(status="matched", title="Dune", destination_path=dest, new_filename=name,
                  original_filename="Dune.2021.2160p.WEB-DL.mkv", resolution="2160p")
        _seed_job(status="matched", title="Dune", destination_path=dest, new_filename=name,
                  original_filename="Dune.2021.2160p.BluRay.REMUX.DV.HDR.TrueHD.mkv",
                  resolution="2160p")
        jobs = client.get("/rename/jobs").json()["jobs"]
        dune = [j for j in jobs if j["title"] == "Dune"]
        keepers = [j for j in dune if j.get("keep_recommended")]
        assert len(keepers) == 1
        assert "REMUX" in (keepers[0]["original_filename"] or "")
        assert keepers[0]["keep_reason"]

    def test_dv_scans_empty_and_shape(self, client):
        body = client.get("/rename/dv-scans").json()
        assert body["scans"] == [] and body["counts"] == {}

    def test_dv_scan_folder_requires_folder(self, client):
        assert client.post("/rename/dv-scan-folder", json={"folder": ""}).status_code == 400

    def test_dv_scan_folder_starts(self, client, monkeypatch):
        # Don't actually walk the FS / run dovi_tool — just confirm the endpoint
        # dispatches the background job and returns 'started'.
        import backend.rename.service as svc_mod
        monkeypatch.setattr(svc_mod.RenameService, "scan_folder_dv",
                            lambda self, folder, force=False, progress_cb=None: {
                                "found": 0, "scanned": 0, "skipped": 0, "by_layer": {}})
        body = client.post("/rename/dv-scan-folder",
                           json={"folder": "/library/movies-4k"}).json()
        assert body["status"] == "started"

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

    def test_poster_url_built_when_poster_path_set(self, client):
        _seed_job(status="matched", title="M", poster_path="/abc.jpg")
        job = client.get("/rename/jobs").json()["jobs"][0]
        assert job["poster_url"] is not None
        assert job["poster_url"].endswith("/abc.jpg")
        assert "image.tmdb.org/t/p/" in job["poster_url"]

    def test_poster_url_null_when_no_poster_path(self, client):
        _seed_job(status="matched", title="M")
        job = client.get("/rename/jobs").json()["jobs"][0]
        assert job["poster_url"] is None

    def test_dv_layer_joined_when_dv_scan_exists(self, client):
        _seed_dv_scan("/x/y.mkv", "fel")
        _seed_job(status="matched", title="M", original_path="/x/y.mkv")
        job = client.get("/rename/jobs").json()["jobs"][0]
        assert job["dv_layer"] == "fel"

    def test_dv_layer_null_when_no_dv_scan(self, client):
        _seed_job(status="matched", title="M", original_path="/x/none.mkv")
        job = client.get("/rename/jobs").json()["jobs"][0]
        assert job["dv_layer"] is None

    def test_rematch_preview_does_not_mutate_db(self, client, monkeypatch):
        import backend.rename.service as svc_mod
        jid = _seed_job(status="needs_review", title="Old", media_type="movie",
                        destination_path="", new_filename="old.mkv")
        monkeypatch.setattr(
            svc_mod.RenameService, "_tmdb_client",
            lambda self: type("T", (), {"details": staticmethod(
                lambda tmdb_id, media_type="movie", language="en-US": {
                    "title": "New Title", "release_date": "2021-01-01",
                    "poster_path": "/n.jpg"})})())
        before = DatabaseManager(); snap = before.get_rename_job(jid); before.close()
        r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                        json={"tmdb_id": 99, "media_type": "movie"}).json()
        assert "new_filename" in r and "library_configured" in r
        after = DatabaseManager(); now = after.get_rename_job(jid); after.close()
        assert now["new_filename"] == snap["new_filename"]
        assert now["title"] == snap["title"]

    def test_rematch_preview_library_unconfigured_flag(self, client, monkeypatch):
        import backend.rename.service as svc_mod
        jid = _seed_job(status="needs_review", title="Old", media_type="movie")
        monkeypatch.setattr(
            svc_mod.RenameService, "_tmdb_client",
            lambda self: type("T", (), {"details": staticmethod(
                lambda tmdb_id, media_type="movie", language="en-US": {
                    "title": "New Title", "release_date": "2021-01-01"})})())
        r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                        json={"tmdb_id": 99, "media_type": "movie"}).json()
        assert r["library_configured"] is False
        assert r["warning"]

    def test_rematch_preview_library_configured(self, client, monkeypatch, tmp_path):
        import backend.rename.service as svc_mod
        lib = str(tmp_path / "movies")
        # Patch the service config to have a movie library set
        jid = _seed_job(status="needs_review", title="Old", media_type="movie",
                        resolution="1080p")
        monkeypatch.setattr(
            svc_mod.RenameService, "_tmdb_client",
            lambda self: type("T", (), {"details": staticmethod(
                lambda tmdb_id, media_type="movie", language="en-US": {
                    "title": "New Title", "release_date": "2021-01-01",
                    "poster_path": "/n.jpg"})})())
        monkeypatch.setattr(
            svc_mod.RenameService, "_cfg",
            property(lambda self: {"auto_rename_movie_library": lib}))
        r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                        json={"tmdb_id": 99, "media_type": "movie"}).json()
        assert r["library_configured"] is True
        assert r["new_filename"] is not None
        assert r["destination_path"] is not None
        assert lib in r["destination_path"]
        assert r["warning"] is None

    # ------------------------------------------------------------------
    # Fail-safe no-500 guarantee tests
    # ------------------------------------------------------------------

    def test_rematch_preview_job_not_found_clean_200(self, client):
        """POST to a non-existent job must return HTTP 200 clean dict (not 500)."""
        r = client.post("/rename/jobs/99999/rematch-preview",
                        json={"tmdb_id": 1, "media_type": "movie"})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"new_filename", "destination_path",
                                    "library_configured", "warning"}
        assert body["library_configured"] is False
        assert body["destination_path"] is None
        assert body["warning"] == "Job not found"

    def test_rematch_preview_tmdb_error_clean_200(self, client, monkeypatch):
        """TMDB fetch raising must return HTTP 200 clean dict with warning (not 500)."""
        import backend.rename.service as svc_mod
        jid = _seed_job(status="needs_review", title="Old", media_type="movie")

        def _boom(*a, **kw):
            raise RuntimeError("TMDB network error")

        monkeypatch.setattr(
            svc_mod.RenameService, "_tmdb_client",
            lambda self: type("T", (), {"details": staticmethod(_boom)})())
        r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                        json={"tmdb_id": 99, "media_type": "movie"})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"new_filename", "destination_path",
                                    "library_configured", "warning"}
        assert body["library_configured"] is False
        assert body["destination_path"] is None
        assert body["warning"]  # warning text is set

    def test_rematch_preview_unconfigured_library_has_new_filename(self, client, monkeypatch):
        """Unconfigured library path: build_target is called with empty root.
        Must return HTTP 200, library_configured=False, destination_path=None,
        and new_filename set (the would-be name) — never 500."""
        import backend.rename.service as svc_mod
        jid = _seed_job(status="needs_review", title="Old", media_type="movie",
                        original_filename="old.mkv")
        monkeypatch.setattr(
            svc_mod.RenameService, "_tmdb_client",
            lambda self: type("T", (), {"details": staticmethod(
                lambda tmdb_id, media_type="movie", language="en-US": {
                    "title": "Real Title", "release_date": "2022-05-01"})})())
        # No library configured (default config has no movie root)
        r = client.post(f"/rename/jobs/{jid}/rematch-preview",
                        json={"tmdb_id": 99, "media_type": "movie"})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"new_filename", "destination_path",
                                    "library_configured", "warning"}
        assert body["library_configured"] is False
        assert body["destination_path"] is None
        assert body["new_filename"] is not None  # would-be name still produced
        assert body["warning"]  # "Movie library not configured…"

    # ------------------------------------------------------------------
    # GET /rename/search-tmdb — Task 6
    # ------------------------------------------------------------------

    def test_search_tmdb_results_include_poster_url(self, client, monkeypatch):
        import backend.rename.service as svc_mod
        monkeypatch.setattr(
            svc_mod.RenameService, "_tmdb_client",
            lambda self: type("T", (), {"search": staticmethod(
                lambda query, media_type="movie", year=None, language="en-US": [
                    {"id": 603, "title": "The Matrix",
                     "release_date": "1999-03-31", "poster_path": "/m.jpg"}])})())
        r = client.get("/rename/search-tmdb?query=matrix&media_type=movie").json()
        assert len(r["results"]) == 1
        res = r["results"][0]
        assert res["tmdb_id"] == 603
        assert res["title"] == "The Matrix"
        assert res["year"] == 1999
        assert res["media_type"] == "movie"
        assert res["poster_url"].endswith("/m.jpg")

    def test_search_tmdb_empty_query_returns_empty(self, client):
        r = client.get("/rename/search-tmdb?query=&media_type=movie").json()
        assert r["results"] == []

    def test_search_tmdb_no_client_returns_empty(self, client, monkeypatch):
        import backend.rename.service as svc_mod
        monkeypatch.setattr(svc_mod.RenameService, "_tmdb_client", lambda self: None)
        r = client.get("/rename/search-tmdb?query=matrix&media_type=movie").json()
        assert r["results"] == []

    # ------------------------------------------------------------------
    # Task 7: Bulk endpoints
    # ------------------------------------------------------------------

    def test_bulk_apply_partial_failure(self, client, tmp_path):
        dest = tmp_path / "lib"
        src = tmp_path / "ok.mkv"; src.write_text("x")
        ok = _seed_job(status="matched", title="Ok", original_path=str(src),
                       destination_path=str(dest), new_filename="Ok (2020).mkv")
        bad = _seed_job(status="matched", title="Bad",
                        original_path=str(tmp_path / "missing.mkv"),
                        destination_path=str(dest), new_filename="Bad (2020).mkv")
        r = client.post("/rename/jobs/bulk/apply",
                        json={"ids": [ok, bad]}).json()
        by = {x["id"]: x for x in r["results"]}
        assert by[ok]["ok"] is True
        assert by[bad]["ok"] is False and by[bad]["error"]
        assert r["applied"] == 1 and r["failed"] == 1

    def test_bulk_delete_counts(self, client):
        a = _seed_job(status="needs_review", title="A")
        b = _seed_job(status="needs_review", title="B")
        r = client.post("/rename/jobs/bulk/delete", json={"ids": [a, b]}).json()
        assert r["deleted"] == 2
        assert client.get("/rename/jobs").json()["jobs"] == []

    def test_bulk_reidentify_applied_not_counted(self, client):
        # An already-applied job cannot be reidentified; reidentify returns ok:False.
        # bulk_reidentify must NOT count it — queued must be 0.
        a = _seed_job(status="applied", title="A")
        r = client.post("/rename/jobs/bulk/reidentify", json={"ids": [a]}).json()
        assert r["ok"] is True
        assert r["queued"] == 0  # reidentify returned ok:False → not counted

    def test_bulk_reidentify_queues_only_successes(self, client, monkeypatch, tmp_path):
        # Patch reidentify to simulate one success and one failure; only the
        # success must be counted.
        import backend.rename.service as svc_mod
        call_count = {"n": 0}

        def _fake_reidentify(self, job_id):
            call_count["n"] += 1
            return {"ok": True, "job_id": job_id} if call_count["n"] == 1 else {"ok": False, "error": "sim fail"}

        monkeypatch.setattr(svc_mod.RenameService, "reidentify", _fake_reidentify)
        a = _seed_job(status="needs_review", title="A")
        b = _seed_job(status="needs_review", title="B")
        r = client.post("/rename/jobs/bulk/reidentify", json={"ids": [a, b]}).json()
        assert r["ok"] is True
        assert r["queued"] == 1  # only the first (ok:True) is counted

    def test_bulk_set_destination_applied_job_not_regressed(self, client, tmp_path):
        # An already-applied job must not have its status changed back to matched.
        jid = _seed_job(status="applied", title="Done", media_type="movie",
                        destination_path="/lib/movies/Done (2020).mkv",
                        new_filename="Done (2020).mkv")
        root = str(tmp_path / "movies")
        r = client.post("/rename/jobs/bulk/set-destination",
                        json={"ids": [jid], "destination_root": root}).json()
        res = r["results"][0]
        assert res["ok"] is False
        assert "already applied" in res["error"]
        assert r["updated"] == 0
        # Status must still be "applied" — no regression to "matched"
        all_jobs = client.get("/rename/jobs").json()["jobs"]
        job = next(j for j in all_jobs if j["id"] == jid)
        assert job["status"] == "applied"

    def test_bulk_set_destination_guard_enforced(self, client, tmp_path):
        # Movie job, valid root -> rebuilt destination under root.
        jid = _seed_job(status="matched", title="The Matrix", year=1999,
                        media_type="movie", resolution="1080p",
                        new_filename="The Matrix (1999) [1080p].mkv")
        root = str(tmp_path / "movies")
        r = client.post("/rename/jobs/bulk/set-destination",
                        json={"ids": [jid], "destination_root": root}).json()
        res = r["results"][0]
        assert res["ok"] is True
        assert res["destination_path"].startswith(root)
        assert r["updated"] == 1

    def test_bulk_set_destination_empty_root_blocks(self, client):
        jid = _seed_job(status="matched", title="M", media_type="movie")
        r = client.post("/rename/jobs/bulk/set-destination",
                        json={"ids": [jid], "destination_root": ""}).json()
        res = r["results"][0]
        assert res["ok"] is False
        assert res["destination_path"] is None

    # ------------------------------------------------------------------
    # Task 8: POST /rename/jobs/apply-confident
    # ------------------------------------------------------------------

    def test_apply_confident_applies_matched_96(self, client, tmp_path):
        dest = tmp_path / "lib"
        src = tmp_path / "ok.mkv"; src.write_text("x")
        jid = _seed_job(status="matched", title="Ok", match_confidence=96,
                        original_path=str(src), destination_path=str(dest),
                        new_filename="Ok (2020).mkv")
        r = client.post("/rename/jobs/apply-confident", json={}).json()
        assert r["applied"] == 1 and r["skipped"] == 0
        assert (dest / "Ok (2020).mkv").exists()

    def test_apply_confident_skips_matched_94(self, client):
        jid = _seed_job(status="matched", title="Low", match_confidence=94)
        r = client.post("/rename/jobs/apply-confident", json={}).json()
        assert r["applied"] == 0 and r["skipped"] == 1

    def test_apply_confident_skips_needs_review_99(self, client):
        jid = _seed_job(status="needs_review", title="NR", match_confidence=99)
        r = client.post("/rename/jobs/apply-confident", json={}).json()
        assert r["applied"] == 0 and r["skipped"] == 1

    def test_apply_confident_scoped_to_ids(self, client, tmp_path):
        dest = tmp_path / "lib"
        s1 = tmp_path / "a.mkv"; s1.write_text("x")
        a = _seed_job(status="matched", title="A", match_confidence=96,
                      original_path=str(s1), destination_path=str(dest),
                      new_filename="A (2020).mkv")
        b = _seed_job(status="matched", title="B", match_confidence=96,
                      original_path=str(tmp_path / "b.mkv"),
                      destination_path=str(dest), new_filename="B (2020).mkv")
        # Scope to only A; B (also confident) must be untouched.
        r = client.post("/rename/jobs/apply-confident", json={"ids": [a]}).json()
        assert r["applied"] == 1
        assert all(x["id"] == a for x in r["results"])

    def test_apply_confident_empty_ids_applies_nothing(self, client, tmp_path):
        # An empty ids list is an explicit empty selection: must apply nothing,
        # even when a confident matched job exists (regression guard for the
        # if ids: → if ids is not None: fix).
        dest = tmp_path / "lib"
        src = tmp_path / "ok.mkv"; src.write_text("x")
        jid = _seed_job(status="matched", title="Ok", match_confidence=96,
                        original_path=str(src), destination_path=str(dest),
                        new_filename="Ok (2020).mkv")
        r = client.post("/rename/jobs/apply-confident", json={"ids": []}).json()
        assert r["applied"] == 0 and r["skipped"] == 0 and r["failed"] == 0
        # The file must NOT have been moved.
        assert not (dest / "Ok (2020).mkv").exists()
        assert src.exists()

    def test_apply_confident_boundary_exactly_95(self, client, tmp_path):
        # A job at match_confidence == 95 must be applied (>= 95 gate is inclusive).
        dest = tmp_path / "lib"
        src = tmp_path / "exact.mkv"; src.write_text("x")
        jid = _seed_job(status="matched", title="Boundary", match_confidence=95,
                        original_path=str(src), destination_path=str(dest),
                        new_filename="Boundary (2020).mkv")
        r = client.post("/rename/jobs/apply-confident", json={}).json()
        assert r["applied"] == 1 and r["skipped"] == 0
        assert (dest / "Boundary (2020).mkv").exists()
