"""HDEncode RSS operations, evidence, and safe manual actions."""
from __future__ import annotations

import json
import logging
import threading
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.hdencode_candidate_service import HDEncodeCandidateService
from backend.hdencode_action_service import (
    HDEncodeActionError,
    HDEncodeActionService,
)
from backend.api.public_errors import capture_public_exception


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rss", tags=["rss"])


def _join_rss_hydration_threads(reg):
    threads = list(getattr(reg, "_rss_hydration_threads", set()))
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=2.0)




def _join_rss_action_threads(reg):
    threads = list(getattr(reg, "_rss_action_threads", set()))
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=2.0)


def _start_tracked_action_thread(reg, target):
    if not hasattr(reg, "_rss_action_threads"):
        reg._rss_action_threads = set()
        if reg.backend is not None:
            reg.backend.add_shutdown_hook(
                lambda: _join_rss_action_threads(reg)
            )
    holder = {}

    def wrapped():
        try:
            target()
        finally:
            reg._rss_action_threads.discard(holder["thread"])

    thread = threading.Thread(
        target=wrapped,
        name="rss-candidate-action",
        daemon=True,
    )
    holder["thread"] = thread
    reg._rss_action_threads.add(thread)
    thread.start()
    return thread

def _start_tracked_hydration_thread(reg, target):
    if not hasattr(reg, "_rss_hydration_threads"):
        reg._rss_hydration_threads = set()
        if reg.backend is not None:
            reg.backend.add_shutdown_hook(
                lambda: _join_rss_hydration_threads(reg)
            )
    holder = {}
    def wrapped():
        try:
            target()
        finally:
            reg._rss_hydration_threads.discard(holder["thread"])
    thread = threading.Thread(
        target=wrapped,
        name="rss-explicit-hydration",
        daemon=True,
    )
    holder["thread"] = thread
    reg._rss_hydration_threads.add(thread)
    thread.start()
    return thread


class ModeRequest(BaseModel):
    mode: str


class CandidateRequest(BaseModel):
    canonical_url: str


class ActionRequest(BaseModel):
    canonical_url: str
    action_kind: Literal["retrieve_links", "grab"]
    service_type: Literal[
        "Rapidgator", "Nitroflare", "1fichier", "ddownload"
    ] = "Rapidgator"
    destination: str = ""
    idempotency_key: Optional[str] = None


_CANDIDATE_FIELDS = (
    "canonical_url",
    "title",
    "pub_date",
    "media_type",
    "clean_title",
    "title_year",
    "description_year",
    "season",
    "episode",
    "episode_end",
    "resolution",
    "size_text",
    "size_gb",
    "dv_evidence",
    "hdr_evidence",
    "hevc_evidence",
    "hdr_formats",
    "categories",
    "identity_state",
    "relevance_state",
    "detail_reason",
    "hydration_state",
    "action_state",
    "description_complete",
    "imdb_id",
    "tmdb_id",
    "discovery_source",
)


def _candidate_row(row: dict) -> dict:
    item = {key: row.get(key) for key in _CANDIDATE_FIELDS}
    for key in ("hdr_formats", "categories"):
        value = item.get(key)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                item[key] = parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                item[key] = []
        elif not isinstance(value, list):
            item[key] = []
    item["year_conflict"] = bool(
        item.get("title_year")
        and item.get("description_year")
        and item["title_year"] != item["description_year"]
    )
    item["evidence_incomplete"] = any(
        item.get(key) == "unknown"
        for key in ("dv_evidence", "hdr_evidence", "hevc_evidence")
    )
    return item


def _readiness(reg: ServiceRegistry) -> dict:
    config = reg.config or {}
    return reg.db.get_hdencode_rss_readiness(
        min_cycles=config.get("hdencode_rss_shadow_min_cycles", 20),
        min_days=config.get("hdencode_rss_shadow_min_days", 7),
    )


