"""System endpoints: health check, version, shutdown, TMDB discover."""
import logging

import requests
from fastapi import APIRouter, Depends, Query

from backend.api.dependencies import ServiceRegistry, get_registry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])

__version__ = "2.0.0-dev"
TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w300"


@router.get("/health")
def health(reg: ServiceRegistry = Depends(get_registry)):
    """Liveness, plus the facts an external watchdog needs to spot a stall.

    ``jd_poll`` is reported because a stalled JDownloader poller is otherwise
    INVISIBLE: the downloads list simply stops changing, and on 2026-08-15 that
    went unnoticed for ~15 hours. It is deliberately exposed on the unauthenticated
    health route so the scheduled host-side checker can read it without holding a
    credential — it contains no secrets, only "how long since JD last answered".

    ``jd_enabled`` is included so a watcher can tell "JD is off, silence is
    correct" from "JD is on and has gone quiet", which are the same absence of
    data and opposite conclusions.
    """
    cfg = reg.config or {}
    body = {
        "status": "ok",
        "version": __version__,
        "plex_connected": bool(
            reg.plex and getattr(reg.plex, "plex_movies", None)
        ),
        "jd_enabled": bool(cfg.get("jd_enabled") and cfg.get("jd_method") == "api"),
    }
    # Queue stall conditions, on the same unauthenticated health route and for
    # the same reason as jd_poll: the scheduled host checker must be able to see
    # a stalled queue without holding a credential. Three separate conditions,
    # because one timer cannot tell "nothing was attempted" from "everything
    # attempted failed" -- the ambiguity that made the 2026-08-13 incident
    # unresolvable.
    if reg.db is not None and hasattr(reg.db, "queue_stall_report"):
        try:
            body["queue"] = reg.db.queue_stall_report()
        except Exception:  # noqa: BLE001
            body["queue"] = None

    # ARM REVISION LIFECYCLE. Round 22 (R21-4).
    #
    # On the day a parser or a request definition changes, the active revision
    # becomes one nothing has observed yet and every existing row silently
    # belongs to a retired one. That is the CONSERVATIVE outcome -- widening
    # decisions needing the active revision become unknown rather than false --
    # and it must not be repaired by rewriting history. The defect was that it
    # happened quietly.
    #
    # Reported here for the same reason jd_poll and queue are: this route is
    # what an external watcher can read without holding a credential, and a
    # transition nobody can see is a transition nobody will act on.
    #
    # COUNTS ONLY, no arm ids. The route is unauthenticated, and a watcher only
    # needs to know that some arm is in the state; which one is in the log and
    # behind the authenticated surfaces.
    if reg.db is not None and hasattr(reg.db, "revision_lifecycle_summary"):
        try:
            from backend.arms import default_registry

            _states = {}
            for _row in reg.db.revision_lifecycle_summary(default_registry()):
                _states[_row["state"]] = _states.get(_row["state"], 0) + 1
            body["arm_revisions"] = _states
        except Exception:  # noqa: BLE001
            # Health must never fail because a sub-report failed; an absent key
            # is itself informative and the watcher treats it as unknown.
            body["arm_revisions"] = None

    health_fn = getattr(reg.download, "jd_poll_health", None) if reg.download else None
    if callable(health_fn):
        try:
            body["jd_poll"] = health_fn()
        except Exception:  # noqa: BLE001
            # Health must never fail because a sub-report failed; an absent key
            # is itself informative and the watcher treats it as unknown.
            body["jd_poll"] = None
    return body


@router.get("/discover")
def discover(
    category: str = Query("trending", description="trending, popular, top_rated, upcoming"),
    page: int = Query(1, ge=1, le=10),
    reg: ServiceRegistry = Depends(get_registry),
):
    """Fetch trending/popular movies from TMDB for discovery."""
    api_key = reg.config.get("tmdb_api_key", "")
    if not api_key:
        return {"items": [], "total_pages": 0, "error": "TMDB API key not configured"}

    endpoints = {
        "trending": f"{TMDB_API_BASE}/trending/movie/week",
        "popular": f"{TMDB_API_BASE}/movie/popular",
        "top_rated": f"{TMDB_API_BASE}/movie/top_rated",
        "upcoming": f"{TMDB_API_BASE}/movie/upcoming",
    }
    url = endpoints.get(category, endpoints["trending"])
    try:
        resp = requests.get(url, params={"api_key": api_key, "page": page}, timeout=10)
        if resp.status_code != 200:
            return {"items": [], "total_pages": 0, "error": f"TMDB returned {resp.status_code}"}
        data = resp.json()
        items = []
        for m in data.get("results", []):
            items.append({
                "id": m.get("id"),
                "title": m.get("title", ""),
                "year": (m.get("release_date") or "")[:4] or None,
                "overview": m.get("overview", ""),
                "poster_url": f"{TMDB_IMAGE_BASE}{m['poster_path']}" if m.get("poster_path") else "",
                "rating": m.get("vote_average"),
                "votes": m.get("vote_count"),
                "genre_ids": m.get("genre_ids", []),
            })
        return {"items": items, "total_pages": data.get("total_pages", 1), "page": page}
    except Exception as e:
        logger.warning("TMDB discover failed: %s", e)
        return {"items": [], "total_pages": 0, "error": str(e)}


@router.post("/shutdown", status_code=202)
def shutdown(reg: ServiceRegistry = Depends(get_registry)):
    reg.request_shutdown()
    return {"status": "shutting_down"}
