"""Tests for the auto-rename API: /rename/jobs, status, apply/undo, llm/test."""
import os

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager
from backend.rename import fileops


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


def _client_with_library(movie_library: str):
    """A client whose auto_rename_movie_library is configured to
    ``movie_library`` — needed for the A3 path-confinement tests, which now
    require a destination_root/folder to fall inside a configured library
    root rather than accepting any path a caller supplies."""
    app = create_app(config_override={
        "plex_url": "", "plex_token": "",
        "auto_rename_movie_library": movie_library,
    })
    return TestClient(app)


def _wait_settled(client, jid, timeout=8.0):
    """Poll until a queued apply finishes (status leaves 'applying').

    Applies now run on a background worker (the HTTP response only queues),
    so tests must wait for the terminal status before asserting on files.

    A successful apply also archives the job instantly (auto-archive-on-apply),
    which drops it out of the default (non-archived) /rename/jobs listing — so
    if the id isn't found there, check the archived listing too before
    concluding the job doesn't exist.
    """
    import time as _t
    deadline = _t.monotonic() + timeout
    status = None
    while _t.monotonic() < deadline:
        jobs = client.get("/rename/jobs").json()["jobs"]
        job = next((j for j in jobs if j["id"] == jid), None)
        if job is None:
            archived_jobs = client.get("/rename/jobs?archived=true").json()["jobs"]
            job = next((j for j in archived_jobs if j["id"] == jid), None)
        status = job and job.get("status")
        if status not in ("applying", "matched"):
            return status
        _t.sleep(0.05)
    return status


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

    def test_health_reports_failed_db_and_corruption_flag_fields(self, client):
        body = client.get("/rename/health").json()
        assert body["failed_db_last_package"] == 0
        assert body["db_corruption_flag"] is False

    def test_health_reports_nonzero_failed_db_last_package(self, client):
        from backend.api.dependencies import registry
        registry._rename_service.last_package_failed_db = 3
        try:
            body = client.get("/rename/health").json()
            assert body["failed_db_last_package"] == 3
        finally:
            registry._rename_service.last_package_failed_db = 0

    def test_health_reports_db_corruption_flag_present(self, client, monkeypatch):
        from backend.api.dependencies import registry
        flag_path = f"{registry.db.db_path}.corrupt_flag.json"
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("{}")
        try:
            body = client.get("/rename/health").json()
            assert body["db_corruption_flag"] is True
        finally:
            os.remove(flag_path)

    def test_status_defaults(self, client):
        body = client.get("/rename/status").json()
        assert body["enabled"] is False
        assert body["confidence_threshold"] == 70
        assert body["counts"] == {}
        assert body["needs_review"] == 0
        assert body["archived"] == 0

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

    def test_list_jobs_annotates_library_duplicate(self, client, tmp_path):
        # A movie job whose destination is free, but a same-title/year Plex row
        # exists at a DIFFERENT path — must be flagged library_duplicate.
        dm = DatabaseManager()
        dm.save_plex_cache([{
            "clean_title": "Dup Movie", "original_title": "Dup Movie", "year": 2021,
            "res": "1080p", "size": 5.0, "imdb_id": "tt777", "rating_key": "5",
            "media_id": "5", "key": "5_5_0", "file": "/library/movies/Dup Movie (2021)/f.mkv",
        }], "Movies")
        job_id = dm.create_rename_job({
            "original_path": str(tmp_path / "src.mkv"), "original_filename": "src.mkv",
            "status": "matched", "media_type": "movie", "title": "Dup Movie", "year": 2021,
            "imdb_id": "tt777", "destination_path": "/library/movies-4k/Dup Movie (2021)",
            "new_filename": "Dup Movie (2021) [2160p].mkv",
        })
        dm.close()
        resp = client.get("/rename/jobs")
        body = resp.json()
        job = next(j for j in body["jobs"] if j["id"] == job_id)
        assert job["library_duplicate"] is True

    def test_jobs_releases_analyzing_ids_when_thread_start_fails(self, client, monkeypatch):
        # If threading.Thread(...).start() itself raises (e.g. OS thread
        # exhaustion), the background _run()'s `finally` that would normally
        # release _analyzing_job_ids never gets a chance to run. The route
        # must release the just-reserved ids itself in that except branch, or
        # those job ids stay pinned "in flight" forever (never re-analyzed).
        import backend.api.routes.rename as rename_routes

        class _BoomThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr(rename_routes.threading, "Thread", _BoomThread)
        dest, name = "/lib/movies", "Dup (2020) [2160p].mkv"
        _seed_job(status="matched", title="Dup", destination_path=dest, new_filename=name)
        _seed_job(status="matched", title="Dup", destination_path=dest, new_filename=name)
        assert rename_routes._analyzing_job_ids == set()
        body = client.get("/rename/jobs").json()
        assert len(body["jobs"]) == 2
        # The except RuntimeError branch must have released the reserved ids.
        assert rename_routes._analyzing_job_ids == set()

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

    def test_dv_scan_folder_notification_reports_skipped_count(
            self, client, monkeypatch):
        """DV-scan success text covers the second pre-3.12 parser-sensitive path."""
        import backend.api.routes.rename as rename_routes
        import backend.rename.service as svc_mod

        events = []
        monkeypatch.setattr(
            svc_mod.RenameService,
            "scan_folder_dv",
            lambda self, folder, force=False, progress_cb=None: {
                "found": 5,
                "scanned": 4,
                "skipped": 2,
                "by_layer": {"fel": 1},
            },
        )
        monkeypatch.setattr(
            rename_routes.ws_manager,
            "broadcast_sync",
            events.append,
        )

        class _ImmediateThread:
            def __init__(self, target, *args, **kwargs):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(rename_routes.threading, "Thread", _ImmediateThread)

        response = client.post(
            "/rename/dv-scan-folder",
            json={"folder": "/library/movies-4k"},
        )

        assert response.status_code == 200
        notification = next(
            event for event in events
            if event.get("type") == "notification"
        )
        assert notification["data"]["body"] == (
            "Scanned 4 of 5 file(s) — 1 FEL, 2 unchanged"
        )
        assert notification["data"]["priority"] == "normal"

    def test_apply_unknown_job_is_400(self, client):
        assert client.post("/rename/jobs/99999/apply").status_code == 400

    def test_apply_cancel_endpoint_returns_ok(self, client):
        # Idle case (nothing running) — cancel_apply() is harmless to call
        # and always reports ok.
        r = client.post("/rename/apply/cancel")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_apply_cancel_endpoint_calls_service_cancel_apply(self, client, monkeypatch):
        # Wiring check: the route must delegate to RenameService.cancel_apply,
        # not reimplement flag-setting itself.
        import backend.rename.service as svc_mod
        calls = []

        def _fake_cancel(self):
            calls.append(True)
            return {"ok": True}

        monkeypatch.setattr(svc_mod.RenameService, "cancel_apply", _fake_cancel)
        r = client.post("/rename/apply/cancel")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert calls == [True]

    def test_apply_then_undo_via_api(self, client, tmp_path):
        src = tmp_path / "src.mkv"; src.write_text("x")
        dest = tmp_path / "lib"
        jid = _seed_job(status="matched", title="Movie",
                        original_path=str(src), destination_path=str(dest),
                        new_filename="Movie (2020).mkv")
        # default move_method is hardlink → source remains, link created.
        # Applies are queued to a background worker now; wait for it to land.
        assert client.post(f"/rename/jobs/{jid}/apply").status_code == 200
        assert _wait_settled(client, jid) == "applied"
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
        assert r["queued"] == 2                      # both accepted for background apply
        assert _wait_settled(client, ok) == "applied"
        assert _wait_settled(client, bad) == "failed"
        jobs = {j["id"]: j for j in client.get("/rename/jobs").json()["jobs"]}
        assert jobs[bad]["error_message"]            # the failure reason survives

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

    # ------------------------------------------------------------------
    # Task 2: POST /rename/jobs/bulk/archive, /unarchive + archived filter
    # ------------------------------------------------------------------

    def test_bulk_archive_archives_jobs(self, client):
        jid = _seed_job(status="matched", title="M")
        r = client.post("/rename/jobs/bulk/archive", json={"ids": [jid]}).json()
        assert r["archived"] == 1
        # Archived jobs drop out of the default (non-archived) listing.
        assert client.get("/rename/jobs").json()["jobs"] == []
        archived_jobs = client.get("/rename/jobs?archived=true").json()["jobs"]
        assert [j["id"] for j in archived_jobs] == [jid]
        assert archived_jobs[0]["archived_at"] is not None

    def test_bulk_archive_skips_applying_status(self, client):
        # The transient 'applying' state must never be archived by a manual
        # bulk action -- guards the same skip rule Task 1 baked into the SQL.
        jid = _seed_job(status="applying", title="M")
        r = client.post("/rename/jobs/bulk/archive", json={"ids": [jid]}).json()
        assert r["archived"] == 0
        jobs = client.get("/rename/jobs").json()["jobs"]
        assert jobs and jobs[0]["archived_at"] is None

    def test_bulk_unarchive_restores_jobs(self, client):
        jid = _seed_job(status="matched", title="M")
        client.post("/rename/jobs/bulk/archive", json={"ids": [jid]})
        r = client.post("/rename/jobs/bulk/unarchive", json={"ids": [jid]}).json()
        assert r["unarchived"] == 1
        jobs = client.get("/rename/jobs").json()["jobs"]
        assert [j["id"] for j in jobs] == [jid]
        assert jobs[0]["archived_at"] is None

    def test_list_jobs_default_excludes_archived(self, client):
        jid = _seed_job(status="matched", title="M")
        client.post("/rename/jobs/bulk/archive", json={"ids": [jid]})
        resp = client.get("/rename/jobs")
        assert resp.status_code == 200
        assert jid not in [j["id"] for j in resp.json()["jobs"]]

    def test_list_jobs_archived_true_returns_archived(self, client):
        jid = _seed_job(status="matched", title="M")
        client.post("/rename/jobs/bulk/archive", json={"ids": [jid]})
        resp = client.get("/rename/jobs?archived=true")
        assert resp.status_code == 200
        assert jid in [j["id"] for j in resp.json()["jobs"]]

    def test_rename_status_reports_archived_count(self, client):
        jid = _seed_job(status="matched", title="M")
        client.post("/rename/jobs/bulk/archive", json={"ids": [jid]})
        resp = client.get("/rename/status")
        assert resp.status_code == 200
        assert resp.json()["archived"] == 1

    def test_bulk_set_destination_applied_job_not_regressed(self, tmp_path):
        # An already-applied job must not have its status changed back to matched.
        root = str(tmp_path / "movies")
        with _client_with_library(root) as client:
            jid = _seed_job(status="applied", title="Done", media_type="movie",
                            destination_path="/lib/movies/Done (2020).mkv",
                            new_filename="Done (2020).mkv")
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

    def test_bulk_set_destination_guard_enforced(self, tmp_path):
        # Movie job, valid (configured) root -> rebuilt destination under root.
        root = str(tmp_path / "movies")
        with _client_with_library(root) as client:
            jid = _seed_job(status="matched", title="The Matrix", year=1999,
                            media_type="movie", resolution="1080p",
                            new_filename="The Matrix (1999) [1080p].mkv")
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
    # A3: path confinement — bulk/set-destination must reject roots outside
    # the configured library allowlist.
    # ------------------------------------------------------------------

    def test_bulk_set_destination_rejects_unconfigured_root(self, client, tmp_path):
        # No auto_rename_movie_library configured (the shared `client` fixture
        # doesn't set one) — any non-empty root must be rejected, not silently
        # honored.
        jid = _seed_job(status="matched", title="M", media_type="movie")
        root = str(tmp_path / "movies")
        resp = client.post("/rename/jobs/bulk/set-destination",
                           json={"ids": [jid], "destination_root": root})
        assert resp.status_code == 422

    def test_bulk_set_destination_rejects_escape_outside_configured_root(self, tmp_path):
        root = str(tmp_path / "movies")
        escape = str(tmp_path / "movies-evil")  # sibling dir, not a subpath
        with _client_with_library(root) as client:
            jid = _seed_job(status="matched", title="M", media_type="movie")
            resp = client.post("/rename/jobs/bulk/set-destination",
                               json={"ids": [jid], "destination_root": escape})
            assert resp.status_code == 422

    def test_bulk_set_destination_rejects_relative_traversal(self, tmp_path):
        root = str(tmp_path / "movies")
        with _client_with_library(root) as client:
            jid = _seed_job(status="matched", title="M", media_type="movie")
            escape = os.path.join(root, "..", "..", "etc")
            resp = client.post("/rename/jobs/bulk/set-destination",
                               json={"ids": [jid], "destination_root": escape})
            assert resp.status_code == 422

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
        assert r["queued"] == 1 and r["skipped"] == 0
        assert _wait_settled(client, jid) == "applied"
        assert (dest / "Ok (2020).mkv").exists()

    def test_apply_confident_skips_matched_94(self, client):
        jid = _seed_job(status="matched", title="Low", match_confidence=94)
        r = client.post("/rename/jobs/apply-confident", json={}).json()
        assert r["queued"] == 0 and r["skipped"] == 1

    def test_apply_confident_skips_needs_review_99(self, client):
        jid = _seed_job(status="needs_review", title="NR", match_confidence=99)
        r = client.post("/rename/jobs/apply-confident", json={}).json()
        assert r["queued"] == 0 and r["skipped"] == 1

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
        assert r["queued"] == 1
        assert _wait_settled(client, a) == "applied"
        # B (also confident) must be untouched.
        jobs = {j["id"]: j for j in client.get("/rename/jobs").json()["jobs"]}
        assert jobs[b]["status"] == "matched"

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
        assert r["queued"] == 0 and r["skipped"] == 0
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
        assert r["queued"] == 1 and r["skipped"] == 0
        assert _wait_settled(client, jid) == "applied"
        assert (dest / "Boundary (2020).mkv").exists()


