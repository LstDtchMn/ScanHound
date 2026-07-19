"""Pipeline tracker endpoints: browse reconcile verdicts, dismiss, regrab,
search other sources, grab an alternative release."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.downloads import _run_grab, DownloadRequest
from backend.api.routes.rename import _poster_url
from backend.sources.registry import SourceRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/items")
def get_items(category: Optional[str] = None, include_dismissed: bool = False,
             reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        return []
    rows = reg.db.get_pipeline_verdicts(category=category, include_dismissed=include_dismissed)
    # get_pipeline_verdicts() returns the raw TMDB poster_path (e.g.
    # "/abc123.jpg") as stored in rename_jobs -- not a browser-loadable URL.
    # Every other poster-bearing route prefixes it with TMDB_IMAGE_BASE
    # before it reaches the client (rename.py's _poster_url, scanner.py,
    # system.py); do the same here rather than shipping the raw path.
    for row in rows:
        row["poster_url"] = _poster_url(row.pop("poster_path", None))
    return rows


@router.get("/counts")
def get_counts(reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        return {}
    rows = reg.db.get_pipeline_verdicts()
    counts: dict = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return counts


class UrlRequest(BaseModel):
    url: str


@router.post("/dismiss")
def dismiss_item(req: UrlRequest, reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database unavailable")
    reg.db.dismiss_pipeline_verdict(req.url)
    return {"ok": True}


@router.post("/regrab")
def regrab_item(req: UrlRequest, background_tasks: BackgroundTasks,
                reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl or not reg.db:
        raise HTTPException(status_code=503, detail="Download service not available")
    rows = reg.db.get_downloads_needing_reconcile(limit=100000)
    row = next((r for r in rows if r["url"] == req.url), None)
    if row is None:
        # Grab may already be in a terminal/dismissed state (not in the
        # eligible set) — fetch the raw downloads row directly instead.
        conn = reg.db.get_connection()
        cur = conn.execute(
            "SELECT title, year, season, resolution, size, hdr, dovi, service_type "
            "FROM downloads WHERE url = ?", (req.url,))
        raw = cur.fetchone()
        if raw is None:
            raise HTTPException(status_code=404, detail="Grab not found")
        row = dict(raw)
    reg.db.clear_pipeline_verdict(req.url)
    dl_req = DownloadRequest(
        url=req.url, title=row.get("title") or "Untitled", season=row.get("season"),
        year=row.get("year"), resolution=row.get("resolution") or "",
        size=row.get("size") or "", hdr=row.get("hdr") or "", dovi=bool(row.get("dovi")),
        service_type=row.get("service_type") or "Rapidgator",
    )
    background_tasks.add_task(_run_grab, dl, reg, dl_req, True)
    return {"status": "started"}


@router.post("/search-sources")
async def search_sources(req: UrlRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Look up a grab's title and search every configured, non-auth source
    for alternative releases. Sources whose config declares requires_auth
    (currently only adithd) need an interactive Selenium session this
    stateless backend request can't provide, so they're excluded from the
    results rather than surfaced as an error."""
    if not reg.db:
        raise HTTPException(status_code=503, detail="Database unavailable")
    conn = reg.db.get_connection()
    cur = conn.execute("SELECT title, season FROM downloads WHERE url = ?", (req.url,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Grab not found")
    title = row["title"] if hasattr(row, "keys") else row[0]
    season = row["season"] if hasattr(row, "keys") else row[1]
    mode = "tv" if season is not None else "movies"

    source_registry = SourceRegistry()
    source_registry.discover_sources()
    source_registry.sync_from_config(reg.config)
    try:
        results = await asyncio.wait_for(source_registry.search_all(title, mode), timeout=45.0)
    except asyncio.TimeoutError:
        return {"releases": [], "errors": ["Search timed out"]}
    except Exception as e:
        logger.exception("search-sources failed")
        return {"releases": [], "errors": [str(e)]}

    releases, errors, seen_urls = [], [], set()
    for source_name, page in results.items():
        source_cfg = next((s.config for s in source_registry.get_enabled_sources()
                           if s.name == source_name), None)
        if source_cfg is not None and getattr(source_cfg, "requires_auth", False):
            continue  # excluded: needs an authenticated Selenium session (e.g. adithd)
        for rel in page.releases:
            if rel.url in seen_urls:
                continue
            seen_urls.add(rel.url)
            releases.append(rel.to_dict())
        errors.extend(page.errors)
    return {"releases": releases, "errors": errors}


class AlternativeReleaseRequest(BaseModel):
    display_title: str
    url: str
    year: Optional[int] = None
    res: str = ""
    size: str = ""
    dovi: bool = False
    hdr: str = ""
    season: Optional[int] = None
    # URL of the original failed/stalled grab this alternative replaces, if
    # any (the pipeline tracker's "Search sources" flow always supplies it;
    # left optional for any future caller that grabs an alternative without
    # an original grab to resolve). When present, its pipeline verdict is
    # dismissed once the alternative grab is backgrounded — see the
    # dismiss vs. clear distinction on dismiss_pipeline_verdict/
    # clear_pipeline_verdict in backend/database.py.
    original_url: Optional[str] = None


@router.post("/grab-alternative")
def grab_alternative(req: AlternativeReleaseRequest, background_tasks: BackgroundTasks,
                     reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    dl_req = DownloadRequest(
        url=req.url, title=req.display_title, season=req.season, year=req.year,
        resolution=req.res, size=req.size, hdr=req.hdr, dovi=req.dovi,
    )
    background_tasks.add_task(_run_grab, dl, reg, dl_req, True)
    if req.original_url and reg.db:
        # Dismiss (not clear) the original's verdict: the user has resolved
        # the situation by grabbing a different release entirely, so the
        # original grab attempt is done — not "pending re-evaluation" (which
        # clear_pipeline_verdict would set it to, risking miscategorization
        # of any partial evidence the original left behind). Swallow errors
        # here (rather than letting them propagate) because add_task above
        # already queued the real grab: an unhandled exception past this
        # point would make FastAPI return an error response instead of the
        # normal one, and background tasks only run when the response they
        # were attached to actually gets sent — so a dismiss failure would
        # silently cancel the grab we just backgrounded, not just the dismiss.
        try:
            reg.db.dismiss_pipeline_verdict(req.original_url)
        except Exception:
            logger.exception("grab-alternative: failed to dismiss original verdict for %s",
                             req.original_url)
    return {"status": "started"}
