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
import threading
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from backend.filename_utils import parse_filename
from backend.rename import confidence as _confidence
from backend.rename import dv_detect as _dv
from backend.rename import fileops as _fileops
from backend.rename import llm_identify as _llm
from backend.rename import naming as _naming

logger = logging.getLogger(__name__)

# Dedicated rename logger, pinned at INFO so each file's parse/query/decision/
# move is visible in production logs WITHOUT enabling app-wide debug. It still
# propagates to the root handlers; pinning only its own level keeps the trace on
# regardless of debug_mode.
rlog = logging.getLogger("scanhound.rename")
rlog.setLevel(logging.INFO)

VIDEO_EXTS = _naming.VIDEO_EXTENSIONS

# Active candidates: a file WILL be placed at this destination if applied.
_ACTIVE_STATUSES = frozenset({"pending", "matched", "needs_review"})
# Statuses that occupy/claim a destination on disk. ``applied`` is included
# because the file is already there — a new candidate landing on the same path
# is a real conflict (its apply would collide). ``failed``/``reverted`` released
# the slot and are excluded.
_CLAIMING_STATUSES = _ACTIVE_STATUSES | frozenset({"applied"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dest_key(job: dict) -> Optional[str]:
    """Normalized full destination (dir + filename) for a job, or None if it has
    no target yet. Case-folded with normalized separators so the comparison is
    robust across the NTFS-backed (case-insensitive) library mounts."""
    dest = (job.get("destination_path") or "").rstrip("/\\")
    name = job.get("new_filename") or ""
    if not name and not dest:
        return None
    full = f"{dest}/{name}" if dest else name
    return full.replace("\\", "/").casefold()


# Dolby Vision tag, excluding the camera/rip formats "DV.Cam" / "dv-rip" whose
# dot/dash makes a naive \bdv\b match. Negative lookahead drops cam/rip suffixes.
_DV_RE = _re.compile(r"\b(?:dovi|dolby[.\s-]?vision)\b|\bdv\b(?![.\s_-]?(?:cam|rip))",
                     _re.IGNORECASE)


def _quality_score(job: dict) -> tuple:
    """Rank a release by desirability from its filename + parsed resolution, so a
    duplicate-target group can recommend which copy to keep. Higher is better.

    Returns a comparable tuple (so ties fall through to the next signal):
        (resolution_rank, dolby_vision, hdr, source_rank, audio_rank, edition)
    Pure string heuristics over the original filename — no I/O."""
    name = str(job.get("original_filename") or "").lower()

    res = str(job.get("resolution") or "").lower()
    if not res:  # fall back to filename if the column wasn't set
        for r in ("2160p", "1080p", "720p", "480p"):
            if r in name:
                res = r
                break
        if "2160" not in res and ("4k" in name or "uhd" in name):
            res = "2160p"
    res_rank = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}.get(res, 0)

    # Dolby Vision is the headline upgrade (esp. for this user). Match DoVi /
    # "dolby vision" / a standalone DV tag — but NOT the camera/rip formats
    # "DV.Cam" / "dv-rip" (the dot/dash is a word boundary, so a naive \bdv\b
    # matches those false positives).
    dv = 1 if _re.search(_DV_RE, name) else 0
    hdr = 1 if _re.search(r"\bhdr(10)?(\+|plus)?\b|\bhlg\b", name) else 0

    if _re.search(r"\bremux\b", name):
        source = 4
    elif _re.search(r"\b(blu[.\s-]?ray|bdrip|bd(25|50|66|100))\b", name):
        source = 3
    elif _re.search(r"\bweb[.\s-]?dl\b", name):
        source = 2
    elif _re.search(r"\bweb[.\s-]?rip\b|\bhdtv\b", name):
        source = 1
    else:
        source = 0

    if _re.search(r"\b(truehd|atmos)\b", name):
        audio = 3
    elif _re.search(r"\bdts[.\s-]?hd\b|\bdts[.\s-]?x\b", name):
        audio = 2
    elif _re.search(r"\b(ddp|eac3|dd\+)\b", name):
        audio = 1
    else:
        audio = 0

    edition = 1 if _re.search(r"\b(imax|extended|uncut|remastered|criterion)\b", name) else 0

    return (res_rank, dv, hdr, source, audio, edition)


def _quality_reason(job: dict) -> str:
    """Short human reason a release was picked as the keeper (its standout tags)."""
    name = (job.get("original_filename") or "").lower()
    bits = []
    if job.get("resolution"):
        bits.append(job["resolution"])
    elif "2160p" in name or "4k" in name or "uhd" in name:
        bits.append("2160p")
    if _re.search(_DV_RE, name):
        bits.append("Dolby Vision")
    if _re.search(r"\bhdr(10)?(\+|plus)?\b", name):
        bits.append("HDR")
    if _re.search(r"\bremux\b", name):
        bits.append("Remux")
    elif _re.search(r"\bblu[.\s-]?ray\b", name):
        bits.append("BluRay")
    if _re.search(r"\b(truehd|atmos)\b", name):
        bits.append("Atmos/TrueHD")
    return " · ".join(bits)


def recommend_keep(group) -> Optional[int]:
    """Given the jobs of one duplicate-target group, return the id of the release
    to keep (best quality), or None if it's a tie at the top or input is empty."""
    ranked = [(j, _quality_score(j)) for j in group if j.get("id") is not None]
    if len(ranked) < 2:
        return ranked[0][0]["id"] if ranked else None
    ranked.sort(key=lambda t: t[1], reverse=True)
    if ranked[0][1] == ranked[1][1]:
        return None  # genuine tie — no clear winner, let the human choose
    return ranked[0][0]["id"]


def destination_conflict_ids(jobs) -> set:
    """Return the ids of ACTIVE jobs whose destination is also claimed by another
    job (active or already applied) — i.e. a duplicate-target conflict such as two
    releases of one movie, or a new grab landing on a file already in the library.

    A settled ``applied`` job counts toward a destination's claim (so its active
    rival is flagged) but is never itself flagged — it's done. Pure and
    DB-free, so it drives the jobs-list 'duplicate' badge and unit-tests cleanly."""
    by_key: dict = {}
    for j in jobs:
        if j.get("status") not in _CLAIMING_STATUSES:
            continue
        k = _dest_key(j)
        if not k:
            continue
        by_key.setdefault(k, []).append(j)
    out: set = set()
    for group in by_key.values():
        if len(group) < 2:
            continue
        for j in group:
            if j.get("status") in _ACTIVE_STATUSES and j.get("id") is not None:
                out.add(j.get("id"))
    return out


def conflict_annotations(jobs) -> dict:
    """Map job-id -> {destination_conflict, keep_recommended, keep_reason} for the
    jobs list. For each duplicate-target group with an active conflict, flags all
    active members and marks the single best-quality active release as the
    recommended keeper (with a short reason). Pure, DB-free."""
    by_key: dict = {}
    for j in jobs:
        if j.get("status") not in _CLAIMING_STATUSES:
            continue
        k = _dest_key(j)
        if not k:
            continue
        by_key.setdefault(k, []).append(j)
    out: dict = {}
    for group in by_key.values():
        active = [j for j in group
                  if j.get("status") in _ACTIVE_STATUSES and j.get("id") is not None]
        if len(group) < 2 or not active:
            continue
        # Recommend over the WHOLE group (incl. applied): if an already-applied
        # copy is the best, keep_id is its id — which isn't in `active`, so no
        # active rival is wrongly flagged "keep" over the better library copy.
        keep_id = recommend_keep(group)
        for j in active:
            ann = out.setdefault(j["id"], {"destination_conflict": True,
                                           "keep_recommended": False,
                                           "keep_reason": None})
            if keep_id is not None and j["id"] == keep_id:
                ann["keep_recommended"] = True
                ann["keep_reason"] = _quality_reason(j) or None
    return out


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
    _used_ollama = False
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
                _used_ollama = True

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
        "method": "ollama" if _used_ollama else "runtime",
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
        self._client_lock = threading.Lock()
        # Serializes bulk background ops (reidentify_all / process_folder) so two
        # concurrent runs can't race into duplicate jobs for the same path.
        self._bulk_lock = threading.Lock()
        # Paths currently being processed, with a lock, so the JD-webhook
        # process_package and a manual process_folder can't both pass the
        # check-then-create gate for the same file and double-create a job.
        self._inflight: set = set()
        self._inflight_lock = threading.Lock()
        # Per-file TMDB search memo lives here (thread-local so concurrent
        # _process_file calls don't share or race on it).
        self._tl = threading.local()

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

    def _movie_root(self, resolution: Optional[str] = None) -> str:
        if resolution == "2160p":
            lib_4k = self._cfg.get("auto_rename_movie_library_4k", "")
            if lib_4k:
                return lib_4k
        return self._cfg.get("auto_rename_movie_library", "")

    def _template_for(self, media_type) -> Optional[str]:
        key = "auto_rename_template_tv" if media_type == "tv" else "auto_rename_template_movie"
        return self._cfg.get(key) or None

    def _lib_set(self, media_type: Optional[str], resolution: Optional[str] = None) -> tuple:
        """Return (is_configured, library_label) for the given media type."""
        if media_type == "tv":
            return bool(self._cfg.get("auto_rename_tv_library")), "TV"
        return bool(self._movie_root(resolution)), "Movie"

    def _tmdb_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:   # double-checked under the lock
                    key = self._cfg.get("tmdb_api_key")
                    if not key:
                        return None
                    from backend.tmdb_client import TmdbClient
                    self._client = TmdbClient(key)
        return self._client

    def _search_tmdb(self, title, year, media_type) -> list:
        if self._tmdb_search_override:
            return self._tmdb_search_override(title, year, media_type) or []
        # Request-scoped memo (set per _process_file, thread-local) dedups the
        # identical searches the retry ladder + candidate build + LLM rung would
        # otherwise repeat for one file — no extra TMDB calls or rate-limit burn.
        memo = getattr(self._tl, "search_memo", None)
        key = (title or "", year, media_type)
        if memo is not None and key in memo:
            return memo[key]
        client = self._tmdb_client()
        if not client:
            return []
        try:
            res = client.search(title, media_type=media_type, year=year) or []
        except Exception:
            res = []
        if memo is not None:
            memo[key] = res
        return res

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
        return {"title": name, "year": year, "tmdb_id": r.get("id"),
                "media_type": media_type, "poster_path": r.get("poster_path")}

    def _tmdb_match(self, title, year, media_type, *, score_year=None) -> Optional[dict]:
        """Search by ``title``/``year`` and rank by confidence. ``score_year``
        (when given) is the year used for *scoring* even if the *search* dropped
        the year filter — so a year-less retry still keeps the year signal and a
        wrong-year remake can't outrank the correct match on title alone. A
        missing/conflicting candidate year only ever penalises (never required),
        so legitimately year-less releases still resolve."""
        if not title:
            return None
        sy = score_year if score_year is not None else year
        best = None
        for r in self._search_tmdb(title, year, media_type)[:8]:
            cand = self._normalize_candidate(r, media_type)
            if not cand:
                continue
            cand["confidence"] = _confidence.match_confidence(
                title, cand["title"], sy, cand["year"])
            if best is None or cand["confidence"] > best["confidence"]:
                best = cand
        return best

    def _search_candidates(self, title, year, media_type, limit=6) -> List[dict]:
        """Top normalized TMDB candidates for a parsed title — fed to the
        vision/subtitle fallbacks so they pick from real options instead of
        hallucinating an open-ended guess."""
        out = []
        for r in self._search_tmdb(title or "", year, media_type)[:limit]:
            c = self._normalize_candidate(r, media_type)
            if c:
                out.append(c)
        return out

    def _enrich_with_credits(self, candidates, limit=5) -> List[dict]:
        """Attach `cast` (top billed names) and `director` to candidates so the
        OCR rung can deterministically match people printed in the credits when
        the title itself never appears on-screen. Best-effort and bounded; a
        candidate is left unenriched on any error. Skipped in tests."""
        if self._tmdb_search_override:
            return candidates
        client = self._tmdb_client()
        if not client:
            return candidates
        for c in candidates[:limit]:
            if "cast" in c or not c.get("tmdb_id"):
                continue
            try:
                data = client.credits(int(c["tmdb_id"]), c.get("media_type", "movie"))
            except Exception:
                data = None
            if not data:
                continue
            # Top-billed leads only — they're what cast cards show; lower-billed
            # names raise the odds of a coincidental surname collision.
            c["cast"] = [p.get("name") for p in (data.get("cast") or [])[:6]
                         if p.get("name")]
            c["director"] = next(
                (p.get("name") for p in (data.get("crew") or [])
                 if p.get("job") == "Director" and p.get("name")), None)
        return candidates

    def _tmdb_match_multi(self, title, year) -> Optional[dict]:
        """Cross-type (movie+tv) search fallback — for content misfiled as the
        wrong type. Skipped in tests (which inject a typed search override)."""
        if not title or self._tmdb_search_override:
            return None
        client = self._tmdb_client()
        if not client:
            return None
        try:
            results = client.search_multi(title) or []
        except Exception:
            return None
        best = None
        for r in results[:8]:
            mt = r.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            cand = self._normalize_candidate(r, mt)
            if not cand:
                continue
            cand["confidence"] = _confidence.match_confidence(
                title, cand["title"], year, cand["year"])
            if best is None or cand["confidence"] > best["confidence"]:
                best = cand
        return best

    def _tmdb_match_imdb(self, imdb_id, media_type) -> Optional[dict]:
        """Resolve an IMDB id embedded in the filename to a TMDB title via
        /find. This is an exact, fuzzy-free lookup, so a hit is the highest-
        confidence match possible. Skipped in tests (typed search override)."""
        if not imdb_id or self._tmdb_search_override:
            return None
        client = self._tmdb_client()
        if not client:
            return None
        try:
            data = client.find(imdb_id, source="imdb_id")
        except Exception:
            return None
        if not data:
            return None
        # Honour the type the filename hints at, then fall back to the other.
        order = (["tv_results", "movie_results"] if media_type == "tv"
                 else ["movie_results", "tv_results"])
        for key in order:
            results = data.get(key) or []
            if results:
                mt = "tv" if key == "tv_results" else "movie"
                cand = self._normalize_candidate(results[0], mt)
                if cand:
                    cand["confidence"] = 100.0  # exact external-id resolve
                    cand["media_type"] = mt
                    cand["imdb_id"] = imdb_id   # persist the exact id we resolved with
                    return cand
        return None

    def _identify(self, filename: str) -> Optional[dict]:
        parsed = parse_filename(filename)
        title = parsed.get("title") or ""
        aka = parsed.get("aka")
        year = parsed.get("year")
        media_type = "tv" if parsed.get("is_tv") else "movie"
        threshold = self._threshold()
        base = os.path.basename(filename)
        rlog.info("parse  | %s | title=%r year=%s aka=%r type=%s res=%s",
                  base, title, year, aka, media_type, parsed.get("resolution"))

        def _decorate(m: dict, source: str, llm: Optional[dict] = None) -> dict:
            # Drop TV-parsed season/episode when the resolved match is a movie
            # (e.g. a TV-styled name that the cross-type search flipped to a
            # film) — otherwise the persisted job carries spurious S/E for a movie.
            is_movie = m.get("media_type") == "movie"
            m.update(source=source, resolution=parsed.get("resolution"),
                     season=None if is_movie else ((llm or {}).get("season") or parsed.get("season")),
                     episode=None if is_movie else ((llm or {}).get("episode") or parsed.get("episode")),
                     episode_end=None if is_movie else parsed.get("episode_end"),
                     part=parsed.get("part"),
                     episode_title=None if is_movie else parsed.get("filename_episode_title"))
            # Carry the filename's tt-id through so job.imdb_id persists the most
            # authoritative identifier the file had (unless the match already set
            # one, e.g. the exact /find resolve).
            m.setdefault("imdb_id", parsed.get("imdb_id"))
            return m

        # ── Exact IMDB-id resolve (highest priority): when the filename carries
        # a tt-id, /find resolves it directly — no fuzzy matching needed.
        imdb_id = parsed.get("imdb_id")
        if imdb_id:
            m = self._tmdb_match_imdb(imdb_id, media_type)
            if m:
                _decorate(m, "imdb_id")
                rlog.info("decide | %s | -> %r (%s) via imdb_id %s [exact]",
                          base, m.get("title"), m.get("year"), imdb_id)
                return m

        # ── Tiered deterministic retry: principled query variations, best-first.
        # Each pass only *accepts* (stops) on a candidate that clears the
        # threshold; a weaker best is retained as a needs-review fallback.
        # Bounded and deduped, so recall improves without hurting precision.
        attempts: List[tuple] = []

        def _add(t, y):
            t = (t or "").strip()
            if t and (t, y) not in attempts:
                attempts.append((t, y))

        _add(title, year)
        if aka:
            _add(aka, year)                 # alternate / English title
        _add(title, None)                   # drop the year (TMDB's can be wrong/missing)
        if aka:
            _add(aka, None)
        subless = _re.split(r'\s*[:\-]\s+', title, 1)[0] if title else ""
        if subless and subless != title:    # strip a trailing subtitle
            _add(subless, year)
            _add(subless, None)

        result = None
        for t, y in attempts:
            # Always score against the parsed year (even when the search dropped
            # it) so a year-less retry can't let a wrong-year remake win on title
            # similarity alone.
            m = self._tmdb_match(t, y, media_type, score_year=year)
            if not m:
                rlog.info("query  | %s | %r y=%s -> 0 candidates", base, t, y)
                continue
            rlog.info("query  | %s | %r y=%s -> best %r (%s) conf=%.0f",
                      base, t, y, m.get("title"), m.get("year"), m.get("confidence", 0))
            m.setdefault("source", "deterministic")
            if result is None or m["confidence"] > result["confidence"]:
                result = m
            if m["confidence"] >= threshold:
                break

        # ── Cross-type multi-search fallback when nothing is strong yet.
        if result is None or result["confidence"] < threshold:
            for t in ([title, aka] if aka else [title]):
                m = self._tmdb_match_multi(t, year)
                if m:
                    m.setdefault("source", "tmdb_multi")
                    if result is None or m["confidence"] > result["confidence"]:
                        result = m
                        media_type = m.get("media_type", media_type)
                if result and result["confidence"] >= threshold:
                    break

        if result:
            _decorate(result, result.get("source", "deterministic"))

        # ── Ollama fallback only when still weak AND properly configured.
        if (self._cfg.get("auto_rename_llm_enabled")
                and (not result or result["confidence"] < threshold)):
            base_url = self._cfg.get("ollama_base_url", "")
            model = self._cfg.get("ollama_model", "")
            if not model or not base_url:
                # Enabled but unconfigured: warn and SKIP (don't fall through to
                # an LLM call that would silently no-op with an empty model).
                logger.warning(
                    "Ollama assist enabled but %s not set; skipping LLM fallback",
                    "model" if not model else "base URL")
            else:
                # Pass parsed year + top TMDB candidates so the model can
                # disambiguate remakes and generic titles, not guess blind.
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
                        result = _decorate(alt, "llm", llm)
        if result:
            rlog.info("decide | %s | -> %r (%s) conf=%.0f via %s (threshold %d)",
                      base, result.get("title"), result.get("year"),
                      result.get("confidence", 0), result.get("source"), threshold)
        else:
            rlog.info("decide | %s | no candidate found", base)
        return result

    # ── entry point from the JD poll loop ─────────────────────────────

    def _translate_path(self, path: str) -> str:
        """Map a host (JDownloader/Windows) path into a container path using the
        configured ``auto_rename_path_mappings`` (one ``host => container`` per
        line). JDownloader runs on the host and reports e.g. ``F:\\Downloads\\X``;
        the container sees that bind-mounted at ``/library/movies/X``. Longest
        host prefix wins. Returns the path unchanged if nothing matches."""
        if not path:
            return path
        raw = self._cfg.get("auto_rename_path_mappings") or ""
        norm = path.replace("\\", "/")
        best = None  # (host_prefix_len, translated)
        for line in str(raw).splitlines():
            if "=>" not in line:
                continue
            host, container = (p.strip() for p in line.split("=>", 1))
            if not host or not container:
                continue
            hp = host.replace("\\", "/").rstrip("/")
            # Require a path boundary (exact match or next char is '/') so a
            # mapping for 'F:/Downloads' doesn't also capture 'F:/Downloads2/…'.
            nl, hl = norm.lower(), hp.lower()
            if hp and (nl == hl or nl.startswith(hl + "/")):
                rest = norm[len(hp):].lstrip("/")
                translated = container.rstrip("/") + ("/" + rest if rest else "")
                if best is None or len(hp) > best[0]:
                    best = (len(hp), translated)
        return best[1] if best else path

    def process_package(self, package_name: str, save_to: str) -> List[int]:
        """Identify + record (and maybe apply) renames for an extracted package.

        Returns the created job ids. Deduped per-file (like process_folder) so
        an interrupted batch resumes correctly after a restart and two concurrent
        calls for the same package can't race through the check and double-create
        jobs. No-op unless ``auto_rename_enabled``.
        """
        if not self._cfg.get("auto_rename_enabled"):
            return []
        db = self._db
        # JDownloader hands us its host (Windows) save path; map it into the
        # container's mounted view before touching the filesystem.
        save_to = self._translate_path(save_to)
        if db is None or not save_to or not os.path.isdir(save_to):
            return []
        created = []
        for path in self._video_files(save_to):
            if not self._claim_path(path):
                continue
            try:
                jid = self._process_file(package_name, path)
            finally:
                self._release_path(path)
            if jid:
                created.append(jid)
        return created

    def _claim_path(self, path) -> bool:
        """Atomically reserve ``path`` for processing. Returns False if it already
        has a rename job (DB) or is in-flight in another thread right now — which
        closes the check-then-create race between a concurrent process_package
        and process_folder touching overlapping paths (both run in-process)."""
        with self._inflight_lock:
            if path in self._inflight:
                return False
            if self._db and self._db.path_has_rename_job(path):
                return False
            self._inflight.add(path)
            return True

    def _release_path(self, path):
        with self._inflight_lock:
            self._inflight.discard(path)

    def process_folder(self, folder: str, dry_run: bool = False) -> dict:
        """Manually scan a folder for video files and create rename jobs for any
        not already tracked — for processing an existing download backlog (no JD
        package). Host paths (e.g. ``F:\\Downloads``) are translated to the
        container's mounted view. Slow (a TMDB lookup per file), so callers
        should run this off the request thread. Returns a summary dict.

        With ``dry_run`` it identifies + proposes target names WITHOUT creating
        any jobs or moving files — a preview of what a real run would do."""
        db = self._db
        if db is None:
            return {"error": "Database unavailable", "found": 0, "created": 0, "skipped": 0}
        resolved = self._translate_path(folder)
        if not resolved or not os.path.isdir(resolved):
            return {"error": f"Folder not found in container: {resolved or folder}",
                    "found": 0, "created": 0, "skipped": 0}
        if dry_run:
            return self._preview_folder(resolved)
        # Single-flight with reidentify_all: the path-dedup (check-then-create)
        # isn't atomic, so two concurrent bulk runs could both pass the check and
        # create duplicate jobs for the same file.
        if not self._bulk_lock.acquire(blocking=False):
            return {"error": "Another bulk rename operation is already running",
                    "found": 0, "created": 0, "skipped": 0, "busy": True}
        try:
            files = self._video_files(resolved)
            created, skipped = [], 0
            for path in files:
                if not self._claim_path(path):
                    skipped += 1
                    continue
                try:
                    jid = self._process_file(None, path)
                except Exception:
                    logger.exception("process_folder: failed on %s", path)
                    jid = None
                finally:
                    self._release_path(path)
                if jid:
                    created.append(jid)
            logger.info("process_folder %s: %d file(s), %d new job(s), %d already tracked",
                        resolved, len(files), len(created), skipped)
            return {"folder": resolved, "found": len(files),
                    "created": len(created), "skipped": skipped, "ids": created}
        finally:
            self._bulk_lock.release()

    def _preview_folder(self, resolved: str) -> dict:
        """Identify each file and propose a target WITHOUT persisting — the
        dry-run preview. Uses the deterministic matcher only (no minutes-long
        file-reading fallbacks); the note flags that a real run may additionally
        use them on weak matches. Read-only, so no bulk lock needed."""
        db = self._db
        threshold = self._threshold()
        files = self._video_files(resolved)
        previews = []
        for path in files:
            filename = os.path.basename(path)
            tracked = bool(db and db.path_has_rename_job(path))
            try:
                match = self._identify(filename)
            except Exception:
                logger.exception("preview: identify failed for %s", filename)
                match = None
            conf = round(match.get("confidence", 0), 1) if match else 0.0
            entry = {"path": path, "filename": filename, "tracked": tracked,
                     "title": match.get("title") if match else None,
                     "year": match.get("year") if match else None,
                     "confidence": conf, "new_filename": None,
                     "status": "needs_review"}
            if match and match.get("tmdb_id"):
                try:
                    fname, _dest = _naming.build_target(
                        {**match, "original_filename": filename},
                        movie_root=self._movie_root(match.get("resolution")),
                        tv_root=self._cfg.get("auto_rename_tv_library", ""),
                        template=self._template_for(match.get("media_type")))
                    entry["new_filename"] = fname
                except Exception:
                    logger.exception("preview: build_target failed for %s", filename)
                entry["status"] = "matched" if conf >= threshold else "needs_review"
            previews.append(entry)
        matched = sum(1 for p in previews if p["status"] == "matched" and not p["tracked"])
        logger.info("preview %s: %d file(s), %d would match", resolved, len(files), matched)
        return {"folder": resolved, "found": len(files), "dry_run": True,
                "would_match": matched, "previews": previews,
                "note": "Deterministic match only; a real run may also use the "
                        "subtitle/OCR/vision fallbacks on weak matches."}

    def scan_folder_dv(self, folder: str, force: bool = False,
                       progress_cb=None) -> dict:
        """Walk a folder and record each video's Dolby Vision enhancement-layer
        type (fel/mel/profile5/...) in the dv_scan inventory via dovi_tool.

        Detection-only — it does NOT label Plex or tag files (those steps are
        separate). Skips files whose (mtime, size) signature is unchanged since
        the last scan unless ``force``. Manual + single-flighted (shares the bulk
        lock); every file is fail-safe, so one bad file never aborts the sweep.
        ``progress_cb(done, total, path, layer)`` streams progress (layer is None
        for a skipped file). Returns a summary dict."""
        db = self._db
        if db is None:
            return {"error": "Database unavailable", "found": 0, "scanned": 0, "skipped": 0}
        resolved = self._translate_path(folder)
        if not resolved or not os.path.isdir(resolved):
            return {"error": f"Folder not found in container: {resolved or folder}",
                    "found": 0, "scanned": 0, "skipped": 0}
        if not _dv.available():
            return {"error": "dovi_tool is not installed in this build",
                    "found": 0, "scanned": 0, "skipped": 0}
        if not self._bulk_lock.acquire(blocking=False):
            return {"error": "Another bulk rename operation is already running",
                    "found": 0, "scanned": 0, "skipped": 0, "busy": True}
        try:
            files = self._video_files(resolved)
            total = len(files)
            scanned, skipped = 0, 0
            by_layer: dict = {}
            for i, path in enumerate(files):
                # Skip-check is itself fail-safe: a stat error just means "scan it".
                try:
                    st = os.stat(path)
                    if not force and db.dv_scan_is_current(path, st.st_mtime, st.st_size):
                        skipped += 1
                        if progress_cb:
                            progress_cb(i + 1, total, path, None)
                        continue
                except OSError:
                    st = None
                # Detect + record. Any failure is recorded as 'unknown' (with a
                # null signature so a later run retries it) rather than dropped —
                # so found == scanned + skipped always holds and the file stays
                # visible in the inventory and progress stream.
                layer = _dv.LAYER_UNKNOWN
                try:
                    res = _dv.detect_layer(path)
                    layer = res.get("layer", _dv.LAYER_UNKNOWN)
                    title = parse_filename(os.path.basename(path)).get("title") or None
                    db.upsert_dv_scan(
                        path, layer, title=title,
                        sig_mtime=(st.st_mtime if st else None),
                        sig_size=(st.st_size if st else None), source="scan")
                except Exception:
                    logger.exception("dv scan failed on %s", path)
                    layer = _dv.LAYER_UNKNOWN
                    try:
                        db.upsert_dv_scan(path, layer, sig_mtime=None,
                                          sig_size=None, source="scan")
                    except Exception:
                        logger.exception("dv scan: could not record failure for %s", path)
                by_layer[layer] = by_layer.get(layer, 0) + 1
                scanned += 1
                rlog.info("dvscan | %s -> %s", os.path.basename(path), layer)
                if progress_cb:
                    progress_cb(i + 1, total, path, layer)
            logger.info("scan_folder_dv %s: %d file(s), %d scanned, %d skipped, %r",
                        resolved, total, scanned, skipped, by_layer)
            return {"folder": resolved, "found": total, "scanned": scanned,
                    "skipped": skipped, "by_layer": by_layer}
        finally:
            self._bulk_lock.release()

    def reidentify(self, job_id: int) -> dict:
        """Re-run identification for an existing (not-yet-applied) job — re-matches
        the same source file with the current matcher and replaces the job. Lets
        you re-try a 'No confident match' after matching improves, without first
        removing the stale job."""
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"ok": False, "error": "Job not found"}
        if job.get("status") == "applied":
            return {"ok": False, "error": "Already applied — undo it first to re-identify"}
        path = job.get("original_path")
        if not path:
            return {"ok": False, "error": "Job has no source path"}
        # Recreate-then-delete: build the replacement first and only drop the
        # original once it succeeds. Deleting first would permanently lose the
        # job (and its source-path tracking) if re-processing raised or produced
        # no job — data loss on a routine, user-initiated retry.
        try:
            new_id = self._process_file(job.get("package_name"), path)
        except Exception:
            logger.exception("reidentify: re-process failed for job %s; keeping original", job_id)
            return {"ok": False, "error": "Re-identify failed; original job kept"}
        if not new_id:
            return {"ok": False, "error": "Re-identify produced no job; original kept"}
        db.delete_rename_job(job_id)
        return {"ok": True, "job_id": new_id}

    def reidentify_all(self) -> dict:
        """Re-identify the reviewable jobs (needs_review / failed). Already-matched
        jobs are left alone — re-running them only risks churning a good match;
        use the per-job Re-identify button to retry an individual matched job.
        Single-flighted so a double-click can't race into duplicate work."""
        db = self._db
        if db is None:
            return {"reidentified": 0}
        if not self._bulk_lock.acquire(blocking=False):
            return {"reidentified": 0, "busy": True}
        try:
            jobs = [j for j in (db.list_rename_jobs(limit=100000) or [])
                    if j.get("status") in ("needs_review", "failed")]
            count = 0
            for job in jobs:
                try:
                    self.reidentify(job["id"])
                    count += 1
                except Exception:
                    logger.exception("reidentify_all: job %s failed", job.get("id"))
            logger.info("reidentify_all: re-ran %d job(s)", count)
            return {"reidentified": count}
        finally:
            self._bulk_lock.release()

    @staticmethod
    def _video_files(root: str) -> List[str]:
        out = []
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    out.append(os.path.join(dirpath, f))
        return sorted(out)

    def _process_file(self, package_name, path) -> Optional[int]:
        self._tl.search_memo = {}   # dedup TMDB searches within this one file
        try:
            return self._process_file_inner(package_name, path)
        finally:
            self._tl.search_memo = None

    def _process_file_inner(self, package_name, path) -> Optional[int]:
        filename = os.path.basename(path)
        threshold = self._threshold()
        match = self._identify(filename)

        # Last-resort media fallbacks (read the file itself), only when the
        # filename-based match is still weak AND Ollama is configured.
        if (self._cfg.get("auto_rename_llm_enabled")
                and (not match or match.get("confidence", 0) < threshold)):
            base_url = self._cfg.get("ollama_base_url", "")
            model = self._cfg.get("ollama_model", "")
            if base_url and model:
                parsed = parse_filename(filename)
                p_type = "tv" if parsed.get("is_tv") else "movie"
                # Real candidates so the model picks from options, not thin air.
                cands = self._search_candidates(
                    parsed.get("title"), parsed.get("year"), p_type)

                def _apply(res, src):
                    """Resolve an LLM/vision pick to a TMDB match and adopt it
                    if it beats the current (weak) match."""
                    if not res or not res.get("title"):
                        return False
                    mtype = res.get("media_type") or (
                        "tv" if res.get("season") else p_type)
                    alt = self._tmdb_match(res["title"], res.get("year"), mtype)
                    cur = match.get("confidence", 0) if match else 0
                    if alt and alt["confidence"] > cur:
                        alt.update(
                            source=src, resolution=parsed.get("resolution"),
                            season=res.get("season") or parsed.get("season"),
                            episode=res.get("episode") or parsed.get("episode"),
                            episode_end=parsed.get("episode_end"),
                            part=parsed.get("part"),
                            episode_title=parsed.get("filename_episode_title"),
                            imdb_id=parsed.get("imdb_id"))
                        rlog.info("media  | %s | %s -> %r (%s) conf=%.0f",
                                  filename, src, alt.get("title"),
                                  alt.get("year"), alt.get("confidence", 0))
                        return alt
                    return False

                # Subtitles first — dialogue is highly identifying and it's a
                # cheap text call (no frame extraction / minutes-long vision).
                subs = _llm.identify_from_subtitles(
                    path, base_url=base_url, model=model,
                    candidates=cands or None)
                picked = _apply(subs, "llm_subtitle")
                if picked:
                    match = picked

                # OCR the title card / end credits — fast (tesseract). A title
                # printed on-screen is decisive; failing that, a deterministic
                # cast/director match (enriched here) identifies the film.
                if not match or match.get("confidence", 0) < threshold:
                    self._enrich_with_credits(cands)
                    ocr = _llm.identify_from_credits_ocr(
                        path, base_url=base_url, model=model,
                        candidates=cands or None)
                    picked = _apply(ocr, "ocr_credits")
                    if picked:
                        match = picked

                # Vision is the true last resort: only if still weak after subs.
                if not match or match.get("confidence", 0) < threshold:
                    vision = _llm.identify_from_frames(
                        path, base_url=base_url, model=model,
                        candidates=cands or None)
                    picked = _apply(vision, "llm_vision")
                    if picked:
                        match = picked

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
                season_data = None
                episodes: list = []
                delta: Optional[float] = None
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

                # Season cache for downstream checks — avoids re-fetching same season
                season_num = match.get("season")
                season_cache: dict = {season_num: season_data} if season_data else {}
                llm_cfg = {
                    "base_url": self._cfg.get("ollama_base_url", ""),
                    "model": self._cfg.get("ollama_model", ""),
                }

                # TV episode-intelligence (combined / re-scan / split). Wrapped
                # defensively: these are optional refinements, so any failure
                # must degrade to "no correction" rather than crash the file —
                # the surrounding runtime try/except no longer covers them.
                try:
                    # ── Combined episode detection (runs first) ──────────────
                    # Cheap + high-precision (ratio window + 8% sum match). Must
                    # win over the re-scan for two-part / double-length files,
                    # else a neighbouring long episode could be proposed as a
                    # "wrong episode" correction and suppress the combined one.
                    if (file_min and tmdb_min
                            and mtype == "tv"
                            and episodes
                            and not match.get("episode_end")):
                        combined = _detect_combined_episode(match, file_min, episodes)
                        if combined:
                            match["combined_episode"] = combined
                            match["episode_end"] = combined["episode_end"]

                    # ── Episode re-scan (only when runtime is suspicious) ────
                    if (delta is not None and delta < -10
                            and mtype == "tv"
                            and file_min
                            and match.get("episode")
                            and not match.get("episode_end")
                            and not match.get("combined_episode")
                            and client):
                        correction = _try_episode_rescan(
                            match, client, file_min, season_cache,
                            int(match["tmdb_id"]), llm_cfg)
                        if correction:
                            match["suggested_correction"] = correction

                    # ── Split file detection ─────────────────────────────────
                    if (mtype == "tv"
                            and file_min and tmdb_min
                            and not match.get("combined_episode")
                            and not match.get("suggested_correction")):
                        split = _detect_split_file(path, file_min, tmdb_min)
                        if split:
                            match["split_file"] = split
                except Exception:
                    logger.exception(
                        "Episode-intelligence step failed for %s; continuing "
                        "without correction", filename)

        job = {"package_name": package_name, "original_path": path,
               "original_filename": filename, "status": "pending"}

        if not match:
            job.update(status="needs_review", warning_message="No confident match found")
            return self._create(job)

        fname, dest = _naming.build_target(
            {**match, "original_filename": filename},
            movie_root=self._movie_root(match.get("resolution")),
            tv_root=self._cfg.get("auto_rename_tv_library", ""),
            template=self._template_for(match.get("media_type")))
        conf = match.get("confidence") or 0.0
        job.update(
            media_type=match.get("media_type"), title=match.get("title"),
            year=match.get("year"), season=match.get("season"),
            episode=match.get("episode"), tmdb_id=match.get("tmdb_id"),
            imdb_id=match.get("imdb_id"), resolution=match.get("resolution"),
            poster_path=match.get("poster_path"),
            match_confidence=conf, match_source=match.get("source"),
            new_filename=fname, destination_path=dest,
            suggested_correction=match.get("suggested_correction"),
            combined_episode=match.get("combined_episode"),
            split_file=match.get("split_file"))

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

        if match.get("suggested_correction"):
            corr = match["suggested_correction"]
            orig = corr["original"]
            prop = corr["proposed"]
            job["warning_message"] = (
                f"Possible wrong episode: "
                f"S{orig['season']:02d}E{orig['episode']:02d} -> "
                f"S{prop['season']:02d}E{prop['episode']:02d} "
                f"\"{prop.get('title', '')}\""
            )

        if match.get("combined_episode"):
            comb = match["combined_episode"]
            job["warning_message"] = (
                f"Likely combined: "
                f"E{comb['episode_start']:02d}+E{comb['episode_end']:02d} "
                f"-> rename as {comb['proposed_code']}"
            )

        if match.get("split_file"):
            sf = match["split_file"]
            job["warning_message"] = (
                f"Likely split file Part {sf['part']} "
                f"(sibling: {os.path.basename(sf['sibling_path'])})"
            )

        # Any proposal (wrong-episode / combined / split) needs a human decision —
        # force needs_review so a flagged file can never silently auto-apply, no
        # matter how high its confidence scored.
        if (match.get("suggested_correction") or match.get("combined_episode")
                or match.get("split_file")):
            job["status"] = "needs_review"

        # Destination library must be configured, or build_target produced a
        # relative path (empty root) that would place the file in the container's
        # CWD instead of the library. Hold for review with a clear message rather
        # than move a file to a junk location.
        mtype = match.get("media_type")
        lib_set, lib_label = self._lib_set(mtype, match.get("resolution"))
        if not lib_set:
            job["status"] = "needs_review"
            # Don't clobber a more specific reason (low confidence, a proposal);
            # only explain the missing library when nothing else has.
            if not job.get("warning_message"):
                job["warning_message"] = (
                    f"{lib_label} library not configured — set it in "
                    f"Settings → Renaming before applying")

        # "Read the file" fallbacks (subtitles / OCR-credits / vision) are
        # heuristics, not authorities — they identify the hard cases the filename
        # couldn't, but they can also be wrong (e.g. two same-genre films sharing
        # cast). Never auto-apply them; always get a human's confirmation.
        if match.get("source") in ("llm_subtitle", "ocr_credits", "llm_vision"):
            if job["status"] == "matched":
                job["status"] = "needs_review"
                if not job.get("warning_message"):
                    job["warning_message"] = (
                        f"Identified via {match['source'].replace('_', ' ')} "
                        f"— please confirm")

        job_id = self._create(job)
        rlog.info("job    | %s | status=%s%s%s", filename, job["status"],
                  f" -> {job['new_filename']}" if job.get("new_filename") else "",
                  f" [{job['warning_message']}]" if job.get("warning_message") else "")
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
            rlog.warning("move   | FAILED %s -> %s (%s): %s", src, dst, method, e)
            db.update_rename_job(job_id, status="failed", error_message=str(e))
            self._broadcast(job_id)
            return {"ok": False, "error": str(e)}
        rlog.info("move   | %s -> %s (%s)", src, dst, used)
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

    def rematch(self, job_id: int, tmdb_id: int, media_type: Optional[str] = None,
                season: Optional[int] = None, episode: Optional[int] = None) -> dict:
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
        poster_path = details.get("poster_path") or job.get("poster_path")
        sea = season if season is not None else job.get("season")
        epi = episode if episode is not None else job.get("episode")
        meta = {**job, "media_type": mtype, "title": title, "year": year,
                "tmdb_id": int(tmdb_id), "season": sea, "episode": epi}
        # Library-not-configured guard (mirrors _process_file_inner).
        lib_set, lib_label = self._lib_set(mtype, job.get("resolution"))
        if not lib_set:
            warning = (f"{lib_label} library not configured — set it in "
                       f"Settings → Renaming before applying")
            db.update_rename_job(job_id, title=title, year=year, tmdb_id=int(tmdb_id),
                                 media_type=mtype, season=sea, episode=epi,
                                 poster_path=poster_path, destination_path=None,
                                 match_confidence=100.0, match_source="manual",
                                 status="needs_review", warning_message=warning)
            self._broadcast(job_id)
            return {"ok": True, "status": "needs_review", "new_filename": None,
                    "destination_path": None, "warning": warning}
        fname, dest = _naming.build_target(
            meta, movie_root=self._movie_root(job.get("resolution")),
            tv_root=self._cfg.get("auto_rename_tv_library", ""),
            template=self._template_for(mtype))
        db.update_rename_job(job_id, title=title, year=year, tmdb_id=int(tmdb_id),
                             media_type=mtype, season=sea, episode=epi,
                             poster_path=poster_path, new_filename=fname,
                             destination_path=dest, match_confidence=100.0,
                             match_source="manual", status="matched",
                             warning_message=None)
        self._broadcast(job_id)
        return {"ok": True, "status": "matched", "new_filename": fname,
                "destination_path": dest, "warning": None}

    def rematch_preview(self, job_id: int, tmdb_id: int,
                        media_type: Optional[str] = None,
                        season: Optional[int] = None,
                        episode: Optional[int] = None) -> dict:
        """Build a would-be target WITHOUT persisting; run the library guard."""
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"new_filename": None, "destination_path": None,
                    "library_configured": False, "warning": "Job not found"}
        mtype = media_type or job.get("media_type") or "movie"
        client = self._tmdb_client()
        details = None
        if client:
            try:
                details = client.details(int(tmdb_id), media_type=mtype)
            except Exception:
                details = None
        if not details:
            return {"new_filename": None, "destination_path": None,
                    "library_configured": False,
                    "warning": "Could not fetch TMDB details"}
        title = details.get("title") or details.get("name") or job.get("title")
        date = details.get("release_date") or details.get("first_air_date") or ""
        year = int(date[:4]) if date[:4].isdigit() else job.get("year")
        sea = season if season is not None else job.get("season")
        epi = episode if episode is not None else job.get("episode")
        meta = {**job, "media_type": mtype, "title": title, "year": year,
                "tmdb_id": int(tmdb_id), "season": sea, "episode": epi}
        lib_set, lib_label = self._lib_set(mtype, job.get("resolution"))
        try:
            fname, dest = _naming.build_target(
                meta, movie_root=self._movie_root(job.get("resolution")),
                tv_root=self._cfg.get("auto_rename_tv_library", ""),
                template=self._template_for(mtype))
        except Exception:
            return {"new_filename": None, "destination_path": None,
                    "library_configured": False,
                    "warning": "Could not build target filename"}
        warning = None
        if not lib_set:
            dest = None
            warning = (f"{lib_label} library not configured — set it in "
                       f"Settings → Renaming before applying")
        return {"new_filename": fname, "destination_path": dest,
                "library_configured": lib_set, "warning": warning}

    def bulk_apply(self, ids: list) -> dict:
        if not self._bulk_lock.acquire(blocking=False):
            return {"results": [], "applied": 0, "failed": 0, "busy": True}
        try:
            results, applied, failed = [], 0, 0
            for jid in ids or []:
                try:
                    out = self.apply(int(jid))
                except Exception as e:
                    out = {"ok": False, "error": str(e)}
                ok = bool(out.get("ok"))
                results.append({"id": int(jid), "ok": ok,
                                "error": out.get("error")})
                applied += 1 if ok else 0
                failed += 0 if ok else 1
            return {"results": results, "applied": applied, "failed": failed}
        finally:
            self._bulk_lock.release()

    def apply_confident(self, ids: Optional[list] = None) -> dict:
        """Apply only matched jobs at confidence >= 95. Server-enforced gate."""
        db = self._db
        if db is None:
            return {"results": [], "applied": 0, "skipped": 0, "failed": 0}
        if ids is not None:
            candidates = []
            for jid in ids:
                job = db.get_rename_job(int(jid))
                if job:
                    candidates.append(job)
        else:
            candidates = db.list_rename_jobs(limit=100000) or []
        if not self._bulk_lock.acquire(blocking=False):
            return {"results": [], "applied": 0, "skipped": 0, "failed": 0,
                    "busy": True}
        try:
            results, applied, skipped, failed = [], 0, 0, 0
            for job in candidates:
                conf = job.get("match_confidence") or 0.0
                if job.get("status") != "matched" or conf < 95:
                    skipped += 1
                    continue
                jid = job["id"]
                try:
                    out = self.apply(int(jid))
                except Exception as e:
                    out = {"ok": False, "error": str(e)}
                ok = bool(out.get("ok"))
                results.append({"id": int(jid), "ok": ok,
                                "error": out.get("error")})
                applied += 1 if ok else 0
                failed += 0 if ok else 1
            return {"results": results, "applied": applied,
                    "skipped": skipped, "failed": failed}
        finally:
            self._bulk_lock.release()

    def bulk_reidentify(self, ids: list) -> dict:
        if not self._bulk_lock.acquire(blocking=False):
            return {"ok": False, "queued": 0, "busy": True}
        try:
            queued = 0
            for jid in ids or []:
                try:
                    result = self.reidentify(int(jid))
                    if result.get("ok"):
                        queued += 1
                except Exception:
                    logger.exception("bulk_reidentify: job %s failed", jid)
            return {"ok": True, "queued": queued}
        finally:
            self._bulk_lock.release()

    def bulk_delete(self, ids: list) -> dict:
        db = self._db
        deleted = 0
        for jid in ids or []:
            try:
                db.delete_rename_job(int(jid))
                deleted += 1
            except Exception:
                logger.exception("bulk_delete: job %s failed", jid)
        return {"deleted": deleted}

    def set_destination(self, job_id: int, root: str) -> dict:
        """Rebuild one job's destination_path under ``root``; re-run guard."""
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"id": int(job_id), "ok": False,
                    "destination_path": None, "error": "Job not found"}
        if job.get("status") == "applied":
            return {"id": int(job_id), "ok": False,
                    "destination_path": job.get("destination_path"),
                    "error": "already applied"}
        if not root or not str(root).strip():
            db.update_rename_job(job_id, status="needs_review",
                                 destination_path=None,
                                 warning_message="Destination library not configured")
            self._broadcast(job_id)
            return {"id": int(job_id), "ok": False, "destination_path": None,
                    "error": "Destination library not configured"}
        mtype = job.get("media_type") or "movie"
        meta = {**job, "media_type": mtype}
        if mtype == "tv":
            fname, dest = _naming.build_target(
                meta, tv_root=root, movie_root=self._movie_root(job.get("resolution")),
                template=self._template_for(mtype))
        else:
            fname, dest = _naming.build_target(
                meta, movie_root=root, tv_root=self._cfg.get("auto_rename_tv_library", ""),
                template=self._template_for(mtype))
        db.update_rename_job(job_id, new_filename=fname, destination_path=dest,
                             status="matched", warning_message=None)
        self._broadcast(job_id)
        return {"id": int(job_id), "ok": True, "destination_path": dest,
                "error": None}

    def bulk_set_destination(self, ids: list, root: str) -> dict:
        if not self._bulk_lock.acquire(blocking=False):
            return {"results": [], "updated": 0, "busy": True}
        try:
            results, updated = [], 0
            for jid in ids or []:
                try:
                    out = self.set_destination(int(jid), root)
                except Exception as e:
                    out = {"id": int(jid), "ok": False,
                           "destination_path": None, "error": str(e)}
                results.append(out)
                updated += 1 if out.get("ok") else 0
            return {"results": results, "updated": updated}
        finally:
            self._bulk_lock.release()

    def search_tmdb_public(self, query: str, media_type: str = "movie") -> list:
        """Search TMDB for the rematch picker; fail-safe → [] on any problem."""
        if not query or not query.strip():
            return []
        mtype = "tv" if media_type == "tv" else "movie"
        client = self._tmdb_client()
        if not client:
            return []
        try:
            raw = client.search(query.strip(), media_type=mtype) or []
        except Exception:
            return []
        out = []
        for r in raw:
            cand = self._normalize_candidate(r, mtype)
            if not cand:
                continue
            out.append({"tmdb_id": cand.get("tmdb_id"),
                        "title": cand.get("title"),
                        "year": cand.get("year"),
                        "media_type": mtype,
                        "poster_path": cand.get("poster_path")})
        return out

    def accept_combined(self, job_id: int) -> dict:
        """Accept a runtime-detected combined-episode proposal.

        The new_filename is already correct (episode_end was set before build_target),
        so accepting just clears the proposal and promotes the job to matched.
        """
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"ok": False, "error": "Job not found"}
        if not job.get("combined_episode"):
            return {"ok": False, "error": "No combined episode proposal on this job"}
        db.update_rename_job(job_id, status="matched", warning_message=None,
                             combined_episode=None)
        self._broadcast(job_id)
        return {"ok": True}

    def accept_correction(self, job_id: int) -> dict:
        """Accept a runtime-gated wrong-episode correction proposal.

        Re-generates new_filename and destination_path from the proposed S/E,
        then promotes the job to matched.
        """
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"ok": False, "error": "Job not found"}
        correction = job.get("suggested_correction")
        if not correction:
            return {"ok": False, "error": "No episode correction proposal on this job"}
        proposed = correction.get("proposed", {})
        meta = {k: job.get(k) for k in (
            "title", "year", "media_type", "tmdb_id", "imdb_id",
            "resolution", "original_filename", "plex_sort_title")}
        meta.update(
            season=proposed.get("season", job.get("season")),
            episode=proposed.get("episode", job.get("episode")),
            # Use the corrected episode's TMDB title — the proposed episode is a
            # different one, so any filename-parsed title would be the wrong show's.
            episode_title=proposed.get("title") or None,
            episode_end=None,
            part=None)
        fname, dest = _naming.build_target(
            meta,
            movie_root=self._movie_root(meta.get("resolution")),
            tv_root=self._cfg.get("auto_rename_tv_library", ""),
            template=self._template_for("tv"))
        db.update_rename_job(
            job_id,
            season=meta["season"], episode=meta["episode"],
            new_filename=fname, destination_path=dest,
            suggested_correction=None,
            status="matched", warning_message=None)
        self._broadcast(job_id)
        return {"ok": True, "new_filename": fname}

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
