"""Auto-rename orchestration.

Ties the pieces together: enumerate extracted video files, identify each
(ScanHound's ``parse_filename`` → TMDB search → confidence score, with an
optional Ollama fallback for low-confidence cases), record a ``rename_jobs``
row, and — per the gates — apply (rename + place into the library) or hold for
review. ``apply``/``undo``/``rematch`` drive the UI actions.

TMDB lookups go through an injectable ``tmdb_search`` callable so the service is
fully unit-testable without network.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from backend.filename_utils import parse_filename
from backend.rename import confidence as _confidence
from backend.rename import fileops as _fileops
from backend.rename import llm_identify as _llm
from backend.rename import naming as _naming

logger = logging.getLogger(__name__)

VIDEO_EXTS = _naming.VIDEO_EXTENSIONS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_sort_title(title: Optional[str]) -> Optional[str]:
    """Plex-style sort title: move a leading article to the end."""
    if not title:
        return None
    for art in ("The ", "A ", "An "):
        if title.startswith(art):
            return f"{title[len(art):]}, {art.strip()}"
    return title


class RenameService:
    def __init__(self, registry, tmdb_search: Optional[Callable] = None):
        self._reg = registry
        self._tmdb_search_override = tmdb_search
        self._client = None

    # ── helpers ───────────────────────────────────────────────────────

    @property
    def _cfg(self) -> dict:
        return self._reg.config or {}

    @property
    def _db(self):
        return self._reg.db

    def _threshold(self) -> int:
        try:
            return max(0, min(100, int(self._cfg.get("auto_rename_confidence_threshold", 70))))
        except (TypeError, ValueError):
            return 70

    def _template_for(self, media_type) -> Optional[str]:
        key = "auto_rename_template_tv" if media_type == "tv" else "auto_rename_template_movie"
        return self._cfg.get(key) or None

    def _tmdb_client(self):
        if self._client is None:
            key = self._cfg.get("tmdb_api_key")
            if not key:
                return None
            from backend.tmdb_client import TmdbClient
            self._client = TmdbClient(key)
        return self._client

    def _search_tmdb(self, title, year, media_type) -> list:
        if self._tmdb_search_override:
            return self._tmdb_search_override(title, year, media_type) or []
        client = self._tmdb_client()
        if not client:
            return []
        try:
            return client.search(title, media_type=media_type, year=year) or []
        except Exception:
            return []

    @staticmethod
    def _normalize_candidate(r: dict, media_type: str) -> Optional[dict]:
        if media_type == "tv":
            name = r.get("name") or r.get("original_name") or ""
            date = r.get("first_air_date") or ""
        else:
            name = r.get("title") or r.get("original_title") or ""
            date = r.get("release_date") or ""
        if not name:
            return None
        year = int(date[:4]) if date[:4].isdigit() else None
        return {"title": name, "year": year, "tmdb_id": r.get("id"), "media_type": media_type}

    def _tmdb_match(self, title, year, media_type) -> Optional[dict]:
        if not title:
            return None
        best = None
        for r in self._search_tmdb(title, year, media_type)[:8]:
            cand = self._normalize_candidate(r, media_type)
            if not cand:
                continue
            cand["confidence"] = _confidence.match_confidence(
                title, cand["title"], year, cand["year"])
            if best is None or cand["confidence"] > best["confidence"]:
                best = cand
        return best

    def _identify(self, filename: str) -> Optional[dict]:
        parsed = parse_filename(filename)
        title = parsed.get("title") or ""
        year = parsed.get("year")
        media_type = "tv" if parsed.get("is_tv") else "movie"

        result = self._tmdb_match(title, year, media_type)
        if result:
            result.update(source="deterministic", resolution=parsed.get("resolution"),
                          season=parsed.get("season"), episode=parsed.get("episode"),
                          episode_title=parsed.get("filename_episode_title"))

        # Optional Ollama fallback only when the deterministic match is weak.
        if (self._cfg.get("auto_rename_llm_enabled")
                and (not result or result["confidence"] < self._threshold())):
            base_url = self._cfg.get("ollama_base_url", "")
            model = self._cfg.get("ollama_model", "")
            if not model or not base_url:
                # Enabled but unconfigured — llm.identify() would silently no-op,
                # so the operator would never learn why no LLM assist happened.
                logger.warning(
                    "Ollama assist enabled but %s not set; skipping LLM fallback",
                    "model" if not model else "base URL")
            llm = _llm.identify(filename, base_url=base_url, model=model)
            if llm and llm.get("title"):
                mtype = llm.get("media_type") or media_type
                alt = self._tmdb_match(llm["title"], llm.get("year"), mtype)
                if alt and (not result or alt["confidence"] > result["confidence"]):
                    alt.update(source="llm", resolution=parsed.get("resolution"),
                               season=llm.get("season") or parsed.get("season"),
                               episode=llm.get("episode") or parsed.get("episode"),
                               episode_title=parsed.get("filename_episode_title"))
                    result = alt
        return result

    # ── entry point from the JD poll loop ─────────────────────────────

    def process_package(self, package_name: str, save_to: str) -> List[int]:
        """Identify + record (and maybe apply) renames for an extracted package.

        Returns the created job ids. Deduped by package name; no-op unless
        ``auto_rename_enabled``.
        """
        if not self._cfg.get("auto_rename_enabled"):
            return []
        db = self._db
        if db is None or not save_to or not os.path.isdir(save_to):
            return []
        if package_name and db.package_has_rename_job(package_name):
            return []
        return [jid for path in self._video_files(save_to)
                if (jid := self._process_file(package_name, path))]

    @staticmethod
    def _video_files(root: str) -> List[str]:
        out = []
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    out.append(os.path.join(dirpath, f))
        return sorted(out)

    def _process_file(self, package_name, path) -> Optional[int]:
        filename = os.path.basename(path)
        threshold = self._threshold()
        match = self._identify(filename)
        job = {"package_name": package_name, "original_path": path,
               "original_filename": filename, "status": "pending"}

        if not match:
            job.update(status="needs_review", warning_message="No confident match found")
            return self._create(job)

        fname, dest = _naming.build_target(
            {**match, "original_filename": filename},
            movie_root=self._cfg.get("auto_rename_movie_library", ""),
            tv_root=self._cfg.get("auto_rename_tv_library", ""),
            template=self._template_for(match.get("media_type")))
        conf = match.get("confidence") or 0.0
        job.update(
            media_type=match.get("media_type"), title=match.get("title"),
            year=match.get("year"), season=match.get("season"),
            episode=match.get("episode"), tmdb_id=match.get("tmdb_id"),
            imdb_id=match.get("imdb_id"), resolution=match.get("resolution"),
            match_confidence=conf, match_source=match.get("source"),
            new_filename=fname, destination_path=dest)

        if conf < threshold:
            job.update(status="needs_review",
                       warning_message=f"Low confidence ({conf:.0f} < {threshold})")
        else:
            job["status"] = "matched"

        job_id = self._create(job)
        if (job_id and job["status"] == "matched"
                and not self._cfg.get("auto_rename_require_confirmation", True)):
            self.apply(job_id)
        return job_id

    def _create(self, job) -> Optional[int]:
        jid = self._db.create_rename_job(job) if self._db else None
        self._broadcast(jid)
        return jid

    # ── UI-driven actions ─────────────────────────────────────────────

    def apply(self, job_id: int) -> dict:
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"ok": False, "error": "Job not found"}
        if job.get("status") == "applied":
            return {"ok": True}
        src = job.get("original_path")
        if not src or not os.path.isfile(src):
            db.update_rename_job(job_id, status="failed", error_message="Source file missing")
            self._broadcast(job_id)
            return {"ok": False, "error": "Source file missing"}
        dst = os.path.join(job.get("destination_path") or "",
                           job.get("new_filename") or os.path.basename(src))
        method = self._cfg.get("auto_rename_move_method", "hardlink")
        try:
            used = _fileops.place_file(src, dst, method)
        except Exception as e:
            db.update_rename_job(job_id, status="failed", error_message=str(e))
            self._broadcast(job_id)
            return {"ok": False, "error": str(e)}
        sort_title = (compute_sort_title(job.get("title"))
                      if self._cfg.get("auto_rename_plex_sort_titles") else None)
        try:
            db.update_rename_job(job_id, status="applied", move_method=used,
                                 processed_at=_now(), plex_sort_title=sort_title,
                                 error_message=None)
        except Exception as e:
            # The file is already placed but we couldn't record it. Leaving the
            # row as-is orphans the file (re-apply sees "source missing", undo
            # sees "not applied"). Reverse the placement so disk and DB stay
            # consistent, then surface the failure.
            try:
                _fileops.undo_place(src, dst, used)
            except Exception:
                logger.exception(
                    "rename apply: DB write failed AND rollback of %s -> %s "
                    "failed; file may be orphaned (job %s)", src, dst, job_id)
            try:
                db.update_rename_job(job_id, status="failed",
                                     error_message=f"apply bookkeeping failed: {e}")
            except Exception:
                pass
            self._broadcast(job_id)
            return {"ok": False, "error": str(e)}
        self._broadcast(job_id)
        return {"ok": True}

    def undo(self, job_id: int) -> dict:
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"ok": False, "error": "Job not found"}
        if job.get("status") != "applied":
            return {"ok": False, "error": "Job is not applied"}
        src = job.get("original_path")
        dst = os.path.join(job.get("destination_path") or "", job.get("new_filename") or "")
        try:
            _fileops.undo_place(src, dst, job.get("move_method") or "move")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        db.update_rename_job(job_id, status="reverted", reverted_at=_now())
        self._broadcast(job_id)
        return {"ok": True}

    def rematch(self, job_id: int, tmdb_id: int, media_type: Optional[str] = None) -> dict:
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"ok": False, "error": "Job not found"}
        mtype = media_type or job.get("media_type") or "movie"
        client = self._tmdb_client()
        details = None
        if client:
            try:
                details = client.details(int(tmdb_id), media_type=mtype)
            except Exception:
                details = None
        if not details:
            return {"ok": False, "error": "Could not fetch TMDB details"}
        title = details.get("title") or details.get("name") or job.get("title")
        date = details.get("release_date") or details.get("first_air_date") or ""
        year = int(date[:4]) if date[:4].isdigit() else job.get("year")
        meta = {**job, "media_type": mtype, "title": title, "year": year,
                "tmdb_id": int(tmdb_id)}
        fname, dest = _naming.build_target(
            meta, movie_root=self._cfg.get("auto_rename_movie_library", ""),
            tv_root=self._cfg.get("auto_rename_tv_library", ""),
            template=self._template_for(mtype))
        db.update_rename_job(job_id, title=title, year=year, tmdb_id=int(tmdb_id),
                             media_type=mtype, new_filename=fname, destination_path=dest,
                             match_confidence=100.0, match_source="manual",
                             status="matched", warning_message=None)
        self._broadcast(job_id)
        return {"ok": True}

    def _broadcast(self, job_id) -> None:
        if not job_id or self._db is None:
            return
        try:
            from backend.api.ws import ws_manager
            job = self._db.get_rename_job(job_id)
            if job:
                ws_manager.broadcast_sync({"type": "rename:job", "data": job})
        except Exception:
            pass
