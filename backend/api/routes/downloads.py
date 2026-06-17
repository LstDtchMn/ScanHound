"""Download endpoints: send to JDownloader, history, open in Plex."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.ws import ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/download", tags=["downloads"])


class DownloadRequest(BaseModel):
    url: str
    title: str = "Untitled"
    season: Optional[int] = None
    year: Optional[int] = None
    resolution: str = ""
    size: str = ""
    service_type: str = "Rapidgator"


class BatchDownloadRequest(BaseModel):
    items: List[DownloadRequest]


class ScrapeRequest(BaseModel):
    url: str
    service_type: str = "Rapidgator"
    title: str = ""
    resolution: str = ""


class OpenPlexRequest(BaseModel):
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    imdb_id: Optional[str] = None
    plex_rating_key: Optional[str] = None


@router.post("")
def download_item(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    if len(req.title.strip()) < 2:
        raise HTTPException(status_code=400, detail="Title must be at least 2 characters")
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")

    def _do_download():
        try:
            def _on_progress(event: str, data: dict):
                ws_manager.broadcast_sync({"type": event, "data": data})

            result = dl.download_item(
                url=req.url, title=req.title, season=req.season,
                resolution=req.resolution, size=req.size,
                service_type=req.service_type, year=req.year,
                progress_callback=_on_progress,
            )
            # Surface the *actual* delivery method so a silent clipboard/browser
            # fallback (e.g. JDownloader disabled or folder unset) is visible.
            method = (result or {}).get("method", "")
            message = (result or {}).get("message", "") or f"Sent: {req.title}"
            if method and method != "jdownloader":
                message = f"{message} (JDownloader not used — method: {method})"
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download", "body": message, "priority": "normal"},
            })
        except Exception as e:
            logger.exception("Download failed for %s", req.title)
            try:
                dl.save_to_history(req.url, req.title, req.season, req.resolution, req.size, status="failed")
            except Exception:
                pass
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download Failed", "body": str(e), "priority": "high"},
            })

    background_tasks.add_task(_do_download)
    return {"status": "started", "title": req.title}


@router.post("/batch")
def download_batch(
    req: BatchDownloadRequest,
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")

    total = len(req.items)

    def _do_batch():
        try:
            def _on_progress(event: str, data: dict):
                ws_manager.broadcast_sync({"type": event, "data": data})

            # Each item is scraped for its real file-host links before being
            # sent on. Previously the raw page URLs (e.g. hdencode.org posts)
            # were forwarded to JDownloader, which can't resolve them.
            for i, item in enumerate(req.items):
                ws_manager.broadcast_sync({
                    "type": "download:batch_progress",
                    "data": {"completed": i, "total": total, "current_title": item.title},
                })
                dl.download_item(
                    url=item.url, title=item.title, season=item.season,
                    resolution=item.resolution, size=item.size,
                    service_type=item.service_type, year=item.year,
                    progress_callback=_on_progress,
                )
            ws_manager.broadcast_sync({
                "type": "download:batch_progress",
                "data": {"completed": total, "total": total, "current_title": ""},
            })
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Batch Download", "body": f"Processed {total} item(s)", "priority": "normal"},
            })
        except Exception as e:
            logger.exception("Batch download failed")
            for item in req.items:
                try:
                    dl.save_to_history(item.url, item.title, item.season, item.resolution, item.size, status="failed")
                except Exception:
                    pass
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Batch Failed", "body": str(e), "priority": "high"},
            })

    background_tasks.add_task(_do_batch)
    return {"status": "started", "count": total}


@router.post("/scrape")
def scrape_links(
    req: ScrapeRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Scrape the real file-host links from a source page and return them.

    Unlike ``/download``, this performs no side effects (no JDownloader, no
    history). The frontend copies the returned links to the clipboard so
    JDownloader's clipboard monitor can pick them up automatically.
    """
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    if not req.url:
        raise HTTPException(status_code=400, detail="No URL provided")
    try:
        links = dl.scrape_links(req.url, req.service_type)
    except Exception as e:
        logger.exception("Scrape failed for %s", req.url)
        raise HTTPException(status_code=502, detail=f"Scrape failed: {e}")
    if links and req.title and reg.db:
        try:
            reg.db.record_scraped_links(links, req.title, req.resolution, req.url)
        except Exception:
            pass
    return {"links": links, "count": len(links)}


class ScrapeBatchRequest(BaseModel):
    items: List[ScrapeRequest]


