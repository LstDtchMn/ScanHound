"""HDEncode RSS operations, evidence, and safe manual actions."""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import ServiceRegistry, get_registry
from backend.hdencode_candidate_service import HDEncodeCandidateService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rss", tags=["rss"])


class ModeRequest(BaseModel):
    mode: str


class CandidateRequest(BaseModel):
    canonical_url: str


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
    candidates = reg.db.list_hdencode_candidates(limit=5000)
    queue = reg.db.list_hdencode_hydration_queue(limit=5000)
    return {
        "mode": (reg.config or {}).get(
            "hdencode_discovery_mode",
            "listing",
        ),
        "enabled": (reg.config or {}).get("hdencode_enabled", True) is True,
        "feeds": reg.db.list_hdencode_feed_states(),
        "last_cycle": (last_run or {}).get("rss"),
        "readiness": _readiness(reg),
        "candidate_counts": _counts(candidates, "relevance_state"),
        "hydration_counts": _counts(queue, "state"),
        "unknown_counts": {
            "dv": sum(
                row.get("dv_evidence") == "unknown"
                for row in candidates
            ),
            "hdr": sum(
                row.get("hdr_evidence") == "unknown"
                for row in candidates
            ),
            "identity": sum(
                row.get("identity_state") in {
                    None, "", "unknown", "ambiguous"
                }
                for row in candidates
            ),
            "year_conflict": sum(
                bool(
                    row.get("title_year")
                    and row.get("description_year")
                    and row["title_year"] != row["description_year"]
                )
                for row in candidates
            ),
        },
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

    threading.Thread(
        target=run_hydration,
        name="rss-explicit-hydration",
        daemon=True,
    ).start()
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
    if reg.db.get_hdencode_candidate(request.canonical_url) is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    reg.db.enqueue_hdencode_hydration(
        request.canonical_url,
        reason="explicit_detail",
        priority=90,
    )
    reg.db.update_hdencode_candidate_state(
        request.canonical_url,
        hydration_state="queued",
        detail_reason="explicit_detail",
    )
    return {
        "status": "queued",
        "canonical_url": request.canonical_url,
    }


def _counts(rows, key):
    counts = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts
