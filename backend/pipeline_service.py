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


class _PlexLookupError(Exception):
    """find_plex_match hit a real error (DB failure, bad data) — distinct from
    a clean no-match (None). Callers map this to 'unknown' rather than letting
    it masquerade as a confirmed 'not_in_plex'."""


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
        raise _PlexLookupError()


def categorize(download_row: dict, result_row: Optional[dict], rename_rows: list,
               plex_max_ts: dict, jd_method: str, grace_margin_minutes: int = 30,
               db=None) -> tuple:
    """Returns (category, detail, package_uuid, plex_rating_key). Never raises
    — any unexpected shape falls through to ('unknown', None, None, None)."""
    try:
        if result_row is None:
            if jd_method != "api":
                return ("unknown", "folder-mode grab has no results row to reconcile", None, None)
            # The download_results row is NOT permanent: the Downloads UI's
            # per-item "remove" (removeDownloadResult) and "clear all"
            # (clearDownloadResults) actions delete rows, and the poller never
            # repopulates a deleted one. So a fully-processed grab (all rename
            # jobs 'applied', Plex already ingesting) can end up here purely
            # because the user did routine Downloads housekeeping. When we still
            # hold rename_jobs evidence, trust it and fall through to the same
            # rename-status/Plex-verification logic used when a result row
            # exists — rather than blindly declaring 'never_started' and
            # surfacing a Re-grab button for a release already in Plex.
            #
            # GATE (regrab safety): only fall through when this grab has NEVER
            # been regrabbed (no excluded_uuid). excluded_uuid is populated by
            # clear_pipeline_verdict on every regrab/grab-alternative and never
            # cleared, so an empty value proves there is exactly one attempt and
            # the rename_rows unambiguously belong to it. If it IS set, the
            # rename_rows may be STALE leftovers from a superseded prior attempt
            # (regrab clears the download_results uuid pin but does not delete
            # old rename_jobs rows), so we must not trust them — fall back to the
            # honest never_started/too-soon logic below.
            if rename_rows and not download_row.get("excluded_uuid"):
                return _categorize_from_rename_rows(
                    download_row, rename_rows, plex_max_ts,
                    package_uuid=None, grace_margin_minutes=grace_margin_minutes, db=db)
            last_grabbed = download_row.get("last_grabbed_at")
            if last_grabbed and _minutes_since(last_grabbed) > 30:
                # Two distinct causes (per the design spec): downloads.status
                # is written 'failed' ONLY by download_service's final
                # honest-failure path (links never delivered to JD at all);
                # any other status means the send reported success but the
                # package never surfaced in JD's queue.
                if download_row.get("status") == "failed":
                    detail = ("The links were never sent to JDownloader — "
                              "the send failed.")
                else:
                    detail = ("Grabbed over 30 minutes ago but never appeared in "
                              "JDownloader's queue — the links may not have been "
                              "delivered.")
                return ("never_started", detail, None, None)
            return (None, None, None, None)  # too soon to judge

        state = result_row.get("state")
        package_uuid = result_row.get("package_uuid")

        if state == "failed":
            return ("download_failed", result_row.get("error"), package_uuid, None)
        if state in _ACTIVE_DOWNLOAD_STATES:
            return ("downloading", None, package_uuid, None)
        if state == "extracted" and not rename_rows:
            return ("pending_rename", None, package_uuid, None)

        return _categorize_from_rename_rows(
            download_row, rename_rows, plex_max_ts,
            package_uuid=package_uuid, grace_margin_minutes=grace_margin_minutes, db=db)
    except Exception:
        logger.exception("categorize failed for %s", download_row.get("url"))
        return ("unknown", "categorize error", None, None)


def _categorize_from_rename_rows(download_row: dict, rename_rows: list,
                                 plex_max_ts: dict, package_uuid: Optional[str],
                                 grace_margin_minutes: int, db) -> tuple:
    """Derive a verdict from rename_jobs evidence (+ Plex-cache freshness gate).

    Shared by the normal 'result row present and extracted' path and by the
    fallthrough categorize() uses when the download_results row was deleted out
    from under a fully-processed grab. Depends only on rename_rows / download_row
    / plex_max_ts / db — never on result_row — so it is safe to call in both.
    Callers that want the applied->Plex verification pass in rename_rows; with
    no rename_rows this returns ('unknown', ...).
    """
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
            return ("awaiting_plex_refresh", None, package_uuid, None)
        resolution = latest.get("resolution") or download_row.get("resolution")
        try:
            match = find_plex_match(db, latest.get("imdb_id"), latest.get("title"),
                                    latest.get("year"), latest.get("season"), resolution)
        except _PlexLookupError:
            return ("unknown", "Plex lookup failed — will retry next pass", package_uuid, None)
        if match:
            return ("verified", None, package_uuid, str(match.get("rating_key") or ""))
        return ("not_in_plex", None, package_uuid, None)

    return ("unknown", None, package_uuid, None)


def _minutes_since(sqlite_timestamp: str) -> float:
    try:
        dt = datetime.strptime(sqlite_timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def _match_download_results(conn, download_row: dict) -> Optional[dict]:
    """Match one grab to its download_results row.

    uuid-first: a previously-recorded verdict.package_uuid (passed in under the
    key 'verdict_package_uuid') pins the match to that exact package, so a
    verdict that has already locked onto a specific download keeps following it.

    Fallback: name + last_grabbed_at-window, with any accumulated excluded_uuid
    (a comma-joined list built by clear_pipeline_verdict on each regrab) filtered
    out, tiebreaking on MAX(id) — the surrogate AUTOINCREMENT primary key, which
    is strictly monotonic even when two repolled rows share the same
    second-resolution updated_at. The tiebreak is deliberately MAX(id), NOT
    state-progression: after a regrab-following-a-late-failure, the newer package
    (higher id) may legitimately be at an *earlier* pipeline stage than the stale
    one, and it must still win. See the plan's Task 4 rationale.
    """
    uuid = download_row.get("verdict_package_uuid")
    if uuid:
        cur = conn.execute(
            "SELECT * FROM download_results WHERE package_uuid = ?", (uuid,))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
    excluded = (download_row.get("excluded_uuid") or "").split(",")
    excluded = [e for e in excluded if e]
    name = download_row.get("jd_confirmed_name") or download_row.get("package_name")
    last_grabbed = download_row.get("last_grabbed_at")
    if not name or not last_grabbed:
        return None
    sql = ("SELECT * FROM download_results WHERE name = ? "
           "AND updated_at >= datetime(?, '-5 seconds')")
    params = [name, last_grabbed]
    if excluded:
        # Only filter uuids when there is something to exclude. Keep NULL-uuid
        # rows (a fresh package not yet assigned a uuid) AND any non-excluded
        # uuid; `package_uuid NOT IN (...)` alone would drop NULL rows because
        # `NULL NOT IN (...)` is NULL/falsy in SQL. When `excluded` is empty we
        # add no uuid predicate at all — every name+window row is a candidate.
        placeholders = ",".join("?" * len(excluded))
        sql += f" AND (package_uuid IS NULL OR package_uuid NOT IN ({placeholders}))"
        params.extend(excluded)
    sql += " ORDER BY id DESC LIMIT 1"
    cur = conn.execute(sql, tuple(params))
    row = cur.fetchone()
    return dict(row) if row else None


def _match_rename_rows(conn, package_name: Optional[str]) -> list:
    """All rename_jobs rows for this grab's package_name (there can be several
    when a package expands to multiple media files)."""
    if not package_name:
        return []
    cur = conn.execute("SELECT * FROM rename_jobs WHERE package_name = ?", (package_name,))
    return [dict(r) for r in cur.fetchall()]


def reconcile_batch(db, limit: int = 500, jd_method: str = "api",
                    grace_margin_minutes: int = 30) -> int:
    """Reconcile up to `limit` eligible grabs and upsert their verdicts.

    Returns the count processed. Per-item failures are caught and categorized
    'unknown' rather than aborting the batch (this function itself does not
    swallow batch-level errors — the maintenance-loop caller wraps this in its
    own try/except). `jd_method` is passed by the caller (Task 5's maintenance
    hook, which owns the live config) as `config.get("jd_method", "folder")`;
    it defaults to "api" here since DatabaseManager holds no config reference.
    `grace_margin_minutes` is likewise forwarded from the caller's live config
    (`pipeline_verify_grace_margin_minutes`, default 30) down to categorize()'s
    Plex-cache-freshness gate; it defaults to 30 here to match categorize()'s
    own default so existing callers that don't pass it are unaffected.
    """
    candidates = db.get_downloads_needing_reconcile(limit=limit)
    if not candidates:
        return 0
    conn = db.get_connection()
    if not conn:
        return 0
    plex_max_ts = db.get_plex_cache_max_timestamp()
    processed = 0
    for row in candidates:
        try:
            row["verdict_package_uuid"] = row.get("package_uuid")
            result_row = _match_download_results(conn, row)
            effective_name = row.get("jd_confirmed_name") or row.get("package_name")
            rename_rows = _match_rename_rows(conn, effective_name)
            category, detail, package_uuid, plex_rating_key = categorize(
                row, result_row, rename_rows, plex_max_ts, jd_method=jd_method,
                grace_margin_minutes=grace_margin_minutes, db=db)
            if category is not None:
                db.upsert_pipeline_verdict(row["url"], category, detail=detail,
                                           package_uuid=package_uuid,
                                           plex_rating_key=plex_rating_key)
            processed += 1
        except Exception:
            logger.exception("reconcile_batch: item failed for %s", row.get("url"))
            try:
                db.upsert_pipeline_verdict(row["url"], "unknown", detail="reconcile error")
            except Exception:
                pass
            processed += 1
    return processed
