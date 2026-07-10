"""Pipeline tracker endpoints: browse reconcile verdicts, dismiss, regrab,
search other sources, grab an alternative release."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.routes.downloads import _run_grab, DownloadRequest

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
