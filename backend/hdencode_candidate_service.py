"""Conservative candidate classification and selective detail hydration."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import logging
import re
from typing import Callable, Optional

from backend.candidate_evidence import (
    CandidateDecisionEngine,
    CandidateEvidence,
)
from backend.hdencode_coordinator import get_hdencode_coordinator


logger = logging.getLogger(__name__)


_HYDRATION_PRIORITY = {
    "explicit_link": 100,
    "explicit_detail": 90,
    "auto_grab_candidate": 80,
    "identity_ambiguous": 75,
    "identity_unresolved": 70,
    "description_truncated": 65,
    "multi_episode_details": 64,
    "dolby_vision_unknown": 60,
    "size_upgrade_with_unknown_dv": 55,
    "insufficient_evidence": 50,
}


class HDEncodeCandidateService:
    """Classify feed evidence without guessing or triggering downloads."""

    def __init__(self, config, db):
        self.config = config if isinstance(config, dict) else {}
        self.db = db
        self.engine = CandidateDecisionEngine()
        try:
            self.db.recover_hdencode_hydration_queue()
        except Exception:
            logger.exception("Failed to recover stale RSS hydration claims")

    def classify_pending(
        self,
        *,
        limit: int = 500,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> dict:
        rows = self.db.list_hdencode_candidates(
            relevance_state="unclassified",
            limit=limit,
        )
        counts = {}
        processed = 0
        for row in rows:
            if _cancelled(stop_requested):
                counts["cancelled"] = counts.get("cancelled", 0) + 1
                break
            outcome = self.classify_candidate(
                row,
                stop_requested=stop_requested,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
            processed += 1
        return {"processed": processed, "states": counts}

    def classify_candidate(
        self,
        row: dict,
        *,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> str:
        identity_state = str(row.get("identity_state") or "unknown")
        evidence = CandidateEvidence.from_mapping({
            "resolution": row.get("resolution"),
            "size_gb": row.get("size_gb"),
            "dv": row.get("dv_evidence"),
            "hdr": row.get("hdr_evidence"),
            "hevc": row.get("hevc_evidence"),
            "hdr_formats": _json_list(row.get("hdr_formats")),
            "title_year": row.get("title_year"),
            "description_year": row.get("description_year"),
            "description_complete": bool(row.get("description_complete")),
            "identity_confidence": identity_state,
        })
        context = self.db.get_hdencode_candidate_context(
            canonical_url=row["canonical_url"],
            clean_title=row.get("clean_title"),
            media_type=row.get("media_type"),
            years=evidence.observed_years,
            season=row.get("season"),
            imdb_id=row.get("imdb_id"),
            tmdb_id=row.get("tmdb_id"),
        )

        resolved_identity = identity_state
        if context.get("exact_url_downloaded"):
            decision = self.engine.decide(
                evidence,
                existing=None,
                exact_url_downloaded=True,
            )
        else:
            matches = context.get("plex_matches") or []
            if len(matches) == 1:
                resolved_identity = "exact"
                decision = self.engine.decide(
                    evidence,
                    existing=matches[0],
                )
            elif len(matches) > 1:
                resolved_identity = "ambiguous"
                decision = _detail_decision("identity_ambiguous")
            elif identity_state in {"exact", "high", "hydrated"}:
                resolved_identity = "exact"
                exact_evidence = CandidateEvidence.from_mapping({
                    **row,
                    "dv": row.get("dv_evidence"),
                    "hdr": row.get("hdr_evidence"),
                    "hevc": row.get("hevc_evidence"),
                    "hdr_formats": _json_list(row.get("hdr_formats")),
                    "identity_confidence": "exact",
                })
                decision = self.engine.decide(
                    exact_evidence,
                    existing=None,
                )
            else:
                decision = _detail_decision("identity_unresolved")

        if _cancelled(stop_requested):
            return "cancelled"

        self.db.update_hdencode_candidate_state(
            row["canonical_url"],
            identity_state=resolved_identity,
            relevance_state=decision.state,
            detail_reason=(
                decision.reason if decision.requires_detail else ""
            ),
        )
        if decision.requires_detail:
            self.db.enqueue_hdencode_hydration(
                row["canonical_url"],
                reason=decision.reason,
                priority=_HYDRATION_PRIORITY.get(decision.reason, 40),
            )
        else:
            self.db.resolve_hdencode_hydration(
                row["canonical_url"],
                reason="classification_resolved",
            )
        return decision.state

    def hydrate_pending(
        self,
        detail_scraper,
        *,
        limit: Optional[int] = None,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> dict:
        if limit is None:
            try:
                limit = int(self.config.get("hdencode_rss_hydration_limit", 10))
            except (TypeError, ValueError):
                limit = 10
        limit = max(0, min(int(limit), 50))
        claimed = self.db.claim_hdencode_hydration(limit=limit)
        completed = failed = cancelled = 0

        for row in claimed:
            if _cancelled(stop_requested):
                self.db.release_hdencode_hydration(
                    row["canonical_url"],
                    reason="cancelled",
                )
                cancelled += 1
                continue
            try:
                with get_hdencode_coordinator().prioritize(
                    int(row.get("priority") or 40)
                ):
                    result = detail_scraper.scrape_details(
                        row["canonical_url"],
                        headers={},
                        scraper=None,
                        stop_requested=stop_requested,
                    )
            except Exception as exc:
                logger.exception(
                    "RSS detail hydration failed for %s",
                    row["canonical_url"],
                )
                if _cancelled(stop_requested):
                    self.db.release_hdencode_hydration(
                        row["canonical_url"],
                        reason="cancelled",
                    )
                    cancelled += 1
                else:
                    self.db.fail_hdencode_hydration(
                        row["canonical_url"],
                        error_code=type(exc).__name__,
                    )
                    failed += 1
                continue

            if _cancelled(stop_requested):
                self.db.release_hdencode_hydration(
                    row["canonical_url"],
                    reason="cancelled",
                )
                cancelled += 1
                continue

            if result is None:
                self.db.fail_hdencode_hydration(
                    row["canonical_url"],
                    error_code="no_detail_result",
                )
                failed += 1
                continue

            payload = _result_dict(result)
            self.db.complete_hdencode_hydration(
                row["canonical_url"],
                payload=payload,
                candidate_updates=_candidate_updates(payload),
            )
            completed += 1

        return {
            "claimed": len(claimed),
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
        }


def _detail_decision(reason):
    from backend.candidate_evidence import CandidateDecision

    return CandidateDecision(
        state="detail_required",
        reason=reason,
        requires_detail=True,
        safe_to_auto_act=False,
    )


def _cancelled(observer):
    if observer is None:
        return False
    try:
        return bool(observer())
    except Exception:
        return True


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _result_dict(result):
    if is_dataclass(result):
        return asdict(result)
    if isinstance(result, dict):
        return dict(result)
    if hasattr(result, "__dict__"):
        return dict(vars(result))
    raise TypeError("Unsupported detail result")


def _candidate_updates(payload):
    """Return only authoritative hydrated fields; absence never means false."""
    updates = {}
    def put(name, value):
        if value is not None and value != "": updates[name] = value

    put("clean_title", str(payload.get("display_title") or "").strip() or None)
    put("title_year", _int_or_none(payload.get("year")))
    put("season", _int_or_none(payload.get("season")))
    put("episode", _int_or_none(payload.get("episode_number")))
    put("resolution", str(payload.get("res") or "").strip() or None)
    put("size_text", str(payload.get("size") or "").strip() or None)
    put("size_gb", _size_gb(payload.get("size")))
    imdb_id = str(payload.get("imdb_id") or "").strip()
    if imdb_id.startswith("tt") and imdb_id[2:].isdigit(): updates["imdb_id"] = imdb_id

    if "dovi" in payload and payload.get("dovi") is not None:
        updates["dv_evidence"] = "asserted" if payload.get("dovi") is True else "negated"

    if "hdr" in payload and payload.get("hdr") is not None:
        hdr_value=str(payload.get("hdr") or "").strip(); hdr_upper=hdr_value.upper()
        if hdr_upper not in {"", "?", "UNKNOWN"}:
            if hdr_upper in {"SDR", "NONE", "NO"}:
                updates["hdr_evidence"]="negated"; updates["hdr_formats"]=[]
            else:
                formats=[]
                if "HDR10+" in hdr_upper or "HDR10P" in hdr_upper: formats.append("HDR10+")
                elif "HDR10" in hdr_upper: formats.append("HDR10")
                if "HLG" in hdr_upper: formats.append("HLG")
                if not formats: formats.append("HDR")
                updates["hdr_evidence"]="asserted"; updates["hdr_formats"]=formats

    if payload.get("url") and payload.get("display_title"):
        updates["description_complete"] = True
        updates["identity_state"] = "hydrated"
    return updates


def _size_gb(value):
    text = str(value or "").strip().upper().replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)(TIB|TB|GIB|GB|MIB|MB)", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit in {"TB", "TIB"}:
        return amount * 1024
    if unit in {"MB", "MIB"}:
        return amount / 1024
    return amount


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
