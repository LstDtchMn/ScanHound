"""Download endpoints: send to JDownloader, history, open in Plex."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.api.public_errors import capture_public_exception
from backend.api.ws import ws_manager
from backend.download_queue import (
    DownloadQueueConflict,
    DownloadQueueError,
    DownloadQueueItemClaimed,
    DownloadQueueUnavailable,
)
from backend.download_service import _source_page_kind
from backend.source_health import record_scrape_outcome
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks
from backend.download_outcome import (
    deferred_result,
    is_source_wide_denial,
    notification_for_result,
    public_download_result,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/download", tags=["downloads"])


def _persist_grab_annotations(reg: ServiceRegistry) -> None:
    """Re-annotate the cached results after a grab so a just-grabbed release's
    siblings keep their 'grabbed similar' note across a reload. The live UI is
    updated optimistically over the WebSocket; this persists it to the cache
    without waiting for the next (3-hourly) background scan. Cheap (no scraping),
    best-effort."""
    try:
        scanner = getattr(reg, "scanner", None)
        if scanner is not None:
            scanner.rematch_cache()
    except Exception:
        logger.debug("post-grab cache re-match skipped", exc_info=True)


class DownloadRequest(BaseModel):
    url: str
    title: str = "Untitled"
    season: Optional[int] = None
    year: Optional[int] = None
    resolution: str = ""
    size: str = ""
    hdr: str = ""
    dovi: bool = False
    service_type: str = "Rapidgator"


class BatchExecution(BaseModel):
    mode: str = "staggered"
    interval_minutes: Optional[int] = None
    auto_resume_after_cooldown: Optional[bool] = None


class BatchDownloadRequest(BaseModel):
    items: List[DownloadRequest] = Field(min_length=1)
    execution: Optional[BatchExecution] = None


class RetryReadyRequest(BaseModel):
    interval_minutes: int = 10


class ResumeBatchRequest(BaseModel):
    interval_minutes: int = 10


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


def _run_grab(dl, reg: ServiceRegistry, req: "DownloadRequest", force: bool = False) -> None:
    """Execute one grab and report its outcome over WS — the shared body used
    by BOTH the existing POST /download route and the pipeline tracker's
    regrab/grab-alternative actions. `force=True` (pipeline-only) bypasses
    download_item's two dedup gates."""
    try:
        def _on_progress(event: str, data: dict):
            ws_manager.broadcast_sync({"type": event, "data": data})

        result = dl.download_item(
            url=req.url, title=req.title, season=req.season,
            resolution=req.resolution, size=req.size,
            service_type=req.service_type, year=req.year,
            hdr=req.hdr, dovi=req.dovi,
            progress_callback=_on_progress,
            force=force,
        )
        outcome = public_download_result(result, title=req.title, url=req.url)
        ws_manager.broadcast_sync({"type": "download:result", "data": outcome})

        # Report the *honest* outcome: only a JDownloader hand-off counts as
        # a delivery. A failed scrape or a clipboard/browser fallback must
        # not look like a successful "Download".
        success = bool((result or {}).get("success"))
        method = (result or {}).get("method", "")
        message = (result or {}).get("message", "") or f"Sent: {req.title}"
        if not success:
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": notification_for_result(outcome, title=req.title),
            })
            if (
                reg.download_queue is not None
                and outcome.get("reason_code") in {
                    "interactive_challenge",
                    "source_temporarily_blocked",
                }
            ):
                reg.download_queue.enqueue_retry(req, outcome)
        elif method in ("duplicate", "duplicate_similar"):
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Already grabbed", "body": message, "priority": "normal"},
            })
        elif method == "jdownloader":
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download", "body": message, "priority": "normal"},
            })
            # Persist the 'grabbed similar' note onto this title's siblings.
            _persist_grab_annotations(reg)
        else:
            # clipboard/browser — succeeded, but nothing reached JDownloader.
            ws_manager.broadcast_sync({
                "type": "notification",
                "data": {"title": "Download", "body": f"{message} (not sent to JDownloader — method: {method})", "priority": "warning"},
            })
    except Exception as e:
        public = capture_public_exception(
            logger, e, code="download_failed",
            message="The download could not be completed.",
            context=f"Download failed for {req.title}",
        )
        try:
            dl.save_to_history(req.url, req.title, req.season, req.resolution, req.size, status="failed", hdr=req.hdr, dovi=req.dovi)
        except Exception:
            pass
        ws_manager.broadcast_sync({
            "type": "notification",
            "data": public.notification_data(title="Download Failed"),
        })


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

    background_tasks.add_task(_run_grab, dl, reg, req, False)
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

    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")

    execution = req.execution
    configured_interval = int(reg.config.get("download_batch_interval_minutes", 10) or 0)
    interval = (
        configured_interval
        if execution is None or execution.interval_minutes is None
        else int(execution.interval_minutes)
    )
    mode = (
        execution.mode
        if execution is not None
        else ("immediate" if interval == 0 else "staggered")
    )
    auto_resume = (
        bool(reg.config.get("download_queue_auto_resume_after_cooldown", False))
        if execution is None or execution.auto_resume_after_cooldown is None
        else bool(execution.auto_resume_after_cooldown)
    )
    try:
        batch = queue.schedule_batch(
            [item.model_dump() for item in req.items],
            interval_minutes=interval,
            mode=mode,
            auto_resume_after_cooldown=auto_resume,
        )
    except DownloadQueueConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except DownloadQueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except DownloadQueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "scheduled",
        "count": batch.get("total_items", len(req.items)),
        "batch_uuid": batch.get("batch_uuid"),
        "mode": batch.get("mode"),
        "interval_minutes": int(batch.get("interval_seconds") or 0) // 60,
        "items": batch.get("items", []),
    }


