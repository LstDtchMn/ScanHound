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
    return {
        "status": "ok",
        "version": __version__,
        "plex_connected": bool(
            reg.plex and getattr(reg.plex, "plex_movies", None)
        ),
    }


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