class TestPathConfinement:
    """A3: process-folder / dv-import must confine caller-supplied paths to an
    allowlist instead of accepting an arbitrary filesystem path."""

    # ── /rename/process-folder ────────────────────────────────────────

    def test_process_folder_in_configured_root_allowed(self, tmp_path):
        root = tmp_path / "movies"
        root.mkdir()
        with _client_with_library(str(root)) as client:
            resp = client.post("/rename/process-folder", json={"folder": str(root)})
            assert resp.status_code == 200
            assert resp.json()["status"] == "started"

    def test_process_folder_notification_reports_skipped_count(
            self, tmp_path, monkeypatch):
        """Process-folder success text remains valid on every supported Python."""
        import backend.api.routes.rename as rename_routes
        import backend.rename.service as svc_mod

        root = tmp_path / "movies"
        root.mkdir()
        events = []

        class _ImmediateThread:
            def __init__(self, target, *args, **kwargs):
                self._target = target

            def start(self):
                self._target()

        # TestClient must create its AnyIO portal with the real stdlib Thread.
        # Scope the synchronous route-thread replacement inside the live client
        # context, and restore it before TestClient begins shutdown.
        with _client_with_library(str(root)) as client:
            with monkeypatch.context() as scoped_patch:
                scoped_patch.setattr(
                    svc_mod.RenameService,
                    "process_folder",
                    lambda self, folder, dry_run=False: {
                        "created": 2,
                        "found": 5,
                        "skipped": 3,
                    },
                )
                scoped_patch.setattr(
                    rename_routes.ws_manager,
                    "broadcast_sync",
                    events.append,
                )
                scoped_patch.setattr(
                    rename_routes.threading,
                    "Thread",
                    _ImmediateThread,
                )

                response = client.post(
                    "/rename/process-folder",
                    json={"folder": str(root)},
                )

        assert response.status_code == 200
        notification = next(
            event for event in events
            if event.get("type") == "notification"
        )
        assert notification["data"]["body"] == (
            "2 new rename job(s) from 5 file(s), 3 already tracked"
        )
        assert notification["data"]["priority"] == "normal"

    def test_process_folder_rejects_when_no_library_configured(self, client, tmp_path):
        folder = tmp_path / "movies"
        folder.mkdir()
        resp = client.post("/rename/process-folder", json={"folder": str(folder)})
        assert resp.status_code == 422

    def test_process_folder_rejects_sibling_escape(self, tmp_path):
        root = tmp_path / "movies"
        root.mkdir()
        escape = tmp_path / "movies-evil"
        escape.mkdir()
        with _client_with_library(str(root)) as client:
            resp = client.post("/rename/process-folder", json={"folder": str(escape)})
            assert resp.status_code == 422

    def test_process_folder_rejects_relative_traversal(self, tmp_path):
        root = tmp_path / "movies"
        root.mkdir()
        with _client_with_library(str(root)) as client:
            escape = os.path.join(str(root), "..", "..", "etc")
            resp = client.post("/rename/process-folder", json={"folder": escape})
            assert resp.status_code == 422

    # ── /rename/dv-import ─────────────────────────────────────────────

    def test_dv_import_default_path_allowed(self, client, monkeypatch):
        # No host_db_path supplied → falls back to the configured default
        # data-dir path, which must always be in-allowlist by construction.
        import backend.api.routes.rename as rename_routes
        calls = []
        monkeypatch.setattr(rename_routes, "import_dv_host_db",
                            lambda db, path: calls.append(path) or {"ok": True})
        resp = client.post("/rename/dv-import", json={})
        assert resp.status_code == 200
        # The route normalizes the path (os.path.normpath) before passing it
        # on, so compare normalized rather than the raw configured string.
        assert calls == [os.path.normpath(rename_routes._DEFAULT_DV_HOST_DB)]

    def test_dv_import_rejects_path_outside_data_dir(self, client, tmp_path):
        outside = str(tmp_path / "elsewhere" / "dv_host.db")
        resp = client.post("/rename/dv-import", json={"host_db_path": outside})
        assert resp.status_code == 422

    def test_dv_import_allows_path_inside_data_dir(self, client, monkeypatch, tmp_path):
        import backend.api.routes.rename as rename_routes
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = str(data_dir / "dv_host.db")
        monkeypatch.setattr(rename_routes, "_DEFAULT_DV_HOST_DB", str(data_dir / "default.db"))
        calls = []
        monkeypatch.setattr(rename_routes, "import_dv_host_db",
                            lambda db, path: calls.append(path) or {"ok": True})
        resp = client.post("/rename/dv-import", json={"host_db_path": db_path})
        assert resp.status_code == 200
        assert calls == [db_path]

    def test_dv_import_rejects_traversal_out_of_data_dir(self, monkeypatch, tmp_path, client):
        import backend.api.routes.rename as rename_routes
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(rename_routes, "_DEFAULT_DV_HOST_DB", str(data_dir / "default.db"))
        escape = os.path.join(str(data_dir), "..", "..", "etc", "passwd")
        resp = client.post("/rename/dv-import", json={"host_db_path": escape})
        assert resp.status_code == 422