@router.get("/browser-status")
def browser_status(reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    return dl.get_browser_status()


@router.get("/retries")
def list_download_retries(
    limit: int = 250,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    items = queue.list_retries(limit=limit)
    return {"items": items, "count": len(items), "status": queue.status()}


@router.post("/retries/retry-ready")
def retry_ready_downloads(
    req: RetryReadyRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    try:
        return queue.retry_ready(req.interval_minutes)
    except Exception as exc:
        detail = exc.detail() if hasattr(exc, "detail") else str(exc)
        raise HTTPException(status_code=409, detail=detail)


@router.post("/retries/{item_uuid}/retry")
def retry_download_item(
    item_uuid: str,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    try:
        return queue.retry_item(item_uuid)
    except Exception as exc:
        detail = exc.detail() if hasattr(exc, "detail") else str(exc)
        raise HTTPException(status_code=409, detail=detail)


@router.delete("/retries/{item_uuid}")
def remove_download_retry(
    item_uuid: str,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    try:
        ok = queue.cancel_item(item_uuid)
    except DownloadQueueItemClaimed as exc:
        raise HTTPException(status_code=409, detail=exc.detail())
    return {"ok": ok, "item_uuid": item_uuid}


@router.get("/batches")
def list_download_batches(
    limit: int = 100,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    items = queue.list_batches(limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/batches/{batch_uuid}/resume")
def resume_download_batch(
    batch_uuid: str,
    req: ResumeBatchRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    try:
        return queue.resume_batch(batch_uuid, req.interval_minutes)
    except Exception as exc:
        detail = exc.detail() if hasattr(exc, "detail") else str(exc)
        raise HTTPException(status_code=409, detail=detail)


@router.delete("/batches/{batch_uuid}")
def cancel_download_batch(
    batch_uuid: str,
    reg: ServiceRegistry = Depends(get_registry),
):
    queue = reg.download_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="Download queue not available")
    try:
        ok = queue.cancel_batch(batch_uuid)
    except DownloadQueueItemClaimed as exc:
        raise HTTPException(status_code=409, detail=exc.detail())
    return {"ok": ok, "batch_uuid": batch_uuid}


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
        public = capture_public_exception(
            logger, e, code="scrape_failed",
            message="Links could not be retrieved.",
            context="Scrape failed",
        )
        raise HTTPException(status_code=502, detail=public.as_detail())
    diagnostic = getattr(links, "diagnostic", None)
    if _source_page_kind(req.url) == "hdencode":
        record_scrape_outcome(reg.db, "hdencode", links)
    if links and req.title and reg.db:
        try:
            reg.db.record_scraped_links(links, req.title, req.resolution, req.url)
        except Exception:
            pass
    response = {"links": list(links), "count": len(links)}
    if diagnostic is not None:
        response.update(diagnostic.to_dict())
    return response


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
        failures: List[Dict[str, Any]] = []
        for i, it in enumerate(items):
            ws_manager.broadcast_sync({
                "type": "download:scrape_progress",
                "data": {"completed": i, "total": total, "current": it.url},
            })
            diagnostic = None
            try:
                links = dl.scrape_links(it.url, it.service_type)
                diagnostic = getattr(links, "diagnostic", None)
                if _source_page_kind(it.url) == "hdencode":
                    record_scrape_outcome(reg.db, "hdencode", links)
            except Exception as exc:
                logger.exception("Batch scrape failed for %s", it.url)
                diagnostic = ScrapeDiagnostic(
                    ScrapeCode.SCRAPE_EXCEPTION,
                    retryable=True,
                    affects_source_health=False,
                    signals=(type(exc).__name__,),
                    detail=f"Batch link scrape failed: {exc}",
                )
                links = ScrapedLinks(diagnostic=diagnostic)
            if not links and diagnostic is not None:
                failures.append({"url": it.url, **diagnostic.to_dict()})
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
            reason_codes = sorted({f["reason_code"] for f in failures})
            suffix = f" ({', '.join(reason_codes)})" if reason_codes else ""
            body = f"No links found across {total} item(s){suffix}"
        elif copied:
            body = f"Copied {len(all_links)} link(s) from {total} item(s) — JDownloader should grab them"
        else:
            body = f"Found {len(all_links)} link(s) but clipboard copy failed"
        ws_manager.broadcast_sync({
            "type": "notification",
            "data": {
                "title": "Copy Links",
                "body": body,
                "priority": "normal" if all_links else "high",
                "reason_codes": sorted({f["reason_code"] for f in failures}),
            },
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
        public = capture_public_exception(
            logger, e, code="plex_lookup_failed",
            message="The Plex item could not be opened.",
            context=f"Failed to open in Plex: {req.title}",
        )
        raise HTTPException(status_code=502, detail=public.as_detail())
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


class RemoveResultRequest(BaseModel):
    id: int


@router.post("/results/remove")
def remove_download_result(req: RemoveResultRequest, reg: ServiceRegistry = Depends(get_registry)):
    """Remove a single tracked download package (JDownloader + DB row)."""
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    return dl.remove_package(req.id)