@router.get("/status")
def rss_status(reg: ServiceRegistry = Depends(get_registry)):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    background = reg.background_scanner
    last_run = background.last_run if background else None
    counts = reg.db.get_hdencode_rss_dashboard_counts()
    shadow = reg.db.get_hdencode_shadow_summary()
    return {
        "mode": (reg.config or {}).get(
            "hdencode_discovery_mode",
            "listing",
        ),
        "enabled": (reg.config or {}).get("hdencode_enabled", True) is True,
        "feeds": reg.db.list_hdencode_feed_states(),
        "last_cycle": (last_run or {}).get("rss"),
        "readiness": _readiness(reg),
        "candidate_counts": counts["candidate_counts"],
        "hydration_counts": counts["hydration_counts"],
        "unknown_counts": counts["unknown_counts"],
        "shadow": shadow,
        "coordinator": (
            (last_run or {}).get("rss", {}).get("coordinator")
            or {}
        ),
        "safe_defaults": {
            "listing_fallback": (
                (reg.config or {}).get(
                    "hdencode_rss_listing_fallback_enabled"
                )
                is True
            ),
            "rss_auto_grab": (
                (reg.config or {}).get("hdencode_rss_auto_grab_enabled")
                is True
            ),
            "hydration_limit": (reg.config or {}).get(
                "hdencode_rss_hydration_limit",
                10,
            ),
        },
    }


