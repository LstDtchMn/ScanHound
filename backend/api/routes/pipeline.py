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
from backend.sources.registry import SourceRegistry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/items")
def get_items(category: Optional[str] = None, include_dismissed: bool = False,
             reg: ServiceRegistry = Depends(get_registry)):
    if not reg.db:
        return []
    return reg.db.get_pipeline_verdicts(category=category, include_dismissed=include_dismissed)


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
    return {"status": "started"}