@router.post("/copy-links")
def copy_links_batch(
    req: ScrapeBatchRequest,
    background_tasks: BackgroundTasks,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Scrape every selected item and copy the combined links to the clipboard.

    Runs in the background (scraping is slow) and copies server-side via the
    OS clipboard, so JDownloader's clipboard monitor picks up the whole batch
    at once. Progress and the final result are reported over the WebSocket.
    """
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    if not req.items:
        raise HTTPException(status_code=400, detail="No items provided")

    items = req.items
    total = len(items)

    def _do_copy():
        all_links: List[str] = []
        seen = set()
        for i, it in enumerate(items):
            ws_manager.broadcast_sync({
                "type": "download:scrape_progress",
                "data": {"completed": i, "total": total, "current": it.url},
            })
            try:
                links = dl.scrape_links(it.url, it.service_type)
            except Exception:
                logger.exception("Batch scrape failed for %s", it.url)
                links = []
            for link in links:
                if link not in seen:
                    seen.add(link)
                    all_links.append(link)
            if links:
                # Remember which movie/show these links belong to.
                if it.title and reg.db:
                    try:
                        reg.db.record_scraped_links(links, it.title, it.resolution, it.url)
                    except Exception:
                        pass
                # Tell the UI this item's links were grabbed → mark it Downloaded.
                ws_manager.broadcast_sync({
                    "type": "download:complete",
                    "data": {"url": it.url, "method": "clipboard", "link_count": len(links)},
                })

        copied = dl.copy_to_clipboard(all_links) if all_links else False
        ws_manager.broadcast_sync({
            "type": "download:scrape_progress",
            "data": {"completed": total, "total": total, "current": ""},
        })
        if not all_links:
            body = f"No links found across {total} item(s)"
        elif copied:
            body = f"Copied {len(all_links)} link(s) from {total} item(s) — JDownloader should grab them"
        else:
            body = f"Found {len(all_links)} link(s) but clipboard copy failed"
        ws_manager.broadcast_sync({
            "type": "notification",
            "data": {"title": "Copy Links", "body": body, "priority": "normal" if all_links else "high"},
        })

    background_tasks.add_task(_do_copy)
    return {"status": "started", "count": total}


@router.post("/open-plex")
def open_in_plex(
    req: OpenPlexRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    dl = reg.download
    plex = reg.plex
    if not dl or not plex:
        raise HTTPException(status_code=503, detail="Download or Plex service not available")
    try:
        url = dl.open_in_plex(
            title=req.title,
            plex_movies=plex.plex_movies or [],
            plex_tv=plex.plex_tv or [],
            year=req.year,
            season=req.season,
            imdb_id=req.imdb_id,
            plex_rating_key=req.plex_rating_key,
        )
    except Exception as e:
        logger.exception("Failed to open in Plex: %s", req.title)
        raise HTTPException(status_code=502, detail=f"Plex lookup failed: {e}")
    if not url:
        raise HTTPException(status_code=404, detail=f"'{req.title}' not found in Plex library")
    return {"url": url}


@router.get("/history")
def download_history(
    limit: int = 100,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db:
        return reg.db.get_download_history(limit=limit)
    return []


@router.get("/jd-test")
def jd_test(reg: ServiceRegistry = Depends(get_registry)):
    """Quick MyJDownloader connection check for the UI status indicators."""
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    if not reg.config.get("jd_enabled"):
        return {"connected": False, "error": "JDownloader integration is disabled"}
    return dl.test_jd_connection()


@router.get("/jd-state")
def jd_state(reg: ServiceRegistry = Depends(get_registry)):
    """Lightweight connectivity + download-queue run-state check.

    Cheap enough to poll frequently (unlike /jd-status, which returns the
    full linkgrabber/downloads link lists).
    """
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    if not reg.config.get("jd_enabled") or reg.config.get("jd_method") != "api":
        return {"connected": False, "error": "Enable JDownloader with the MyJDownloader API method in Settings.", "state": "unknown"}
    return dl.get_jd_state()


@router.get("/jd-status")
def jd_status(reg: ServiceRegistry = Depends(get_registry)):
    """Live JDownloader LinkGrabber + Downloads list (online/offline/broken)."""
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    if not reg.config.get("jd_enabled") or reg.config.get("jd_method") != "api":
        return {
            "connected": False,
            "error": "Enable JDownloader with the MyJDownloader API method in Settings.",
            "links": [], "online": 0, "offline": 0, "total": 0, "state": "unknown",
        }
    return dl.get_jd_status()


class JdControlRequest(BaseModel):
    action: str  # start | stop | pause | resume


@router.post("/jd-control")
def jd_control(req: JdControlRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Start / stop / pause / resume JDownloader's global download queue."""
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    if not reg.config.get("jd_enabled") or reg.config.get("jd_method") != "api":
        raise HTTPException(
            status_code=400,
            detail="Enable JDownloader with the MyJDownloader API method in Settings.",
        )
    if req.action not in ("start", "stop", "pause", "resume"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")
    result = dl.jd_control(req.action)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "JDownloader control failed"))
    # Push the new run-state to all clients immediately.
    ws_manager.broadcast_sync({"type": "download:state", "data": {"state": result.get("state")}})
    return result


@router.get("/results")
def download_results(limit: int = 200, reg: ServiceRegistry = Depends(get_registry)):
    """Persisted per-item download + extraction outcomes (polled from JDownloader)."""
    if reg.db:
        return reg.db.get_download_results(limit=limit)
    return []


@router.delete("/results")
def clear_download_results(reg: ServiceRegistry = Depends(get_registry)):
    """Clear the tracked download/extraction results list."""
    if reg.db:
        reg.db.clear_download_results()
    return {"status": "cleared"}