@router.get("/candidates")
def rss_candidates(
    state: Optional[str] = None,
    hydration: Optional[str] = None,
    limit: int = 250,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    rows = reg.db.list_hdencode_candidates(
        relevance_state=state,
        hydration_state=hydration,
        limit=max(1, min(limit, 1000)),
    )
    return {
        "items": [_candidate_row(row) for row in rows],
        "count": len(rows),
    }


@router.get("/hydration")
def rss_hydration(
    limit: int = 250,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    rows = reg.db.list_hdencode_hydration_queue(
        limit=max(1, min(limit, 1000))
    )
    return {
        "items": [
            {
                key: row.get(key)
                for key in (
                    "canonical_url",
                    "reason",
                    "priority",
                    "state",
                    "attempts",
                    "queued_at",
                    "claimed_at",
                    "completed_at",
                    "last_error_code",
                    "title",
                    "pub_date",
                    "media_type",
                    "resolution",
                    "dv_evidence",
                )
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.post("/mode")
def set_rss_mode(
    request: ModeRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    allowed = {"listing", "rss_shadow", "rss_primary"}
    if request.mode not in allowed:
        raise HTTPException(status_code=422, detail="Invalid RSS mode")
    if reg.config is None or reg.backend is None or reg.db is None:
        raise HTTPException(status_code=503, detail="Configuration unavailable")
    if request.mode == "rss_primary" and not _readiness(reg)["ready"]:
        raise HTTPException(
            status_code=409,
            detail="RSS primary requires completed shadow validation",
        )
    reg.config["hdencode_discovery_mode"] = request.mode
    reg.backend.save_config()
    return {"mode": request.mode}


@router.post("/hydrate")
def hydrate_candidate(
    request: CandidateRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if (reg.config or {}).get("hdencode_enabled", True) is not True:
        raise HTTPException(status_code=409, detail="HDEncode is disabled")
    candidate = reg.db.get_hdencode_candidate(request.canonical_url)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.get("hydration_state") == "completed":
        return {
            "status": "already_hydrated",
            "canonical_url": request.canonical_url,
        }

    reg.db.enqueue_hdencode_hydration(
        request.canonical_url,
        reason="explicit_detail",
        priority=90,
    )
    detail_scraper = getattr(
        getattr(reg.scanner, "scrapers", None),
        "_detail",
        None,
    )
    if detail_scraper is None:
        return {
            "status": "queued",
            "canonical_url": request.canonical_url,
        }

    generation = reg.lifespan_generation
    service = HDEncodeCandidateService(reg.config or {}, reg.db)

    def run_hydration():
        try:
            service.hydrate_pending(
                detail_scraper,
                limit=1,
                stop_requested=lambda: not reg.owns_lifespan(generation),
            )
        except Exception:
            logger.exception("Explicit RSS hydration worker failed")

    _start_tracked_hydration_thread(reg, run_hydration)
    return {
        "status": "started",
        "canonical_url": request.canonical_url,
    }


@router.post("/retry")
def retry_candidate(
    request: CandidateRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if (reg.config or {}).get("hdencode_enabled", True) is not True:
        raise HTTPException(status_code=409, detail="HDEncode is disabled")
    if reg.db.get_hdencode_candidate(request.canonical_url) is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    reg.db.requeue_hdencode_hydration(
        request.canonical_url,
        reason="explicit_detail",
        priority=90,
    )
    return {
        "status": "queued",
        "canonical_url": request.canonical_url,
    }


@router.get("/actions")
def rss_actions(
    state: Optional[str] = None,
    limit: int = 250,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    rows = reg.db.list_hdencode_actions(
        state=state,
        limit=max(1, min(limit, 1000)),
    )
    items = []
    for row in rows:
        item = {
            key: row.get(key)
            for key in (
                "action_uuid", "canonical_url", "action_kind",
                "requested_by", "service_type", "priority", "state",
                "package_name", "link_count", "attempts", "queued_at",
                "claimed_at", "links_ready_at", "submitted_at",
                "completed_at", "cancelled_at", "updated_at",
                "last_error_code", "correlation_id", "title",
                "clean_title", "resolution", "dv_evidence",
                "hdr_evidence", "discovery_source",
            )
        }
        item["links"] = []
        if row.get("state") == "links_ready":
            try:
                parsed = json.loads(row.get("links_json") or "[]")
                if isinstance(parsed, list):
                    item["links"] = [str(value) for value in parsed]
            except (TypeError, ValueError):
                pass
        items.append(item)
    return {"items": items, "count": len(items)}


def _action_worker(reg, service, action_uuid, generation):
    try:
        service.run_action(
            action_uuid,
            owns_lifespan=lambda: reg.owns_lifespan(generation),
        )
    except HDEncodeActionError as exc:
        reg.db.fail_hdencode_action(
            action_uuid, error_code=exc.code
        )
    except Exception as exc:
        public = capture_public_exception(
            logger, exc, code="rss_action_failed",
            message="The RSS action could not be completed.",
            context="Tracked RSS action worker failed",
        )
        reg.db.fail_hdencode_action(
            action_uuid, error_code=public.code,
            correlation_id=public.correlation_id,
        )


@router.post("/actions")
def start_rss_action(
    request: ActionRequest,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None or reg.download is None:
        raise HTTPException(status_code=503, detail="Action services unavailable")
    generation = reg.lifespan_generation
    service = HDEncodeActionService(reg.config or {}, reg.db, reg.download)
    try:
        action = service.queue_action(
            request.canonical_url,
            action_kind=request.action_kind,
            requested_by="explicit",
            service_type=request.service_type,
            destination=request.destination,
            idempotency_key=request.idempotency_key,
            lifespan_generation=generation,
        )
    except HDEncodeActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail())
    except Exception as exc:
        public = capture_public_exception(
            logger, exc, code="rss_action_queue_failed",
            message="The RSS action could not be queued.",
            context="RSS action queue failed",
        )
        raise HTTPException(status_code=500, detail=public.as_detail())

    if action.get("created"):
        _start_tracked_action_thread(
            reg,
            lambda: _action_worker(
                reg, service, action["action_uuid"], generation
            ),
        )

    return {
        "status": action.get("state"),
        "action_uuid": action.get("action_uuid"),
        "created": bool(action.get("created")),
        "idempotent": bool(action.get("idempotent")),
    }


@router.post("/actions/{action_uuid}/cancel")
def cancel_rss_action(
    action_uuid: str,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    action = reg.db.request_cancel_hdencode_action(action_uuid)
    if action is None:
        raise HTTPException(status_code=404, detail="RSS action not found")
    return {"action_uuid": action_uuid, "status": action.get("state")}


@router.post("/actions/{action_uuid}/retry")
def retry_rss_action(
    action_uuid: str,
    reg: ServiceRegistry = Depends(get_registry),
):
    if reg.db is None or reg.download is None:
        raise HTTPException(status_code=503, detail="Action services unavailable")
    if (reg.config or {}).get("hdencode_enabled", True) is not True:
        raise HTTPException(
            status_code=409,
            detail={"code": "source_disabled", "message": "HDEncode is disabled."},
        )
    action = reg.db.retry_hdencode_action(action_uuid)
    if action is None:
        raise HTTPException(status_code=404, detail="RSS action not found")
    if action.get("state") != "queued":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "action_not_retryable",
                "message": "This action cannot be retried safely.",
            },
        )
    generation = reg.lifespan_generation
    service = HDEncodeActionService(reg.config or {}, reg.db, reg.download)
    _start_tracked_action_thread(
        reg,
        lambda: _action_worker(reg, service, action_uuid, generation),
    )
    return {"action_uuid": action_uuid, "status": "queued"}


def _counts(rows, key):
    counts = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts
