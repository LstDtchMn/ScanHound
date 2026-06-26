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
import re as _re
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


def _try_episode_rescan(
    match: dict,
    client,
    file_min: float,
    season_cache: dict,
    tmdb_id: int,
    llm_cfg: dict,
) -> Optional[dict]:
    # Find a better episode assignment when runtime is suspicious.
    current_season = match.get("season", 1)
    current_ep = match.get("episode", 1)

    def _get_season(s: int) -> list:
        if s not in season_cache:
            data = client.season(tmdb_id, s)
            season_cache[s] = data or {}
        return (season_cache[s].get("episodes") or []) if season_cache.get(s) else []

    current_episodes = _get_season(current_season)
    current_ep_data = next(
        (e for e in current_episodes if e.get("episode_number") == current_ep), None)
    current_rt = current_ep_data.get("runtime") if current_ep_data else None
    current_score = (
        _confidence.runtime_confidence_delta(file_min, current_rt)
        if current_rt else -30.0
    )

    candidates: list[dict] = []

    for ep in current_episodes:
        ep_num = ep.get("episode_number")
        ep_rt = ep.get("runtime")
        if not ep_num or not ep_rt or ep_num == current_ep:
            continue
        if abs(ep_num - current_ep) > 3:
            continue
        score = _confidence.runtime_confidence_delta(file_min, ep_rt)
        if score - current_score >= 15.0:
            candidates.append({
                "episode": ep_num, "season": current_season,
                "title": ep.get("name", ""), "runtime": ep_rt,
                "score_delta": score,
            })

    for adj in (current_season - 1, current_season + 1):
        if adj < 1:
            continue
        adj_eps = _get_season(adj)
        adj_ep = next((e for e in adj_eps if e.get("episode_number") == current_ep), None)
        if adj_ep and adj_ep.get("runtime"):
            score = _confidence.runtime_confidence_delta(file_min, adj_ep["runtime"])
            if score - current_score >= 15.0:
                candidates.append({
                    "episode": current_ep, "season": adj,
                    "title": adj_ep.get("name", ""), "runtime": adj_ep["runtime"],
                    "score_delta": score,
                })

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score_delta"], reverse=True)

    best = candidates[0]
    if (len(candidates) >= 2
            and candidates[0]["score_delta"] - candidates[1]["score_delta"] < 10
            and llm_cfg.get("base_url") and llm_cfg.get("model")):
        pick = _llm.disambiguate_episode(
            match.get("original_filename", ""),
            candidates[:3],
            base_url=llm_cfg["base_url"],
            model=llm_cfg["model"],
        )
        if pick:
            matched = next(
                (c for c in candidates
                 if c["episode"] == pick["episode"] and c["season"] == pick["season"]),
                None)
            if matched:
                best = matched

    return {
        "type": "episode_correction",
        "original": {"season": current_season, "episode": current_ep},
        "proposed": {
            "season": best["season"],
            "episode": best["episode"],
            "title": best["title"],
            "runtime": best["runtime"],
        },
        "confidence_gain": round(best["score_delta"] - current_score, 1),
        "method": "ollama" if len(candidates) >= 2 else "runtime",
    }


def _detect_combined_episode(match: dict, file_min: float, episodes: list) -> Optional[dict]:
    # Detect if a file contains two consecutive episodes.
    ep_num = match.get("episode")
    if not ep_num or not file_min:
        return None
    ep_data = next((e for e in episodes if e.get("episode_number") == ep_num), None)
    tmdb_min = ep_data.get("runtime") if ep_data else None
    if not tmdb_min:
        return None

    ratio = file_min / tmdb_min
    if not 1.7 <= ratio <= 2.4:
        return None

    next_ep = next((e for e in episodes if e.get("episode_number") == ep_num + 1), None)
    if not next_ep or not next_ep.get("runtime"):
        return None

    combined = tmdb_min + next_ep["runtime"]
    pct_off = abs(file_min - combined) / combined
    if pct_off > 0.08:
        return None

    return {
        "episode_start": ep_num,
        "episode_end": ep_num + 1,
        "proposed_code": f"E{ep_num:02d}E{ep_num + 1:02d}",
        "runtime_match_pct": round(pct_off * 100, 1),
    }


def _detect_split_file(path: str, file_min: float, tmdb_min: float) -> Optional[dict]:
    # Detect if a file is one part of a split episode.
    if not file_min or not tmdb_min or file_min >= tmdb_min * 0.6:
        return None
    sibling = _find_split_sibling(path)
    if not sibling:
        return None
    part = 1 if path < sibling else 2
    return {
        "part": part,
        "sibling_path": sibling,
        "proposed_suffix": f"Part {part}",
    }


_SPLIT_SIBLING_EXTS = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".flv", ".webm"})
_SPLIT_PART_RE = _re.compile(
    r'[.\s\-_](?:Part|Pt)[\s.\-]?\d', _re.IGNORECASE)


