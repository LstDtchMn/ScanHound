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

from backend.database import RenameJobDBError
from backend.filename_utils import parse_filename
from backend.rename import confidence as _confidence
from backend.rename import dv_detect as _dv
from backend.rename import fileops as _fileops
from backend.rename import llm_identify as _llm
from backend.rename import naming as _naming
from backend.rename.mediainfo import probe_specs
# Re-exported for backwards compatibility — these were moved out of this
# module (C5 decomposition) into their own pure, DB-free siblings, but
# callers (backend/api/routes/rename.py, tests) still import them from
# backend.rename.service, so they're pulled back in here unchanged.
from backend.rename.conflicts import (  # noqa: F401
    _dest_key,
    _quality_score,
    _quality_reason,
    recommend_keep,
    destination_conflict_ids,
    conflict_annotations,
    rank_conflict,
)
from backend.rename.episodes import (  # noqa: F401
    _try_episode_rescan,
    _detect_combined_episode,
    _detect_split_file,
    _find_split_sibling,
)

logger = logging.getLogger(__name__)

# Dedicated rename logger, pinned at INFO so each file's parse/query/decision/
# move is visible in production logs WITHOUT enabling app-wide debug. It still
# propagates to the root handlers; pinning only its own level keeps the trace on
# regardless of debug_mode.
rlog = logging.getLogger("scanhound.rename")
rlog.setLevel(logging.INFO)

VIDEO_EXTS = _naming.VIDEO_EXTENSIONS


def _fmt_size(n: int) -> str:
    """Human file size — mirrors the frontend conflictView.formatBytes
    (KB/MB/GB/TB, 1 decimal) so the two never disagree."""
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    v = n / 1024.0
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}"


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


# Friendly names for how a title was identified (a fallback method is itself a
# reason to be less than fully certain).
_MATCH_SOURCE_LABELS = {
    "fuzzy": "a fuzzy title match",
    "vision": "AI vision on the poster/frames",
    "ocr": "OCR of the opening credits",
    "subtitle": "text from the subtitle track",
    "multi": "a broad multi-search",
    "imdb": "an embedded IMDb id",
}