class TestTrashEndpoints:
    """/rename/trash (list) and /rename/trash/restore."""

    def _trash_one(self, tmp_path, monkeypatch, filename="movie.mkv", content="x"):
        trash_root = tmp_path / "trash"
        monkeypatch.setattr(fileops, "_TRASH_ROOT", str(trash_root))
        monkeypatch.setattr(fileops, "_trash_root_for", lambda path: str(trash_root))
        monkeypatch.setattr(fileops, "_trash_bucket_name", lambda: "20260101-000000")
        f = tmp_path / filename
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        trashed_path = fileops._trash(str(f))
        return trash_root, trashed_path, f

    def test_list_shows_trashed_file_with_original_path(self, client, tmp_path, monkeypatch):
        _, _, original = self._trash_one(tmp_path, monkeypatch)
        body = client.get("/rename/trash").json()
        entries = [e for e in body["entries"] if e["name"] == "movie.mkv"]
        assert len(entries) == 1
        assert entries[0]["original_path"] == os.path.abspath(str(original))
        assert entries[0]["restorable"] is True

    def test_restore_puts_file_back_and_removes_manifest_record(self, client, tmp_path, monkeypatch):
        trash_root, trashed_path, original = self._trash_one(tmp_path, monkeypatch)
        assert not original.exists()

        resp = client.post("/rename/trash/restore",
                           json={"bucket": "20260101-000000", "name": "movie.mkv"})
        assert resp.status_code == 200
        assert original.exists()
        assert not os.path.exists(trashed_path)

        body = client.get("/rename/trash").json()
        assert not any(e["name"] == "movie.mkv" for e in body["entries"])

    def test_restore_occupied_destination_errors_and_keeps_file_in_trash(
            self, client, tmp_path, monkeypatch):
        trash_root, trashed_path, original = self._trash_one(tmp_path, monkeypatch)
        original.write_text("occupied")  # something now occupies the original path

        resp = client.post("/rename/trash/restore",
                           json={"bucket": "20260101-000000", "name": "movie.mkv"})
        assert resp.status_code == 409
        assert os.path.isfile(trashed_path)
        assert original.read_text() == "occupied"

    def test_restore_traversal_attempt_rejected(self, client, tmp_path, monkeypatch):
        self._trash_one(tmp_path, monkeypatch)
        resp = client.post("/rename/trash/restore",
                           json={"bucket": "20260101-000000", "name": "../escape.mkv"})
        assert resp.status_code == 400

    def test_restore_missing_entry_errors(self, client, tmp_path, monkeypatch):
        trash_root = tmp_path / "trash"
        monkeypatch.setattr(fileops, "_TRASH_ROOT", str(trash_root))
        resp = client.post("/rename/trash/restore",
                           json={"bucket": "20260101-999999", "name": "ghost.mkv"})
        assert resp.status_code == 404


class TestStartupCorruptionNotify:
    """_init_services() surfaces a pending DB corruption flag exactly once,
    after the notification bridge exists."""

    def test_corruption_check_runs_at_startup_with_bridge_and_db_path(self, monkeypatch):
        calls = []

        def _fake_notify(db_path, bridge):
            calls.append((db_path, bridge))
            return False

        monkeypatch.setattr("backend.database.notify_db_corruption_once", _fake_notify)
        app = create_app(config_override={"plex_url": "", "plex_token": ""})
        with TestClient(app):
            pass

        assert len(calls) == 1
        db_path, bridge = calls[0]
        assert db_path  # a real path was passed, not None
        assert bridge is not None  # the notification bridge, not a stub