def _find_split_sibling(path: str) -> Optional[str]:
    # Return a sibling video file in the same directory with the same SxxExx code.
    directory = os.path.dirname(path)
    basename = os.path.basename(path)
    se_match = _re.search(r'S(\d{1,2})E(\d{1,3})', basename, _re.IGNORECASE)
    if not se_match:
        return None
    se_code = se_match.group(0).upper()
    try:
        candidates = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if (os.path.isfile(os.path.join(directory, f))
                and os.path.splitext(f)[1].lower() in _SPLIT_SIBLING_EXTS
                and se_code in f.upper()
                and os.path.join(directory, f) != path)
        ]
    except OSError:
        return None
    for c in candidates:
        if _SPLIT_PART_RE.search(os.path.basename(c)):
            return c
    return candidates[0] if len(candidates) == 1 else None


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
            # Pass parsed year + top TMDB candidates so the model can
            # disambiguate remakes and generic titles rather than guessing blind.
            raw = self._search_tmdb(title, year, media_type)[:5]
            llm_candidates = []
            for r in raw:
                c = self._normalize_candidate(r, media_type)
                if c:
                    c["confidence"] = _confidence.match_confidence(
                        title, c["title"], year, c["year"])
                    llm_candidates.append(c)
            llm = _llm.identify(filename, base_url=base_url, model=model,
                                parsed_year=year,
                                candidates=llm_candidates or None)
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

        # Vision fallback: if still low-confidence, extract video frames and
        # ask the vision model to read title cards / credits.
        if (self._cfg.get("auto_rename_llm_enabled")
                and (not match or match.get("confidence", 0) < threshold)):
            base_url = self._cfg.get("ollama_base_url", "")
            model = self._cfg.get("ollama_model", "")
            if base_url and model:
                vision = _llm.identify_from_frames(
                    path, base_url=base_url, model=model)
                if vision and vision.get("title"):
                    mtype = vision.get("media_type") or (
                        "tv" if vision.get("season") else "movie")
                    alt = self._tmdb_match(
                        vision["title"], vision.get("year"), mtype)
                    if alt and (not match or alt["confidence"] > match.get("confidence", 0)):
                        parsed = parse_filename(filename)
                        alt.update(
                            source="llm_vision",
                            resolution=parsed.get("resolution"),
                            season=vision.get("season") or parsed.get("season"),
                            episode=vision.get("episode") or parsed.get("episode"),
                            episode_title=parsed.get("filename_episode_title"))
                        match = alt

        # Runtime + episode-validity confirmation.
        # Skipped for season packs (season set, episode=None) — pack duration
        # != single-episode runtime.
        if match and match.get("tmdb_id"):
            mtype = match.get("media_type", "movie")
            is_pack = match.get("season") is not None and match.get("episode") is None
            if not is_pack:
                client = self._tmdb_client()
                file_min = _llm.video_duration_minutes(path)
                tmdb_min = None
                try:
                    if client:
                        det = client.details(int(match["tmdb_id"]), media_type=mtype)
                        if det:
                            if mtype == "tv":
                                season_num = match.get("season")
                                ep_num = match.get("episode")
                                if season_num and ep_num:
                                    # Fetch episode-specific runtime + validity
                                    season_data = client.season(
                                        int(match["tmdb_id"]), season_num)
                                    if season_data:
                                        episodes = season_data.get("episodes") or []
                                        ep = next(
                                            (e for e in episodes
                                             if e.get("episode_number") == ep_num),
                                            None)
                                        if ep:
                                            tmdb_min = ep.get("runtime")
                                        # Episode validity: penalise if episode
                                        # number exceeds the season's length.
                                        max_ep = max(
                                            (e.get("episode_number", 0) for e in episodes),
                                            default=0)
                                        if max_ep and ep_num > max_ep:
                                            match["confidence"] = round(
                                                max(0.0, match["confidence"] - 20), 1)
                                            match["runtime_warning"] = (
                                                f"E{ep_num:02d} exceeds season "
                                                f"length ({max_ep} episodes)")
                                if not tmdb_min:
                                    run_list = det.get("episode_run_time") or []
                                    tmdb_min = run_list[0] if run_list else None
                            else:
                                tmdb_min = det.get("runtime")
                except Exception:
                    pass

                resolution = match.get("resolution")
                if tmdb_min:
                    if file_min:
                        delta = _confidence.runtime_confidence_delta(file_min, tmdb_min)
                    else:
                        # ffprobe unavailable — fall back to file-size plausibility
                        try:
                            file_bytes = os.path.getsize(path)
                        except OSError:
                            file_bytes = 0
                        delta = _confidence.filesize_plausibility_delta(
                            file_bytes, tmdb_min, resolution)
                    if delta != 0.0:
                        match["confidence"] = round(
                            max(0.0, min(100.0, match["confidence"] + delta)), 1)
                    if delta < -10 and not match.get("runtime_warning"):
                        src = f"file {file_min}min" if file_min else "size check"
                        match["runtime_warning"] = (
                            f"Runtime mismatch: {src} vs TMDB {tmdb_min}min")
                        logger.debug(
                            "Runtime penalised '%s' by %.0f (%s, TMDB %dmin)",
                            match.get("title"), delta, src, tmdb_min)

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

        runtime_warn = match.get("runtime_warning", "")
        if conf < threshold:
            msg = f"Low confidence ({conf:.0f} < {threshold})"
            if runtime_warn:
                msg += f"; {runtime_warn}"
            job.update(status="needs_review", warning_message=msg)
        else:
            job["status"] = "matched"
            if runtime_warn:
                job["warning_message"] = runtime_warn

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