def build_match_reasons(parsed: dict, match: dict, threshold: int) -> list:
    """Human-readable reasons a match is < 100% certain, for the Renames UI.

    Pure/derives from the parsed filename + the chosen match, so it explains the
    same signals that fed confidence scoring (title similarity, year, runtime,
    identification method). Returns [] for an exact 100% match."""
    conf = match.get("confidence") or 0.0
    if conf >= 100:
        return []
    reasons: list = []

    def _norm(s):
        return _re.sub(r"[^a-z0-9]", "", (s or "").lower())

    src = (match.get("source") or "").lower()
    label = _MATCH_SOURCE_LABELS.get(src)
    if label:
        reasons.append(f"Matched using {label} rather than a clean title match")

    p_title = (parsed.get("title") or "").strip()
    m_title = (match.get("title") or "").strip()
    if p_title and m_title and _norm(p_title) != _norm(m_title):
        reasons.append(
            f"Filename title “{p_title}” doesn't exactly match “{m_title}”")

    p_year, m_year = parsed.get("year"), match.get("year")
    try:
        if p_year and m_year and int(p_year) != int(m_year):
            reasons.append(
                f"Year differs: filename says {p_year}, the match is {m_year}")
        elif not p_year and match.get("media_type") != "tv":
            reasons.append("No year in the filename to confirm the match")
    except (TypeError, ValueError):
        pass

    if match.get("runtime_warning"):
        reasons.append(match["runtime_warning"])

    if not reasons:
        reasons.append(f"Title is a close but not exact match (~{conf:.0f}% similar)")

    if conf < threshold:
        reasons.append(
            f"Overall confidence {conf:.0f}% is below the {threshold}% auto-apply threshold")
    return reasons


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
        # Count of files dropped by the most recent process_package() run due
        # to a genuine DB failure (RenameJobDBError) — see process_package's
        # docstring. Not thread-safe across concurrent process_package calls
        # (last-writer-wins); process_folder's returned dict is the
        # thread-safe per-call surface for that case.
        self.last_package_failed_db = 0

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

        A genuine DB failure while creating a job (RenameJobDBError) is logged
        loudly and counted in ``self.last_package_failed_db`` — distinct from a
        file being skipped because it's already tracked — rather than silently
        dropping the file with no trace. The return type stays List[int] for
        backward compatibility with existing callers; the failure count is a
        best-effort side channel for callers that want it (see process_folder's
        richer dict summary for the primary user-facing surface).
        """
        self.last_package_failed_db = 0
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
            except RenameJobDBError as e:
                logger.error("process_package %s: DB failure creating job for %s: %s",
                             package_name, path, e)
                self.last_package_failed_db += 1
                jid = None
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
            created, skipped, failed_db = [], 0, 0
            for path in files:
                if not self._claim_path(path):
                    skipped += 1
                    continue
                try:
                    jid = self._process_file(None, path)
                except RenameJobDBError as e:
                    # Genuine DB failure — distinct from "already tracked"
                    # (skipped) so it's never silently indistinguishable from
                    # a legitimate no-op.
                    logger.error("process_folder: DB failure creating job for %s: %s", path, e)
                    failed_db += 1
                    jid = None
                except Exception:
                    logger.exception("process_folder: failed on %s", path)
                    jid = None
                finally:
                    self._release_path(path)
                if jid:
                    created.append(jid)
            logger.info(
                "process_folder %s: %d file(s), %d new job(s), %d already tracked, %d DB failure(s)",
                resolved, len(files), len(created), skipped, failed_db)
            return {"folder": resolved, "found": len(files),
                    "created": len(created), "skipped": skipped,
                    "failed_db": failed_db, "ids": created}
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

    def scan_conflict_dv(self, job_id: int) -> dict:
        """Detect + cache the DV FEL/MEL layer of a conflict's two files (incoming
        source + existing destination). Reuses dv_detect + the dv_scan cache.
        Intended to run on a background thread (see the route).

        Single-flighted on the shared bulk lock (mirrors scan_folder_dv): a
        dovi_tool RPU walk streams the whole file and can take up to 1800s, so
        a second concurrent scan (of this conflict or a full-folder DV sweep)
        must not run at the same time — it would double the I/O and race the
        same dv_scan rows."""
        db = self._db
        if db is None:
            return {"error": "Database unavailable", "scanned": 0}
        job = db.get_rename_job(job_id)
        if not job:
            return {"error": "Job not found", "scanned": 0}
        if not _dv.available():
            return {"error": "dovi_tool is not installed in this build", "scanned": 0}
        if not self._bulk_lock.acquire(blocking=False):
            return {"error": "Another bulk rename operation is already running",
                    "scanned": 0, "busy": True}
        try:
            dest_dir = job.get("destination_path") or ""
            dst = os.path.join(dest_dir, job.get("new_filename")
                               or os.path.basename(job.get("original_path") or "")) \
                if dest_dir else None
            paths = [p for p in (job.get("original_path"), dst)
                     if p and os.path.isfile(p)]
            scanned = 0
            for path in paths:
                # Skip-check is itself fail-safe: a stat error just means "scan it"
                # (mirrors scan_folder_dv).
                try:
                    st = os.stat(path)
                    if db.dv_scan_is_current(path, st.st_mtime, st.st_size):
                        continue
                except OSError:
                    st = None
                # Detect + record. Any failure is recorded as 'unknown' (with a
                # null signature so a later run retries it) rather than dropped —
                # so a bad file never silently vanishes from the inventory.
                try:
                    layer = _dv.detect_layer(path).get("layer", _dv.LAYER_UNKNOWN)
                    title = parse_filename(os.path.basename(path)).get("title") or None
                    db.upsert_dv_scan(path, layer, title=title,
                                      sig_mtime=(st.st_mtime if st else None),
                                      sig_size=(st.st_size if st else None),
                                      source="scan")
                except Exception:
                    logger.exception("scan_conflict_dv failed on %s", path)
                    try:
                        db.upsert_dv_scan(path, _dv.LAYER_UNKNOWN, sig_mtime=None,
                                          sig_size=None, source="scan")
                    except Exception:
                        logger.exception("scan_conflict_dv: could not record failure for %s", path)
                scanned += 1
            return {"job_id": job_id, "scanned": scanned}
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
            vision_model = self._cfg.get("ollama_vision_model", "")
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
                # Sends raw frame bytes, so it MUST use a vision-capable model
                # (ollama_vision_model) — never the text-only ollama_model,
                # which would silently mishandle/error on image input. If no
                # vision model is configured, skip the rung entirely rather
                # than falling back to the text model.
                if not match or match.get("confidence", 0) < threshold:
                    if not vision_model:
                        logger.info(
                            "vision rung skipped: no ollama_vision_model "
                            "configured for %s", filename)
                    else:
                        vision = _llm.identify_from_frames(
                            path, base_url=base_url, model=vision_model,
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

        # Resolution routing fallback: a filename with no resolution tag
        # (e.g. a 4K release scene-named without "2160p"/"4K") would silently
        # route to the 1080p/default movie root since _movie_root only keys
        # off resolution == '2160p'. Probe the actual video width via ffprobe
        # only when the parse left it unknown — never for already-tagged
        # files, so this never adds a subprocess call to the common case.
        # TV never routes through _movie_root's 4K split, so skip the probe
        # entirely there — it would just be an unnecessary subprocess call.
        if match.get("media_type") != "tv" and not match.get("resolution"):
            width = None
            try:
                width = _llm.probe_video_width(path)
            except Exception:
                width = None  # fail-safe: never let a probe error crash the file
            if width and width >= 3000:
                match["resolution"] = "2160p"

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
            split_file=match.get("split_file"),
            # Explain a less-than-certain match so the UI can show the why.
            # Re-parse the filename here (cheap, pure) rather than relying on a
            # `parsed` local that isn't bound on every code path into this point.
            match_reasons=build_match_reasons(
                parse_filename(filename), match, self._threshold()))

        # TMDB year mismatch: the chosen match's year can legitimately differ
        # from the filename's parsed year by 1 without being penalised by
        # confidence scoring (see confidence.match_confidence) — e.g. a
        # limited theatrical release dated one way in the filename and
        # another in TMDB. That's silent otherwise: the job stores the
        # match's year, the destination folder uses it, and confidence stays
        # high, so the user never sees the substitution. Purely additive/
        # informational — never blocks apply or changes matching/confidence.
        filename_year = parse_filename(filename).get("year")
        match_year = match.get("year")
        year_warn = ""
        if filename_year and match_year and int(filename_year) != int(match_year):
            year_warn = f"Year adjusted {filename_year} -> {match_year} from TMDB match"

        runtime_warn = match.get("runtime_warning", "")
        if conf < threshold:
            msg = f"Low confidence ({conf:.0f} < {threshold})"
            if runtime_warn:
                msg += f"; {runtime_warn}"
            if year_warn:
                msg += f"; {year_warn}"
            job.update(status="needs_review", warning_message=msg)
        else:
            job["status"] = "matched"
            if runtime_warn:
                job["warning_message"] = runtime_warn
            if year_warn:
                job["warning_message"] = (
                    f"{job['warning_message']}; {year_warn}"
                    if job.get("warning_message") else year_warn)

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
            self.apply(job_id, automatic=True)
        return job_id

    def _create(self, job) -> Optional[int]:
        """Persist ``job``. Lets RenameJobDBError propagate (a genuine DB
        failure) rather than swallowing it to None — callers (process_folder /
        process_package) count that distinctly from a legitimate no-op so a
        silently-dropped file is never indistinguishable from "nothing to do"."""
        jid = self._db.create_rename_job(job) if self._db else None
        self._broadcast(jid)
        return jid

    # ── UI-driven actions ─────────────────────────────────────────────

    def apply(self, job_id: int, automatic: bool = False,
             conflict_strategy: Optional[str] = None) -> dict:
        """Apply a matched job's placement.

        ``automatic`` marks an apply with no per-item human confirmation
        (e.g. the auto-rename pipeline with confirmation disabled). Such
        applies never consume the source file — see
        :func:`backend.rename.fileops.place_file`.

        ``conflict_strategy`` resolves a destination collision (see the guard
        below) without requiring a separate review round-trip:
          - ``None`` (default) or ``'skip'`` — hold for review; the file at
            ``dst`` is never touched.
          - ``'overwrite'`` — the file occupying ``dst`` is moved to the
            recoverable trash (:func:`fileops._trash`), never deleted, then
            the incoming file is placed at ``dst``.
          - ``'keep_both'`` — the incoming file is placed under a
            deduped sibling name (:func:`fileops.dedupe_dest`); the existing
            file at ``dst`` is left untouched.
        """
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
        # Destination guard: a job whose library isn't configured has an empty or
        # RELATIVE destination_path (build_target used an empty root). Applying it
        # would place the media under the container's CWD (/app overlay) — invisible
        # to Plex and a multi-GB copy into the writable layer. Refuse and hold for
        # review instead of writing to a junk location. (The identify/rematch paths
        # already guard this; the apply path — reachable via bulk-apply on a
        # needs_review job — did not.)
        dest_dir = job.get("destination_path") or ""
        if not dest_dir or not os.path.isabs(dest_dir):
            msg = ("Library not configured — set the destination in "
                   "Settings → Renaming before applying")
            db.update_rename_job(job_id, status="needs_review", warning_message=msg)
            self._broadcast(job_id)
            return {"ok": False, "error": msg}
        dst = os.path.join(dest_dir,
                           job.get("new_filename") or os.path.basename(src))
        # Collision guard: a prior apply (or a file already present in the
        # library) may already occupy this destination — e.g. two different
        # releases of the same title resolve to the identical target path.
        # place_file() itself refuses to overwrite (raises FileExistsError),
        # but letting that surface as a hard 'failed' job gives the user no
        # guidance. Detect it up front and hold for review instead — never
        # auto-replace or delete the existing file; that stays a manual,
        # explicit user action.
        if os.path.lexists(dst):
            # Same-inode re-apply (already hardlinked to this exact destination)
            # is a no-op success, not a conflict — never trash a file onto
            # itself.
            try:
                if os.path.samefile(src, dst):
                    db.update_rename_job(job_id, status="applied", processed_at=_now(),
                                         conflict_kind=None, conflict_same_size=None)
                    self._broadcast(job_id)
                    return {"ok": True, "already": True}
            except OSError:
                pass
            if conflict_strategy == "overwrite":
                # Displace the occupant into the recoverable trash — never a
                # hard delete — then fall through to the normal placement
                # below (dst is now free).
                _fileops._trash(dst)
            elif conflict_strategy == "keep_both":
                # Place the incoming file alongside the existing one under a
                # deduped sibling name; the existing file is never touched.
                # Persist the rewritten filename so the job record (and any
                # subsequent undo) reflects where the file actually landed.
                new_dst = _fileops.dedupe_dest(dst)
                db.update_rename_job(job_id, new_filename=os.path.basename(new_dst))
                dst = new_dst
            else:
                # None → hold for review (existing behavior); 'skip' → same,
                # explicit — either way the file at dst is left untouched and
                # the job goes back to needs_review (never left 'applying').
                same_size = None
                try:
                    existing_size = os.path.getsize(dst)
                    candidate_size = os.path.getsize(src)
                    same_size = existing_size == candidate_size
                    if same_size:
                        msg = (f"A copy is already in the library at the same size "
                               f"({_fmt_size(existing_size)}) — likely a duplicate. "
                               f"Review to replace or keep.")
                    else:
                        msg = (f"A copy is already in the library "
                               f"(existing {_fmt_size(existing_size)} vs. new "
                               f"{_fmt_size(candidate_size)}). Review to replace or keep.")
                except OSError:
                    msg = "A copy is already in the library. Review to replace or keep."
                # Append to (never clobber) a warning already on the job — e.g. a
                # year-mismatch note set at creation time — so the collision guard
                # never silently discards an earlier reason the file needs review.
                existing = job.get("warning_message")
                combined = f"{existing}; {msg}" if existing else msg
                db.update_rename_job(
                    job_id, status="needs_review", warning_message=combined,
                    conflict_kind="destination_exists", conflict_same_size=same_size)
                self._broadcast(job_id)
                return {"ok": False, "error": msg}
        method = self._cfg.get("auto_rename_move_method", "hardlink")
        deletions_require_confirmation = self._cfg.get(
            "deletions_require_confirmation", True)
        # Per-item progress: only a genuine cross-device COPY streams bytes and
        # fires this; a same-device rename/hardlink completes instantly and
        # never calls it. Throttled to ~2.5 Hz so a big remux doesn't flood the
        # socket, but always emits the final 100%.
        _last_emit = [0.0]

        def _progress(done: int, total: int) -> None:
            import time as _t
            now = _t.monotonic()
            if done < total and (now - _last_emit[0]) < 0.4:
                return
            _last_emit[0] = now
            try:
                from backend.api.ws import ws_manager
                pct = int(done * 100 / total) if total else 0
                ws_manager.broadcast_sync({
                    "type": "rename:progress",
                    "data": {"id": job_id, "bytes_done": done,
                             "bytes_total": total, "pct": pct}})
            except Exception:
                pass
        try:
            used = _fileops.place_file(
                src, dst, method, automatic=automatic,
                deletions_require_confirmation=deletions_require_confirmation,
                progress_cb=_progress)
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
                                 error_message=None,
                                 conflict_kind=None, conflict_same_size=None)
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
        # After undo_place removes the placed file, dst is free — if this apply
        # overwrote a prior file (captured in trash), restore it so undo is
        # symmetric and no data is stranded. Best-effort: a normal undo (no
        # overwrite ever happened) simply finds no matching trash entry.
        # ``restore_warning`` surfaces a failure here to the caller instead of
        # only a server-log line — the new file was still reverted (ok stays
        # True), but the displaced original may be left stranded in trash and
        # the UI/caller should know.
        restore_warning = None
        try:
            dst_key = os.path.normcase(os.path.abspath(dst))
            roots = _fileops.trash_roots(dst)
            cands = [e for e in _fileops.list_trash_entries(roots)
                     if e.get("original_path")
                     and os.path.normcase(os.path.abspath(e["original_path"])) == dst_key
                     and e.get("restorable")]
            cands.sort(key=lambda e: e.get("trashed_at") or "", reverse=True)
            if cands:
                restore_result = _fileops.restore_trash_entry(
                    cands[0]["bucket"], cands[0]["name"], roots)
                if not restore_result.get("ok"):
                    restore_warning = (
                        "The file that was overwritten could not be restored "
                        f"from trash ({restore_result.get('error') or 'unknown error'}); "
                        "it remains recoverable there.")
                    logger.warning(
                        "undo: overwrite-original restore failed for job %s, "
                        "destination %s left stranded in trash: %s",
                        job_id, dst, restore_result.get("error"))
        except Exception:
            restore_warning = ("The file that was overwritten could not be "
                               "restored from trash (see server logs).")
            logger.exception("undo: overwrite-original restore best-effort failed")
        db.update_rename_job(job_id, status="reverted", reverted_at=_now())
        self._broadcast(job_id)
        return {"ok": True, "restore_warning": restore_warning}

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
                                 match_reasons=[],  # manual pick — no uncertainty
                                 status="needs_review", warning_message=warning,
                                 conflict_kind=None, conflict_same_size=None)
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
                             match_source="manual", match_reasons=[],
                             status="matched", warning_message=None,
                             conflict_kind=None, conflict_same_size=None)
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

    def conflict_preview(self, job_id: int) -> dict:
        """Two-file spec comparison for a destination conflict, WITHOUT
        persisting anything (mirrors rematch_preview's no-commit pattern).

        ``existing`` is the probed file already on disk at the job's would-be
        destination (or a {present: False} spec if nothing's there yet);
        ``incoming`` is the probed source file. The recommendation judges
        probed technical specs (see rank_conflict) rather than filenames, so a
        Plex-renamed library file (tags stripped) isn't unfairly beaten by a
        tag-rich but lower-quality incoming release.

        If ``probe_specs`` genuinely FAILED (ffprobe missing/timeout/error —
        distinct from "file doesn't exist", which is a legitimate {present:
        False} result) on either side, that spec is filename-only/bare — never
        emit a confidently-wrong recommendation off degraded data. Both specs
        are still returned (degraded) so the UI can show what it has.
        """
        db = self._db
        job = db.get_rename_job(job_id) if db else None
        if not job:
            return {"existing": None, "incoming": None,
                    "recommended": None, "reason": "Job not found"}
        dest_dir = job.get("destination_path") or ""
        dst = (os.path.join(dest_dir, job.get("new_filename")
                            or os.path.basename(job.get("original_path") or ""))
               if dest_dir else None)
        incoming_probe = probe_specs(job.get("original_path"), db=db)
        incoming = incoming_probe or {
            "present": os.path.exists(job.get("original_path") or ""),
            "path": job.get("original_path")}
        incoming["original_filename"] = job.get("original_filename")
        incoming["resolution"] = incoming.get("resolution") or job.get("resolution")
        existing_probe_failed = False
        if dst and os.path.lexists(dst):
            existing_probe = probe_specs(dst, db=db)
            existing = existing_probe or {"present": True, "path": dst}
            existing["original_filename"] = os.path.basename(dst)
            existing_probe_failed = existing_probe is None
        else:
            existing = {"present": False, "path": dst}
        rec = rank_conflict(existing, {**incoming, "id": job_id})
        if incoming_probe is None or existing_probe_failed:
            rec = {"recommended": None, "reason": None}
        return {"existing": existing, "incoming": incoming,
                "recommended": rec["recommended"], "reason": rec["reason"]}

    def backfill_posters(self, limit: int = 200) -> dict:
        """Fill poster_path on jobs that predate poster capture (2026-07-04).

        Cheap + idempotent: only touches jobs with an empty poster_path. Jobs
        with a tmdb_id use a direct details() lookup; the rest do one search by
        title+year and take the top hit's poster (display-only — the match
        itself is never altered). Safe to run repeatedly (maintenance loop).
        """
        db = self._db
        client = self._tmdb_client()
        if db is None or client is None:
            return {"filled": 0, "checked": 0}
        jobs = [j for j in (db.list_rename_jobs(limit=100000) or [])
                if not j.get("poster_path") and (j.get("title") or "").strip()]
        filled = 0
        for job in jobs[:limit]:
            mtype = job.get("media_type") or "movie"
            poster = None
            try:
                if job.get("tmdb_id"):
                    details = client.details(int(job["tmdb_id"]), media_type=mtype)
                    poster = (details or {}).get("poster_path")
                else:
                    hits = client.search(job["title"], media_type=mtype,
                                         year=job.get("year")) or []
                    poster = (hits[0] or {}).get("poster_path") if hits else None
            except Exception:
                continue  # network blip — the next pass retries
            if poster:
                try:
                    db.update_rename_job(job["id"], poster_path=poster)
                    filled += 1
                except Exception:
                    logger.debug("poster backfill: DB write failed for job %s",
                                 job.get("id"))
        if filled:
            rlog.info("poster | backfilled %d poster(s)", filled)
        return {"filled": filled, "checked": len(jobs[:limit])}

    def queue_apply(self, ids: Optional[list] = None,
                    confident_only: bool = False,
                    conflict_strategy: Optional[str] = None) -> dict:
        """Queue applies to run on a background thread and return immediately.

        Applying moves/copies the file. Cross-device placements (hardlink
        EXDEV → full byte copy through the Docker bind mounts) can take many
        minutes for a remux — far beyond any HTTP/proxy timeout — so the
        request must never wait for them. Each job is flipped to a transient
        'applying' status up front (broadcast, so every client shows progress
        instantly) and lands on applied/failed/needs_review via the per-job
        broadcast in apply().

        With ``confident_only``, only matched jobs at confidence >= 95 are
        eligible (the server-enforced 'Apply all confident' gate).

        ``conflict_strategy`` is threaded through to every queued
        :meth:`apply` call — see its docstring for the overwrite/keep_both/
        skip semantics.
        """
        db = self._db
        if db is None:
            return {"ok": False, "queued": 0, "skipped": 0,
                    "error": "Database unavailable"}
        if ids is not None:
            jobs = [db.get_rename_job(int(j)) for j in ids or []]
            jobs = [j for j in jobs if j]
        else:
            jobs = db.list_rename_jobs(limit=100000) or []

        eligible, skipped = [], 0
        for job in jobs:
            status = job.get("status")
            conf = job.get("match_confidence") or 0.0
            if status in ("applied", "applying"):
                skipped += 1
                continue
            if confident_only and (status != "matched" or conf < 95):
                skipped += 1
                continue
            if not confident_only and status not in ("matched", "needs_review"):
                skipped += 1
                continue
            eligible.append(int(job["id"]))

        if not eligible:
            return {"ok": True, "queued": 0, "skipped": skipped}

        for job in jobs:
            if int(job["id"]) not in set(eligible):
                continue
            try:
                # Remember the status we're leaving so crash recovery restores it
                # (a needs_review job must not come back as auto-appliable 'matched').
                db.update_rename_job(int(job["id"]), status="applying",
                                     prior_status=job.get("status"))
                self._broadcast(int(job["id"]))
            except Exception:
                logger.exception("queue_apply: could not mark job %s applying", job.get("id"))

        def _worker(job_ids: list) -> None:
            # Serialize with other bulk operations; blocking here is fine —
            # we're on a daemon thread, not an HTTP request.
            total = len(job_ids)
            with self._bulk_lock:
                for idx, jid in enumerate(job_ids):
                    job = db.get_rename_job(jid) if db else None
                    self._broadcast_queue(idx, total,
                                          (job or {}).get("title") if job else None)
                    try:
                        self.apply(jid, conflict_strategy=conflict_strategy)
                    except Exception:
                        logger.exception("queued apply failed for job %s", jid)
                        try:
                            db.update_rename_job(
                                jid, status="failed",
                                error_message="apply crashed — see server log")
                            self._broadcast(jid)
                        except Exception:
                            pass
                # Signal completion so the UI clears the queue bar.
                self._broadcast_queue(total, total, None)

        threading.Thread(target=_worker, args=(eligible,), daemon=True,
                         name="rename-apply").start()
        return {"ok": True, "queued": len(eligible), "skipped": skipped}

    def _broadcast_queue(self, done: int, total: int, current_title) -> None:
        """Emit overall apply-queue progress (job ``done`` of ``total``)."""
        try:
            from backend.api.ws import ws_manager
            ws_manager.broadcast_sync({
                "type": "rename:queue_progress",
                "data": {"done": done, "total": total,
                         "current_title": current_title,
                         "active": done < total}})
        except Exception:
            pass

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
                                 warning_message="Destination library not configured",
                                 conflict_kind=None, conflict_same_size=None)
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
                             status="matched", warning_message=None,
                             conflict_kind=None, conflict_same_size=None)
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
            status="matched", warning_message=None,
            conflict_kind=None, conflict_same_size=None)
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
