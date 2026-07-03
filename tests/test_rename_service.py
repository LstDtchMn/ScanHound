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
            dm = DatabaseManager(); dm.clear_rename_jobs(); dm.clear_dv_scans(); dm.close()
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


def _show_search(title, year, media_type):
    # TV-shaped result (name / first_air_date) for the library-guard tests.
    return [{"id": 100, "name": "Test Show", "first_air_date": "2020-01-01"}]


def _matrix_search_poster(title, year, media_type):
    """Like _matrix_search but the TMDB result carries a poster_path."""
    return [{
        "id": 603, "title": "The Matrix", "original_title": "The Matrix",
        "release_date": "1999-03-31", "poster_path": "/matrix.jpg",
    }]


def _no_poster_search(title, year, media_type):
    """A movie result with poster_path entirely absent."""
    return [{
        "id": 603, "title": "The Matrix", "original_title": "The Matrix",
        "release_date": "1999-03-31",
    }]


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
    d.mkdir(parents=True, exist_ok=True)
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

    def test_dedup_by_path(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.mkv")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        first = svc.process_package("dup", save_to)
        second = svc.process_package("dup", save_to)
        assert len(first) == 1 and second == []

    def test_partial_batch_resumes_on_second_call(self, db, tmp_path):
        """A second call for the same package must process only the files that
        don't yet have a job — so an interrupted batch completes instead of
        being silently skipped (the old package-level coarse check would skip
        all remaining files once any job existed)."""
        d = tmp_path / "extracted"
        d.mkdir()
        (d / "file1.mkv").write_text("x")
        (d / "file2.mkv").write_text("x")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        # Simulate partial first run: manually create a job for file1 only.
        db.create_rename_job({
            "original_path": str(d / "file1.mkv"),
            "original_filename": "file1.mkv",
            "package_name": "pkg",
            "status": "matched",
        })
        # Second call should pick up file2 (not already tracked) and skip file1.
        ids = svc.process_package("pkg", str(d))
        assert len(ids) == 1
        job = db.get_rename_job(ids[0])
        assert "file2" in job["original_filename"]

    def test_claim_path_guards_inflight_and_tracked(self, db, tmp_path):
        # The in-flight reservation that stops process_package and process_folder
        # double-creating a job for the same file when they overlap.
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        p = str(tmp_path / "x.mkv")
        assert svc._claim_path(p) is True    # first claim wins
        assert svc._claim_path(p) is False   # already in-flight → blocked
        svc._release_path(p)
        assert svc._claim_path(p) is True    # released → claimable again
        svc._release_path(p)
        db.create_rename_job({"original_path": p, "original_filename": "x.mkv",
                              "status": "matched"})
        assert svc._claim_path(p) is False   # now tracked in DB → blocked

    def test_auto_apply_when_confirmation_not_required(self, db, tmp_path):
        """Guard 1: an automatic apply (no per-item user confirmation) must
        never consume the source, even when the configured method is
        'move' — it degrades to hardlink/copy instead."""
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        lib = str(tmp_path / "lib")
        ids = _service(db, _matrix_search, movie_lib=lib,
                       auto_rename_require_confirmation=False).process_package("pa", save_to)
        job = db.get_rename_job(ids[0])
        assert job["status"] == "applied"
        assert os.path.exists(src)  # automatic apply never consumes the source
        assert os.path.isfile(os.path.join(lib, "The Matrix (1999)", "The Matrix (1999) [1080p].mkv"))

    def test_normalize_candidate_retains_poster_path(self):
        from backend.rename.service import RenameService
        r = {"id": 603, "title": "The Matrix", "release_date": "1999-03-31",
             "poster_path": "/matrix.jpg"}
        cand = RenameService._normalize_candidate(r, "movie")
        assert cand["poster_path"] == "/matrix.jpg"

    def test_normalize_candidate_poster_path_absent_is_none(self):
        from backend.rename.service import RenameService
        r = {"id": 603, "title": "The Matrix", "release_date": "1999-03-31"}
        cand = RenameService._normalize_candidate(r, "movie")
        assert cand["poster_path"] is None

    def test_identified_job_persists_poster_path(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.BluRay.x264.mkv")
        ids = _service(db, _matrix_search_poster,
                       movie_lib=str(tmp_path / "lib")).process_package("pkg1", save_to)
        job = db.get_rename_job(ids[0])
        assert job["poster_path"] == "/matrix.jpg"

    def test_job_without_poster_stores_null(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.BluRay.x264.mkv")
        ids = _service(db, _no_poster_search,
                       movie_lib=str(tmp_path / "lib")).process_package("pkg1", save_to)
        job = db.get_rename_job(ids[0])
        assert job["poster_path"] is None


# ── DV folder scan accounting ─────────────────────────────────────────

class TestDvFolderScan:
    def test_every_file_accounted_including_failures(self, db, tmp_path, monkeypatch):
        import backend.rename.service as svc_mod
        d = tmp_path / "v"; d.mkdir()
        (d / "a.mkv").write_text("x")
        (d / "b.mkv").write_text("x")
        svc = _service(db, _matrix_search)
        monkeypatch.setattr(svc_mod._dv, "available", lambda: True)

        def fake_detect(path):
            if path.endswith("a.mkv"):
                return {"layer": "fel", "tool": True, "error": None}
            raise OSError("boom")  # b.mkv fails detection
        monkeypatch.setattr(svc_mod._dv, "detect_layer", fake_detect)

        seen = []
        out = svc.scan_folder_dv(
            str(d), progress_cb=lambda done, total, p, layer: seen.append(done))
        assert out["found"] == 2
        # found == scanned + skipped always holds, even with a failure
        assert out["scanned"] + out["skipped"] == 2
        assert out["scanned"] == 2 and out["skipped"] == 0
        assert len(seen) == 2  # progress fired for every file
        counts = db.count_dv_scans_by_layer()
        assert counts.get("fel") == 1
        assert counts.get("unknown") == 1  # the failed file recorded, not dropped

    def test_skip_unchanged_on_second_pass(self, db, tmp_path, monkeypatch):
        import backend.rename.service as svc_mod
        d = tmp_path / "v"; d.mkdir()
        (d / "a.mkv").write_text("x")
        svc = _service(db, _matrix_search)
        monkeypatch.setattr(svc_mod._dv, "available", lambda: True)
        monkeypatch.setattr(svc_mod._dv, "detect_layer",
                            lambda p: {"layer": "mel", "tool": True, "error": None})
        first = svc.scan_folder_dv(str(d))
        assert first["scanned"] == 1 and first["skipped"] == 0
        second = svc.scan_folder_dv(str(d))  # unchanged → skipped
        assert second["scanned"] == 0 and second["skipped"] == 1


# ── library-not-configured guard ──────────────────────────────────────

class TestLibraryGuard:
    def test_tv_match_without_library_needs_review(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "Test.Show.S01E01.1080p.WEB-DL.mkv")
        # tv library unset → a confident TV match must NOT auto-place into a
        # broken relative path; hold for review with a clear reason.
        svc = _service(db, _show_search, tv_lib="")
        ids = svc.process_package("tvpkg", save_to)
        job = db.get_rename_job(ids[0])
        assert job["status"] == "needs_review"
        assert "TV library not configured" in (job["warning_message"] or "")

    def test_movie_match_without_library_needs_review(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        svc = _service(db, _matrix_search, movie_lib="")  # movie library unset
        ids = svc.process_package("mpkg", save_to)
        job = db.get_rename_job(ids[0])
        assert job["status"] == "needs_review"
        assert "Movie library not configured" in (job["warning_message"] or "")

    def test_configured_library_still_matches(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        job = db.get_rename_job(svc.process_package("ok", save_to)[0])
        assert job["status"] == "matched"  # guard doesn't fire when set


# ── resolution=None -> ffprobe width fallback routing ──────────────────

class TestResolutionProbe:
    def test_unknown_resolution_probed_as_4k_routes_to_4k_library(
            self, db, tmp_path, monkeypatch):
        """A plain filename with no resolution tag that's actually a 4K file
        (ffprobe reports width >= 3000) must route to the 4K library, not
        silently land in the 1080p one."""
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.mkv")
        monkeypatch.setattr(
            "backend.rename.llm_identify.probe_video_width",
            lambda path, timeout=30: 3840)
        lib = str(tmp_path / "lib")
        lib_4k = str(tmp_path / "lib-4k")
        svc = _service(db, _matrix_search, movie_lib=lib,
                       auto_rename_movie_library_4k=lib_4k)
        job = db.get_rename_job(svc.process_package("pkg", save_to)[0])
        assert job["destination_path"].startswith(lib_4k)
        assert job["resolution"] == "2160p"

    def test_ffprobe_failure_falls_back_to_1080p_no_crash(
            self, db, tmp_path, monkeypatch):
        """If ffprobe errors/times out/can't read the file, routing must fall
        back to the CURRENT (non-4K) behavior — never raise."""
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.mkv")

        def _boom(path, timeout=30):
            raise RuntimeError("ffprobe exploded")
        monkeypatch.setattr(
            "backend.rename.llm_identify.probe_video_width", _boom)
        lib = str(tmp_path / "lib")
        lib_4k = str(tmp_path / "lib-4k")
        svc = _service(db, _matrix_search, movie_lib=lib,
                       auto_rename_movie_library_4k=lib_4k)
        job = db.get_rename_job(svc.process_package("pkg", save_to)[0])
        assert job["destination_path"].startswith(lib)
        assert not job["destination_path"].startswith(lib_4k)

    def test_already_tagged_resolution_skips_probe(self, db, tmp_path, monkeypatch):
        """Probing is only for unknown resolution — a file already tagged
        (e.g. 2160p) must not trigger an extra subprocess call."""
        save_to, _ = _extracted(tmp_path, "The.Matrix.1999.2160p.mkv")
        called = {"n": 0}

        def _spy(path, timeout=30):
            called["n"] += 1
            return 3840
        monkeypatch.setattr(
            "backend.rename.llm_identify.probe_video_width", _spy)
        lib = str(tmp_path / "lib")
        lib_4k = str(tmp_path / "lib-4k")
        svc = _service(db, _matrix_search, movie_lib=lib,
                       auto_rename_movie_library_4k=lib_4k)
        job = db.get_rename_job(svc.process_package("pkg", save_to)[0])
        assert called["n"] == 0
        assert job["destination_path"].startswith(lib_4k)  # already 2160p, routes fine


# ── TMDB year mismatch vs. parsed filename year ─────────────────────────

def _carolina_search_2026(title, year, media_type):
    """TMDB's chosen match has year 2026, one off the filename's 2025 — a
    gap of 1 isn't penalized by confidence scoring, so this stays 'matched'."""
    return [{"id": 999, "title": "Carolina Caroline", "release_date": "2026-01-15"}]


def _carolina_search_2025(title, year, media_type):
    return [{"id": 999, "title": "Carolina Caroline", "release_date": "2025-01-15"}]


class TestYearMismatchWarning:
    def test_match_year_differs_from_filename_year_warns(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "Carolina.Caroline.2025.1080p.mkv")
        svc = _service(db, _carolina_search_2026, movie_lib=str(tmp_path / "lib"))
        job = db.get_rename_job(svc.process_package("pkg", save_to)[0])
        assert job["year"] == 2026
        assert job["status"] == "matched"  # purely additive — must not block
        assert job["warning_message"], "expected a warning to be attached"
        assert "2025" in job["warning_message"]
        assert "2026" in job["warning_message"]

    def test_match_year_equal_to_filename_year_no_warning(self, db, tmp_path):
        save_to, _ = _extracted(tmp_path, "Carolina.Caroline.2025.1080p.mkv")
        svc = _service(db, _carolina_search_2025, movie_lib=str(tmp_path / "lib"))
        job = db.get_rename_job(svc.process_package("pkg", save_to)[0])
        assert job["year"] == 2025
        assert job["status"] == "matched"
        assert not job["warning_message"]


# ── duplicate-destination conflict detection ──────────────────────────

class TestDestinationConflict:
    def _job(self, jid, status, dest, name):
        return {"id": jid, "status": status,
                "destination_path": dest, "new_filename": name}

    def test_two_active_jobs_same_dest_both_flagged(self):
        from backend.rename.service import destination_conflict_ids
        jobs = [
            self._job(1, "matched", "/lib", "Movie (2020) [2160p].mkv"),
            self._job(2, "matched", "/lib", "Movie (2020) [2160p].mkv"),
            self._job(3, "matched", "/lib", "Other (2019).mkv"),
        ]
        assert destination_conflict_ids(jobs) == {1, 2}

    def test_applied_job_claims_slot_flagging_active_rival(self):
        from backend.rename.service import destination_conflict_ids
        # An applied job holds the destination on disk: a new matched job landing
        # on the same path IS a conflict (its apply would collide). The applied
        # job itself is done, so it's not flagged; the active rival is.
        jobs = [
            self._job(1, "applied", "/lib", "M.mkv"),
            self._job(2, "matched", "/lib", "M.mkv"),
            self._job(3, "failed", "/lib", "M.mkv"),  # released the slot
        ]
        assert destination_conflict_ids(jobs) == {2}

    def test_lone_applied_job_is_not_flagged(self):
        from backend.rename.service import destination_conflict_ids
        jobs = [self._job(1, "applied", "/lib", "M.mkv")]
        assert destination_conflict_ids(jobs) == set()

    def test_failed_and_reverted_do_not_claim(self):
        from backend.rename.service import destination_conflict_ids
        jobs = [
            self._job(1, "failed", "/lib", "M.mkv"),
            self._job(2, "reverted", "/lib", "M.mkv"),
            self._job(3, "matched", "/lib", "M.mkv"),
        ]
        # Only one active claimant on the slot → no conflict.
        assert destination_conflict_ids(jobs) == set()

    def test_match_is_case_and_separator_insensitive(self):
        from backend.rename.service import destination_conflict_ids
        jobs = [
            self._job(1, "matched", "/Lib\\Movies", "M.mkv"),
            self._job(2, "needs_review", "/lib/movies", "m.MKV"),
        ]
        assert destination_conflict_ids(jobs) == {1, 2}

    def test_jobs_without_destination_are_ignored(self):
        from backend.rename.service import destination_conflict_ids
        jobs = [
            self._job(1, "needs_review", None, None),
            self._job(2, "needs_review", None, None),
        ]
        assert destination_conflict_ids(jobs) == set()


# ── duplicate "keep which?" recommendation ────────────────────────────

class TestKeepRecommendation:
    def _job(self, jid, name, *, status="matched", res="2160p", dest="/lib"):
        return {"id": jid, "status": status, "original_filename": name,
                "resolution": res, "destination_path": dest,
                "new_filename": "Movie (2026) [2160p].mkv"}

    def test_dolby_vision_beats_non_dv_same_resolution(self):
        from backend.rename.service import recommend_keep
        a = self._job(1, "Finding.Emily.2026.2160p.AMZN.WEB-DL.DDP5.1.Atmos.H265-MADSKY.mkv")
        b = self._job(7, "Finding.Emily.2026.Hybrid.2160p.WEB-DL.DV.HDR.DDP5.1.Atmos.H265-AOC.mkv")
        assert recommend_keep([a, b]) == 7  # b carries Dolby Vision

    def test_remux_beats_webdl(self):
        from backend.rename.service import recommend_keep
        a = self._job(1, "Movie.2020.2160p.WEB-DL.DV.HDR.mkv")
        b = self._job(2, "Movie.2020.2160p.BluRay.REMUX.DV.HDR.mkv")
        assert recommend_keep([a, b]) == 2

    def test_higher_resolution_wins_over_better_source(self):
        from backend.rename.service import recommend_keep
        a = self._job(1, "Movie.2020.1080p.BluRay.REMUX.mkv", res="1080p")
        b = self._job(2, "Movie.2020.2160p.WEB-DL.mkv", res="2160p")
        assert recommend_keep([a, b]) == 2  # resolution dominates

    def test_identical_quality_is_a_tie(self):
        from backend.rename.service import recommend_keep
        a = self._job(1, "Movie.2020.2160p.WEB-DL.DV.HDR.Atmos.mkv")
        b = self._job(2, "Movie.2020.2160p.WEB-DL.DV.HDR.Atmos.mkv")
        assert recommend_keep([a, b]) is None  # no clear winner

    def test_annotations_mark_single_keeper_with_reason(self):
        from backend.rename.service import conflict_annotations
        a = self._job(1, "Movie.2020.2160p.WEB-DL.mkv")
        b = self._job(2, "Movie.2020.2160p.BluRay.REMUX.DV.HDR.TrueHD.mkv")
        ann = conflict_annotations([a, b])
        assert ann[1]["destination_conflict"] and ann[2]["destination_conflict"]
        assert ann[2]["keep_recommended"] is True
        assert ann[1]["keep_recommended"] is False
        assert ann[2]["keep_reason"]  # non-empty reason string

    def test_no_recommendation_outside_a_conflict(self):
        from backend.rename.service import conflict_annotations
        a = self._job(1, "Movie.2020.2160p.WEB-DL.mkv")
        a["new_filename"] = "Alone (2020).mkv"
        assert conflict_annotations([a]) == {}

    def test_camera_dv_not_mistaken_for_dolby_vision(self):
        # "DV.Cam" / "dv-rip" are camera formats, NOT Dolby Vision — they must
        # not win over a real (non-DV-but-otherwise-equal) release.
        from backend.rename.service import _quality_score, _quality_reason, recommend_keep
        cam = self._job(1, "Movie.2020.2160p.WEB-DL.DV.Cam.mkv")
        rip = self._job(2, "Movie.2020.2160p.WEB-DL.dv-rip.mkv")
        plain = self._job(3, "Movie.2020.2160p.WEB-DL.mkv")
        # dv flag (index 1 of the score tuple) is 0 for the camera/rip formats
        assert _quality_score(cam)[1] == 0
        assert _quality_score(rip)[1] == 0
        assert "Dolby Vision" not in _quality_reason(cam)
        # a genuine DV release still scores the DV bit
        real_dv = self._job(4, "Movie.2020.2160p.WEB-DL.DV.HDR.mkv")
        assert _quality_score(real_dv)[1] == 1

    def test_applied_best_release_suppresses_active_keeper(self):
        # If the best copy is already APPLIED (in the library), no active rival
        # should be marked "keep" over it.
        from backend.rename.service import conflict_annotations
        applied = self._job(1, "Movie.2020.2160p.BluRay.REMUX.DV.HDR.TrueHD.mkv",
                            status="applied")
        active = self._job(2, "Movie.2020.2160p.WEB-DL.mkv")  # worse, active
        ann = conflict_annotations([applied, active])
        assert ann[2]["destination_conflict"] is True
        assert ann[2]["keep_recommended"] is False  # the applied REMUX is better
        assert 1 not in ann  # applied job is never annotated

    def test_quality_score_tolerates_non_string_fields(self):
        from backend.rename.service import _quality_score
        # SQLite could hand back a non-string; must not raise.
        j = {"id": 1, "status": "matched", "original_filename": 12345,
             "resolution": 1080, "destination_path": "/l", "new_filename": "x.mkv"}
        assert isinstance(_quality_score(j), tuple)


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

    def test_apply_automatic_true_forces_hardlink_not_move(self, db, tmp_path):
        """Guard 1, exercised directly on RenameService.apply(): passing
        automatic=True must not consume the source even with
        auto_rename_move_method='move'."""
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        lib = str(tmp_path / "lib")
        svc = _service(db, _matrix_search, movie_lib=lib)
        jid = svc.process_package("pkg", save_to)[0]
        out = svc.apply(jid, automatic=True)
        assert out["ok"] is True
        job = db.get_rename_job(jid)
        assert job["status"] == "applied"
        assert job["move_method"] in ("hardlink", "copy")
        assert os.path.exists(src)

    def test_apply_second_of_two_colliding_jobs_needs_review_not_failed(self, db, tmp_path):
        """Two source files that resolve to the identical destination (same
        title/year/resolution/filename) — e.g. two different releases of the
        same movie. Applying the first succeeds; applying the second must NOT
        hard-fail with FileExistsError. It should land needs_review with a
        clear warning, and the first job/file must be untouched."""
        second_root = tmp_path / "second"
        second_root.mkdir()
        save_to1, src1 = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        save_to2, src2 = _extracted(
            second_root, "The.Matrix.1999.1080p.Alt.Release.mkv")
        lib = str(tmp_path / "lib")
        svc = _service(db, _matrix_search, movie_lib=lib)
        jid1 = svc.process_package("pkg1", save_to1)[0]
        jid2 = svc.process_package("pkg2", save_to2)[0]
        job1 = db.get_rename_job(jid1)
        job2 = db.get_rename_job(jid2)
        # Sanity: both jobs really do target the identical destination file.
        assert job1["destination_path"] == job2["destination_path"]
        assert job1["new_filename"] == job2["new_filename"]

        out1 = svc.apply(jid1)
        assert out1["ok"] is True
        applied_job1 = db.get_rename_job(jid1)
        assert applied_job1["status"] == "applied"
        dst = os.path.join(job1["destination_path"], job1["new_filename"])
        assert os.path.isfile(dst)

        out2 = svc.apply(jid2)
        job2_after = db.get_rename_job(jid2)
        assert job2_after["status"] == "needs_review"
        assert job2_after["status"] != "failed"
        assert "already exists" in (job2_after["warning_message"] or "").lower()
        # The first job/file must be untouched by the second's failed apply.
        assert db.get_rename_job(jid1)["status"] == "applied"
        assert os.path.isfile(dst)
        # Source of the second file must remain in place (nothing deleted).
        assert os.path.exists(src2)

    def test_apply_to_empty_destination_still_succeeds(self, db, tmp_path):
        """Non-colliding apply must be unaffected by the new collision guard."""
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        lib = str(tmp_path / "lib")
        svc = _service(db, _matrix_search, movie_lib=lib)
        jid = svc.process_package("pkg", save_to)[0]
        out = svc.apply(jid)
        assert out["ok"] is True
        job = db.get_rename_job(jid)
        assert job["status"] == "applied"
        dst = os.path.join(job["destination_path"], job["new_filename"])
        assert os.path.isfile(dst)

    def test_apply_rolls_back_file_when_db_write_fails(self, db, tmp_path):
        """If the 'applied' DB write fails *after* the move, the file must be
        rolled back — otherwise it's orphaned (re-apply sees 'source missing',
        undo sees 'not applied') with no way to recover from the UI.
        """
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        lib = str(tmp_path / "lib")
        svc = _service(db, _matrix_search, movie_lib=lib)
        jid = svc.process_package("pkg", save_to)[0]
        dst = os.path.join(lib, "The Matrix (1999)",
                           db.get_rename_job(jid)["new_filename"])

        real_update = db.update_rename_job

        def _boom(job_id, **fields):
            if fields.get("status") == "applied":
                raise RuntimeError("simulated DB failure")
            return real_update(job_id, **fields)

        db.update_rename_job = _boom
        out = svc.apply(jid)

        assert out["ok"] is False
        # Disk rolled back: source restored, nothing orphaned in the library.
        assert os.path.exists(src)
        assert not os.path.exists(dst)
        # And the row is recorded as failed, not stuck on its old status.
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


# ── accept proposal endpoints ─────────────────────────────────────────

class TestAcceptProposals:
    def _correction_job(self, db, tmp_path):
        save_to, src = _extracted(tmp_path, "The.Show.S01E05.1080p.HDTV.x264.mkv")
        return db.create_rename_job({
            "package_name": "corr-pkg",
            "original_path": src,
            "original_filename": "The.Show.S01E05.1080p.HDTV.x264.mkv",
            "media_type": "tv", "title": "The Show", "year": 2020,
            "season": 1, "episode": 5, "tmdb_id": 1234, "resolution": "1080p",
            "status": "needs_review",
            "suggested_correction": {
                "type": "episode_correction",
                "original": {"season": 1, "episode": 5},
                "proposed": {"season": 1, "episode": 7,
                             "title": "Real Episode Name", "runtime": 42},
                "confidence_gain": 30.0, "method": "runtime",
            },
        })

    def test_accept_correction_uses_proposed_episode_title(self, db, tmp_path):
        jid = self._correction_job(db, tmp_path)
        out = _service(db, _matrix_search, tv_lib=str(tmp_path / "tv")).accept_correction(jid)
        assert out["ok"] is True
        # The corrected episode's TMDB title must appear in the new filename —
        # not the original (wrong) episode's, and not be silently dropped.
        assert out["new_filename"] == "The Show (2020) - S01E07 - Real Episode Name.mkv"
        job = db.get_rename_job(jid)
        assert job["status"] == "matched"
        assert job["season"] == 1 and job["episode"] == 7
        assert job["suggested_correction"] is None

    def test_accept_correction_requires_a_proposal(self, db, tmp_path):
        save_to, src = _extracted(tmp_path, "The.Matrix.1999.1080p.mkv")
        jid = db.create_rename_job({
            "package_name": "p", "original_path": src,
            "original_filename": "The.Matrix.1999.1080p.mkv", "status": "matched"})
        out = _service(db, _matrix_search).accept_correction(jid)
        assert out["ok"] is False

    def test_accept_combined_promotes_without_renaming(self, db, tmp_path):
        save_to, src = _extracted(tmp_path, "Show.S01E01E02.1080p.mkv")
        jid = db.create_rename_job({
            "package_name": "comb", "original_path": src,
            "original_filename": "Show.S01E01E02.1080p.mkv",
            "media_type": "tv", "title": "Show", "season": 1, "episode": 1,
            "new_filename": "Show - S01E01E02.mkv", "status": "needs_review",
            "combined_episode": {"episode_start": 1, "episode_end": 2,
                                 "proposed_code": "E01E02", "runtime_match_pct": 2.0},
        })
        out = _service(db, _matrix_search).accept_combined(jid)
        assert out["ok"] is True
        job = db.get_rename_job(jid)
        assert job["status"] == "matched"
        assert job["combined_episode"] is None
        # accept_combined trusts the filename built at detection time — unchanged.
        assert job["new_filename"] == "Show - S01E01E02.mkv"


# ── rematch: library guard + poster_path + season/episode overrides ────

class _FakeTmdb:
    def __init__(self, details):
        self._details = details

    def details(self, tmdb_id, media_type="movie", language="en-US"):
        return dict(self._details, id=tmdb_id)


class TestRematch:
    def test_rematch_tv_library_unset_needs_review(self, db, monkeypatch):
        svc = _service(db, _matrix_search, tv_lib="")  # TV library unset
        jid = db.create_rename_job({
            "original_path": "/x/show.mkv", "original_filename": "show.mkv",
            "status": "needs_review", "media_type": "tv", "season": 1, "episode": 2})
        monkeypatch.setattr(svc, "_tmdb_client",
            lambda: _FakeTmdb({"name": "The Show", "first_air_date": "2020-01-01",
                               "poster_path": "/show.jpg"}))
        out = svc.rematch(jid, 1234, media_type="tv")
        job = db.get_rename_job(jid)
        assert out["ok"] is True
        assert job["status"] == "needs_review"
        assert job["warning_message"]
        assert job["destination_path"] in (None, "")

    def test_rematch_movie_library_unset_needs_review(self, db, monkeypatch):
        svc = _service(db, _matrix_search, movie_lib="")  # movie library unset
        jid = db.create_rename_job({
            "original_path": "/x/film.mkv", "original_filename": "film.mkv",
            "status": "needs_review", "media_type": "movie"})
        monkeypatch.setattr(svc, "_tmdb_client",
            lambda: _FakeTmdb({"title": "The Matrix", "release_date": "1999-03-30",
                               "poster_path": "/matrix.jpg"}))
        out = svc.rematch(jid, 603, media_type="movie")
        job = db.get_rename_job(jid)
        assert out["ok"] is True
        assert job["status"] == "needs_review"
        assert job["warning_message"]
        assert job["destination_path"] in (None, "")

    def test_rematch_tv_library_set_matched_under_root(self, db, monkeypatch, tmp_path):
        tv = str(tmp_path / "tv")
        svc = _service(db, _matrix_search, tv_lib=tv)
        jid = db.create_rename_job({
            "original_path": "/x/show.mkv", "original_filename": "show.mkv",
            "status": "needs_review", "media_type": "tv", "season": 1, "episode": 2})
        monkeypatch.setattr(svc, "_tmdb_client",
            lambda: _FakeTmdb({"name": "The Show", "first_air_date": "2020-01-01",
                               "poster_path": "/show.jpg"}))
        out = svc.rematch(jid, 1234, media_type="tv")
        job = db.get_rename_job(jid)
        assert job["status"] == "matched"
        assert job["destination_path"].startswith(tv)
        assert job["poster_path"] == "/show.jpg"

    def test_rematch_season_episode_override_changes_filename(self, db, monkeypatch, tmp_path):
        tv = str(tmp_path / "tv")
        svc = _service(db, _matrix_search, tv_lib=tv)
        jid = db.create_rename_job({
            "original_path": "/x/show.mkv", "original_filename": "show.mkv",
            "status": "needs_review", "media_type": "tv", "season": 1, "episode": 2})
        monkeypatch.setattr(svc, "_tmdb_client",
            lambda: _FakeTmdb({"name": "The Show", "first_air_date": "2020-01-01",
                               "poster_path": "/show.jpg"}))
        svc.rematch(jid, 1234, media_type="tv", season=3, episode=7)
        fname = db.get_rename_job(jid)["new_filename"]
        assert "S03E07" in fname

    def test_rematch_poster_path_persisted_from_details(self, db, monkeypatch, tmp_path):
        movie = str(tmp_path / "movies")
        svc = _service(db, _matrix_search, movie_lib=movie)
        jid = db.create_rename_job({
            "original_path": "/x/film.mkv", "original_filename": "film.mkv",
            "status": "needs_review", "media_type": "movie"})
        monkeypatch.setattr(svc, "_tmdb_client",
            lambda: _FakeTmdb({"title": "The Matrix", "release_date": "1999-03-30",
                               "poster_path": "/matrix.jpg"}))
        svc.rematch(jid, 603, media_type="movie")
        job = db.get_rename_job(jid)
        assert job["poster_path"] == "/matrix.jpg"
        assert job["status"] == "matched"


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


# ── Host→container path translation (JDownloader runs on Windows) ─────

class TestTranslatePath:
    def _svc(self, db, mappings):
        return _service(db, _matrix_search, auto_rename_path_mappings=mappings)

    def test_maps_windows_prefix_to_container(self, tmp_path):
        db = DatabaseManager()
        svc = self._svc(db, "F:\Downloads => /library/movies")
        assert svc._translate_path("F:\Downloads\Movie (2020)") == "/library/movies/Movie (2020)"
        db.close()

    def test_longest_prefix_wins(self, tmp_path):
        db = DatabaseManager()
        svc = self._svc(db, "F:\Downloads => /a\nF:\Downloads\4k => /b")
        assert svc._translate_path("F:\Downloads\4k\X") == "/b/X"
        db.close()

    def test_unmapped_path_unchanged(self, tmp_path):
        db = DatabaseManager()
        svc = self._svc(db, "F:\Downloads => /library/movies")
        assert svc._translate_path("Z:\Other\X") == "Z:\Other\X"
        db.close()

    def test_no_mappings_returns_input(self, tmp_path):
        db = DatabaseManager()
        svc = self._svc(db, "")
        assert svc._translate_path("F:\Downloads\X") == "F:\Downloads\X"
        db.close()


# ── process_folder (manual backlog processing) ───────────────────────

class TestProcessFolder:
    def test_creates_jobs_and_dedups(self, tmp_path):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        folder = tmp_path / "dl"
        folder.mkdir()
        (folder / "The.Matrix.1999.1080p.BluRay.mkv").write_text("x")
        result = svc.process_folder(str(folder))
        assert result["found"] == 1
        assert result["created"] == 1
        # Re-running dedups by path — no new jobs.
        again = svc.process_folder(str(folder))
        assert again["created"] == 0 and again["skipped"] == 1
        db.close()

    def test_missing_folder_returns_error(self, tmp_path):
        db = DatabaseManager()
        svc = _service(db, _matrix_search)
        result = svc.process_folder(str(tmp_path / "does-not-exist"))
        assert result["created"] == 0 and "error" in result
        db.close()

    def test_translates_host_path(self, tmp_path):
        db = DatabaseManager()
        real = tmp_path / "container"
        real.mkdir()
        (real / "The.Matrix.1999.mkv").write_text("x")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"),
                       auto_rename_path_mappings="F:\Downloads => " + str(real))
        result = svc.process_folder("F:\Downloads")
        assert result["found"] == 1 and result["created"] == 1
        db.close()


# ── Surfacing dropped rename jobs (DB failure vs legitimate skip) ────

class TestSurfacedDbFailures:
    """A genuine create_rename_job DB failure must be counted distinctly from
    the ordinary 'already has a job for this path' skip — not silently
    dropped as if nothing happened."""

    def test_process_folder_counts_db_failure_separately_from_skip(self, tmp_path):
        from backend.database import RenameJobDBError

        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        folder = tmp_path / "dl"
        folder.mkdir()
        (folder / "The.Matrix.1999.1080p.BluRay.mkv").write_text("x")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db, "create_rename_job",
                       lambda job: (_ for _ in ()).throw(RenameJobDBError("disk full")))
            result = svc.process_folder(str(folder))

        assert result["created"] == 0
        assert result["failed_db"] == 1
        assert result["skipped"] == 0  # not conflated with "already tracked"
        db.close()

    def test_process_folder_normal_already_tracked_path_only_increments_skipped(self, tmp_path):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        folder = tmp_path / "dl"
        folder.mkdir()
        (folder / "The.Matrix.1999.1080p.BluRay.mkv").write_text("x")

        svc.process_folder(str(folder))          # first run: creates the job
        again = svc.process_folder(str(folder))  # second run: already tracked

        assert again["created"] == 0
        assert again["skipped"] == 1
        assert again["failed_db"] == 0
        db.close()

    def test_process_package_counts_db_failure_without_crashing_the_batch(self, tmp_path):
        from backend.database import RenameJobDBError

        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        d = tmp_path / "extracted"
        d.mkdir()
        (d / "file1.mkv").write_text("x")
        (d / "file2.mkv").write_text("x")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db, "create_rename_job",
                       lambda job: (_ for _ in ()).throw(RenameJobDBError("disk full")))
            ids = svc.process_package("pkg", str(d))

        assert ids == []  # nothing created
        assert svc.last_package_failed_db == 2  # both files' DB writes failed, not silently dropped
        db.close()

    def test_process_package_normal_path_leaves_failed_db_at_zero(self, tmp_path):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        d = tmp_path / "extracted"
        d.mkdir()
        (d / "file1.mkv").write_text("x")

        ids = svc.process_package("pkg", str(d))
        assert len(ids) == 1
        assert svc.last_package_failed_db == 0
        db.close()


# ── Identify retry ladder (year-strip, a.k.a.) ───────────────────────

class TestRetryLadder:
    def test_aka_alternate_title_resolves(self):
        db = DatabaseManager()
        def search(title, year, mt):
            if "moving" in (title or "").lower():
                return [{"id": 99, "title": "Moving", "release_date": "1993-03-20"}]
            return []  # the romaji primary "Ohikkoshi" finds nothing
        svc = _service(db, search)
        m = svc._identify("Ohikkoshi_a.k.a._Moving_1993_2160p_WEB-DL_x265.mkv")
        assert m is not None and m["title"] == "Moving" and m["year"] == 1993
        db.close()

    def test_drop_year_retry(self):
        db = DatabaseManager()
        calls = []
        def search(title, year, mt):
            calls.append((title, year))
            if year is None and "kombucha" in title.lower():
                return [{"id": 7, "title": "Kombucha", "release_date": "2025-01-01"}]
            return []  # year-filtered pass misses
        svc = _service(db, search)
        m = svc._identify("Kombucha_2025_2160p_WEB-DL_x265.mkv")
        assert m is not None and m["title"] == "Kombucha"
        assert ("Kombucha", 2025) in calls and ("Kombucha", None) in calls
        db.close()


# ── Re-identify (re-run matcher on an existing job) ──────────────────

class TestReidentify:
    def test_reidentify_replaces_job(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib="/lib")
        jid = svc._process_file(None, "/x/The.Matrix.1999.1080p.mkv")
        assert jid
        r = svc.reidentify(jid)
        assert r["ok"] is True and r["job_id"] and r["job_id"] != jid
        assert db.get_rename_job(jid) is None
        assert db.get_rename_job(r["job_id"]) is not None
        db.close()

    def test_reidentify_missing_job(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search)
        assert svc.reidentify(999999)["ok"] is False
        db.close()

    def test_reidentify_all_counts_reviewable(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib="/lib")
        svc._process_file(None, "/x/The.Matrix.1999.1080p.mkv")
        svc._process_file(None, "/x/Inception.2010.1080p.mkv")
        out = svc.reidentify_all()
        assert out["reidentified"] >= 1
        db.close()


# ── IMDB-id fast path (/find exact resolve) ──────────────────────────

class _FakeFind:
    def __init__(self, movie=None, tv=None):
        self._movie, self._tv = movie or [], tv or []
    def find(self, ext, source="imdb_id"):
        return {"movie_results": self._movie, "tv_results": self._tv}


class TestImdbFastPath:
    _matrix = [{"id": 603, "title": "The Matrix", "release_date": "1999-03-31"}]

    def test_resolve_via_find(self):
        db = DatabaseManager()
        svc = _service(db, _weak_search)
        svc._tmdb_search_override = None  # enable the /find path
        svc._tmdb_client = lambda: _FakeFind(movie=self._matrix)
        m = svc._tmdb_match_imdb("tt0133093", "movie")
        assert m and m["tmdb_id"] == 603 and m["confidence"] == 100.0
        assert m["title"] == "The Matrix"
        db.close()

    def test_identify_short_circuits_on_imdb(self):
        db = DatabaseManager()
        svc = _service(db, _weak_search)  # title search alone would mis-match
        svc._tmdb_search_override = None
        svc._tmdb_client = lambda: _FakeFind(movie=self._matrix)
        m = svc._identify("Whatever.Title.1999.{imdb-tt0133093}.1080p.mkv")
        assert m and m["title"] == "The Matrix" and m["source"] == "imdb_id"
        db.close()

    def test_override_bypasses_imdb_in_tests(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search)
        assert svc._tmdb_match_imdb("tt0133093", "movie") is None
        db.close()


# ── Candidate credit enrichment (cast/director for OCR) ──────────────

class _FakeCredits:
    def credits(self, tid, media_type="movie"):
        return {"cast": [{"name": "Adam Sandler"}, {"name": "Drew Barrymore"}],
                "crew": [{"name": "Lighting Guy", "job": "Gaffer"},
                         {"name": "Peter Segal", "job": "Director"}]}


class TestEnrichWithCredits:
    def test_skipped_in_test_mode(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search)
        cands = [{"title": "X", "tmdb_id": 1, "media_type": "movie"}]
        out = svc._enrich_with_credits(cands)
        assert out is cands and "cast" not in cands[0]
        db.close()

    def test_adds_cast_and_director(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search)
        svc._tmdb_search_override = None
        svc._tmdb_client = lambda: _FakeCredits()
        cands = [{"title": "50 First Dates", "tmdb_id": 1, "media_type": "movie"}]
        svc._enrich_with_credits(cands)
        assert cands[0]["cast"] == ["Adam Sandler", "Drew Barrymore"]
        assert cands[0]["director"] == "Peter Segal"
        db.close()


# ── Heuristic fallbacks never auto-apply ─────────────────────────────

class TestHeuristicSourceGate:
    def test_ocr_source_forced_to_review(self):
        db = DatabaseManager()
        # require_confirmation OFF → a high-conf filename match would auto-apply,
        # but a heuristic (OCR/subtitle/vision) source must still need review.
        svc = _service(db, _matrix_search, movie_lib="/lib",
                       auto_rename_require_confirmation=False)
        svc._identify = lambda fn: {
            "tmdb_id": 603, "title": "The Matrix", "year": 1999,
            "media_type": "movie", "confidence": 95.0, "source": "ocr_credits",
            "resolution": "1080p", "season": None, "episode": None}
        jid = svc._process_file(None, "/x/cryptic.release.mkv")
        job = db.get_rename_job(jid)
        assert job["status"] == "needs_review"
        db.close()

    def test_deterministic_source_can_auto_apply(self, tmp_path):
        db = DatabaseManager()
        src = tmp_path / "The.Matrix.1999.1080p.mkv"
        src.write_text("x")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"),
                       auto_rename_require_confirmation=False,
                       auto_rename_move_method="copy")
        svc._identify = lambda fn: {
            "tmdb_id": 603, "title": "The Matrix", "year": 1999,
            "media_type": "movie", "confidence": 95.0, "source": "deterministic",
            "resolution": "1080p", "season": None, "episode": None}
        jid = svc._process_file(None, str(src))
        job = db.get_rename_job(jid)
        assert job["status"] in ("matched", "applied")
        db.close()


# ── Review fixes: imdb_id persistence + reidentify data-loss window ──

class TestReviewFixes:
    def test_imdb_id_persisted_on_match(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib="/lib")
        jid = svc._process_file(None, "/x/The.Matrix.1999.{imdb-tt0133093}.1080p.mkv")
        job = db.get_rename_job(jid)
        assert job["imdb_id"] == "tt0133093"
        db.close()

    def test_reidentify_keeps_job_on_processing_error(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib="/lib")
        jid = svc._process_file(None, "/x/The.Matrix.1999.1080p.mkv")
        assert jid
        def boom(*a, **k):
            raise RuntimeError("boom")
        svc._process_file = boom
        r = svc.reidentify(jid)
        assert r["ok"] is False
        assert db.get_rename_job(jid) is not None  # original preserved
        db.close()

    def test_reidentify_keeps_job_when_no_replacement(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search, movie_lib="/lib")
        jid = svc._process_file(None, "/x/The.Matrix.1999.1080p.mkv")
        svc._process_file = lambda *a, **k: None
        r = svc.reidentify(jid)
        assert r["ok"] is False and db.get_rename_job(jid) is not None
        db.close()


# ── Minor review fix: _translate_path requires a path boundary ───────

class TestTranslatePathBoundary:
    def test_sibling_dir_not_captured(self):
        db = DatabaseManager()
        svc = _service(db, _matrix_search,
                       auto_rename_path_mappings="F:\Downloads => /library/movies")
        assert svc._translate_path("F:/Downloads/Film") == "/library/movies/Film"
        assert svc._translate_path("F:/Downloads") == "/library/movies"
        # 'Downloads2' shares a prefix but is a different dir → unchanged
        assert svc._translate_path("F:/Downloads2/Film") == "F:/Downloads2/Film"
        db.close()


# ── Year-less retry keeps the year signal (no wrong-year remake) ─────

class TestYearlessRetryScoring:
    def test_remake_does_not_win_yearless_retry(self):
        db = DatabaseManager()
        # Year-filtered search misses (forces the year-less rung); the year-less
        # search returns BOTH a same-title 2019 remake and the correct 1974 film.
        def search(title, year, mt):
            if year is not None:
                return []
            return [
                {"id": 1, "title": "The Movie", "release_date": "2019-01-01"},  # remake
                {"id": 2, "title": "The Movie", "release_date": "1974-01-01"},  # correct
            ]
        svc = _service(db, search)
        m = svc._identify("The.Movie.1974.1080p.mkv")
        assert m is not None and m["year"] == 1974 and m["tmdb_id"] == 2
        db.close()

    def test_yearless_still_resolves_when_tmdb_year_missing(self):
        db = DatabaseManager()
        # Year filter misses; year-less returns a candidate with no release date.
        def search(title, year, mt):
            if year is not None:
                return []
            return [{"id": 7, "title": "Obscure Film", "release_date": ""}]
        svc = _service(db, search)
        m = svc._identify("Obscure.Film.1999.1080p.mkv")
        assert m is not None and m["tmdb_id"] == 7  # no year → no penalty, still resolves
        db.close()


# ── Feature: process-folder dry-run preview (no jobs created) ────────

class TestDryRunPreview:
    def test_preview_does_not_create_jobs(self, tmp_path):
        db = DatabaseManager()
        (tmp_path / "The.Matrix.1999.1080p.mkv").write_text("x")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        out = svc.process_folder(str(tmp_path), dry_run=True)
        assert out["dry_run"] is True and out["found"] == 1
        p = out["previews"][0]
        assert p["title"] == "The Matrix" and p["status"] == "matched"
        assert p["new_filename"] == "The Matrix (1999) [1080p].mkv"
        assert db.count_rename_jobs_by_status() == {}  # nothing persisted
        db.close()

    def test_preview_marks_already_tracked(self, tmp_path):
        db = DatabaseManager()
        (tmp_path / "The.Matrix.1999.1080p.mkv").write_text("x")
        svc = _service(db, _matrix_search, movie_lib=str(tmp_path / "lib"))
        svc.process_folder(str(tmp_path))            # create the job for real
        out = svc.process_folder(str(tmp_path), dry_run=True)
        assert out["previews"][0]["tracked"] is True
        db.close()
