"""Reconcile every grab through download -> extraction -> rename -> Plex
ingestion into one categorized verdict. Pure/fail-safe: categorize() never
raises. See docs/superpowers/specs/2026-07-10-pipeline-tracker-design.md."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_ACTIVE_DOWNLOAD_STATES = {"queued", "downloading", "extracting", "downloaded"}
_PENDING_RENAME_STATUSES = {"pending", "matched", "applying"}
_FAILED_RENAME_STATUSES = {"failed", "needs_review"}

# ffprobe-style '2160p'/'4K' both mean UHD; Plex's plex_cache.res literal is
# one of "4K"/"1080p"/"720p"/"?" (backend/plex_service.py). "?" is Plex's
# unknown-resolution token, so it normalizes to None and skips the res check.
_RES_EQUIV = {"2160p": "4k", "4k": "4k", "1080p": "1080p", "720p": "720p"}


def _normalize_res(res: Optional[str]) -> Optional[str]:
    if not res:
        return None
    normalized = str(res).lower()
    if normalized == "?":  # Plex's unknown-resolution sentinel — treat as unknown
        return None
    return _RES_EQUIV.get(normalized, normalized)


def find_plex_match(db, imdb_id: Optional[str], title: Optional[str],
                    year: Optional[int], season: Optional[int],
                    resolution: Optional[str]) -> Optional[dict]:
    """Look up a plex_cache row for this rename: imdb_id first, else
    normalized title+year; require season match for TV; skip the resolution
    check when either side is unknown rather than failing it strictly."""
    from backend.app_service import normalize_title  # existing helper (clean_string alias)
    try:
        conn = db.get_connection()
        if not conn:
            return None
        cur = conn.cursor()
        # A single imdb_id / title maps to MANY plex_cache rows: one per season
        # for TV, and one per media version for movies (a library holding both a
        # 1080p and a 4K copy of a film = two rows, same imdb_id, different res).
        # Gather every candidate and pick the first that clears the season+res
        # gates — a plain fetchone() would grab an arbitrary row and wrongly
        # reject the wanted season/resolution even when it is present.
        candidates = []
        if imdb_id:
            cur.execute("SELECT * FROM plex_cache WHERE imdb_id = ?", (imdb_id,))
            candidates = [dict(r) for r in cur.fetchall()]
        if not candidates and title:
            norm = normalize_title(title)
            cur.execute("SELECT * FROM plex_cache")
            for candidate in cur.fetchall():
                cdict = dict(candidate)
                if normalize_title(cdict.get("title") or "") != norm:
                    continue
                if year and cdict.get("year") and int(cdict["year"]) != int(year):
                    continue
                candidates.append(cdict)
        want_res = _normalize_res(resolution)
        for rdict in candidates:
            if season is not None and rdict.get("season") is not None and int(rdict["season"]) != int(season):
                continue
            have_res = _normalize_res(rdict.get("res"))
            if want_res and have_res and want_res != have_res:
                continue
            return rdict
        return None
    except Exception:
        logger.exception("find_plex_match failed")
        return None


def categorize(download_row: dict, result_row: Optional[dict], rename_rows: list,
               plex_max_ts: dict, jd_method: str, grace_margin_minutes: int = 30,
               db=None) -> tuple:
    """Returns (category, detail, package_uuid, plex_rating_key). Never raises
    — any unexpected shape falls through to ('unknown', None, None, None)."""
    try:
        if result_row is None:
            if jd_method != "api":
                return ("unknown", "folder-mode grab has no results row to reconcile", None, None)
            last_grabbed = download_row.get("last_grabbed_at")
            if last_grabbed and _minutes_since(last_grabbed) > 30:
                return ("never_started", None, None, None)
            return (None, None, None, None)  # too soon to judge

        state = result_row.get("state")
        package_uuid = result_row.get("package_uuid")

        if state == "failed":
            return ("download_failed", result_row.get("error"), package_uuid, None)
        if state in _ACTIVE_DOWNLOAD_STATES:
            return ("in_progress", None, package_uuid, None)
        if state == "extracted" and not rename_rows:
            return ("pending_rename", None, package_uuid, None)

        if any(r.get("status") in _FAILED_RENAME_STATUSES for r in rename_rows):
            failed = next(r for r in rename_rows if r.get("status") in _FAILED_RENAME_STATUSES)
            detail = failed.get("error_message") or failed.get("warning_message")
            return ("rename_failed", detail, package_uuid, None)
        if any(r.get("status") in _PENDING_RENAME_STATUSES for r in rename_rows):
            return ("pending_rename", None, package_uuid, None)
        if any(r.get("status") == "reverted" for r in rename_rows):
            return ("rename_failed", "reverted", package_uuid, None)

        if rename_rows and all(r.get("status") == "applied" for r in rename_rows):
            latest = max(rename_rows, key=lambda r: r.get("processed_at") or "")
            processed_at = latest.get("processed_at")
            if not processed_at:
                return ("unknown", None, package_uuid, None)
            dt = datetime.fromisoformat(processed_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            content_type = "TV Shows" if latest.get("media_type") == "tv" else "Movies"
            cache_max = plex_max_ts.get(content_type, 0)
            if cache_max < dt.timestamp() + grace_margin_minutes * 60:
                return ("in_progress", None, package_uuid, None)
            resolution = latest.get("resolution") or download_row.get("resolution")
            match = find_plex_match(db, latest.get("imdb_id"), latest.get("title"),
                                    latest.get("year"), latest.get("season"), resolution)
            if match:
                return ("verified", None, package_uuid, str(match.get("rating_key") or ""))
            return ("not_in_plex", None, package_uuid, None)

        return ("unknown", None, package_uuid, None)
    except Exception:
        logger.exception("categorize failed for %s", download_row.get("url"))
        return ("unknown", "categorize error", None, None)


def _minutes_since(sqlite_timestamp: str) -> float:
    try:
        dt = datetime.strptime(sqlite_timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
