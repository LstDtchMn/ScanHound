"""Persistent, coordinator-controlled RSS candidate actions."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Callable, Optional

from backend.download_service import compute_package_name
from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficDenied,
    get_hdencode_coordinator,
)

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS = {"retrieve_links", "grab"}
_ALLOWED_SERVICES = {"Rapidgator", "Nitroflare", "1fichier", "ddownload"}


class HDEncodeActionError(RuntimeError):
    """Closed business error for an RSS action request."""

    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message}


class HDEncodeActionService:
    """Queue and execute explicit or policy-approved candidate actions.

    Discovery, classification, and hydration never call this service on their
    own. The only callers are explicit API actions and the separately gated
    auto-action loop. Every network operation inherits a coordinator priority.
    """

    def __init__(self, config, db, download_service):
        self.config = config if isinstance(config, dict) else {}
        self.db = db
        self.download = download_service
        self.coordinator = get_hdencode_coordinator()
        try:
            self.db.recover_hdencode_actions()
        except Exception:
            logger.exception("Failed to recover interrupted HDEncode actions")

    def queue_action(
        self,
        canonical_url: str,
        *,
        action_kind: str,
        requested_by: str,
        service_type: str = "Rapidgator",
        destination: str = "",
        idempotency_key: Optional[str] = None,
        lifespan_generation: Optional[int] = None,
    ) -> dict:
        if self.config.get("hdencode_enabled", True) is not True:
            raise HDEncodeActionError("source_disabled", "HDEncode is disabled.")
        if self.download is None:
            raise HDEncodeActionError(
                "download_service_unavailable",
                "The download service is unavailable.",
                status_code=503,
            )
        if action_kind not in _ALLOWED_ACTIONS:
            raise HDEncodeActionError(
                "invalid_action",
                "The requested action is not supported.",
                status_code=422,
            )
        if service_type not in _ALLOWED_SERVICES:
            raise HDEncodeActionError(
                "invalid_service",
                "The requested file host is not supported.",
                status_code=422,
            )
        if requested_by not in {"explicit", "auto"}:
            raise HDEncodeActionError(
                "invalid_requester",
                "The action requester is invalid.",
                status_code=422,
            )

        candidate = self.db.get_hdencode_candidate(canonical_url)
        if candidate is None:
            raise HDEncodeActionError(
                "candidate_not_found",
                "The RSS candidate was not found.",
                status_code=404,
            )

        context = self.db.get_hdencode_candidate_context(
            canonical_url=canonical_url,
            clean_title=candidate.get("clean_title"),
            media_type=candidate.get("media_type"),
            years=[
                year
                for year in (
                    candidate.get("description_year"),
                    candidate.get("title_year"),
                )
                if year is not None
            ],
            season=candidate.get("season"),
            imdb_id=candidate.get("imdb_id"),
            tmdb_id=candidate.get("tmdb_id"),
        )
        if context.get("exact_url_downloaded"):
            raise HDEncodeActionError(
                "already_downloaded",
                "This source post has already been submitted.",
            )

        if requested_by == "auto":
            self._validate_auto_action(candidate, action_kind)
            priority = 80
        else:
            priority = 100

        evidence = self._authorization_evidence(candidate, requested_by)
        action_uuid = str(uuid.uuid4())
        idempotency_key = (
            str(idempotency_key or "").strip()
            or (
                f"{requested_by}:{action_kind}:{canonical_url}:"
                f"{candidate.get('raw_hash') or ''}"
            )
        )
        package_name = compute_package_name(
            candidate.get("clean_title")
            or candidate.get("title")
            or "RSS Candidate",
            candidate.get("description_year") or candidate.get("title_year"),
            candidate.get("resolution") or "",
            candidate.get("season"),
        )
        result = self.db.create_hdencode_action(
            action_uuid=action_uuid,
            idempotency_key=idempotency_key,
            canonical_url=canonical_url,
            action_kind=action_kind,
            requested_by=requested_by,
            service_type=service_type,
            priority=priority,
            package_name=package_name,
            destination=destination or "",
            lifespan_generation=lifespan_generation,
            authorized_evidence=evidence,
        )
        if result.get("conflict"):
            raise HDEncodeActionError(
                "action_conflict",
                "Another action for this candidate is already active.",
            )
        return result

    def run_action(
        self,
        action_uuid: str,
        *,
        owns_lifespan: Optional[Callable[[], bool]] = None,
    ) -> dict:
        action = self.db.claim_hdencode_action(action_uuid)
        if action is None:
            existing = self.db.get_hdencode_action(action_uuid)
            if existing is None:
                raise HDEncodeActionError(
                    "action_not_found",
                    "The RSS action was not found.",
                    status_code=404,
                )
            return existing

        def cancelled() -> bool:
            if owns_lifespan is not None:
                try:
                    if not owns_lifespan():
                        return True
                except Exception:
                    return True
            return bool(self.db.hdencode_action_cancel_requested(action_uuid))

        if cancelled():
            self.db.cancel_hdencode_action(
                action_uuid,
                reason="cancelled_before_request",
            )
            return self.db.get_hdencode_action(action_uuid)

        try:
            with self.coordinator.prioritize(int(action.get("priority") or 100)):
                scraped = self.download.scrape_links(
                    action["canonical_url"],
                    action.get("service_type") or "Rapidgator",
                )
        except (HDEncodeRequestCancelled, HDEncodeTrafficDenied) as exc:
            self.db.fail_hdencode_action(
                action_uuid,
                error_code=getattr(exc, "code", "traffic_denied"),
            )
            return self.db.get_hdencode_action(action_uuid)
        except Exception as exc:
            logger.exception("RSS action link retrieval failed")
            self.db.fail_hdencode_action(
                action_uuid,
                error_code=type(exc).__name__,
            )
            return self.db.get_hdencode_action(action_uuid)

        # The response is not allowed to publish after ownership/cancellation
        # changes, even when the underlying browser request itself succeeded.
        if cancelled():
            self.db.cancel_hdencode_action(
                action_uuid,
                reason="cancelled_after_response",
            )
            return self.db.get_hdencode_action(action_uuid)

        links = list(scraped or [])
        if not links:
            diagnostic = getattr(scraped, "diagnostic", None)
            code = getattr(getattr(diagnostic, "code", None), "value", None)
            self.db.fail_hdencode_action(
                action_uuid,
                error_code=code or "no_links_found",
            )
            return self.db.get_hdencode_action(action_uuid)

        # Persist both the result and source-post mapping before any external
        # JDownloader side effect.
        if not self.db.mark_hdencode_action_links_ready(action_uuid, links=links):
            return self.db.get_hdencode_action(action_uuid)
        try:
            self.db.record_scraped_links(
                links,
                action.get("title")
                or action.get("package_name")
                or "RSS Candidate",
                action.get("resolution") or "",
                action["canonical_url"],
            )
        except Exception:
            logger.exception("Failed to persist RSS action source-link mapping")

        if action.get("action_kind") == "retrieve_links":
            return self.db.get_hdencode_action(action_uuid)

        if cancelled():
            self.db.cancel_hdencode_action(
                action_uuid,
                reason="cancelled_before_submit",
            )
            return self.db.get_hdencode_action(action_uuid)

        if not self.db.mark_hdencode_action_submitting(action_uuid):
            return self.db.get_hdencode_action(action_uuid)

        if cancelled():
            self.db.cancel_hdencode_action(
                action_uuid,
                reason="cancelled_before_submit",
            )
            return self.db.get_hdencode_action(action_uuid)

        try:
            submitted = self.download.send_to_jdownloader(
                links,
                action.get("package_name") or "RSS Candidate",
                action.get("destination") or "",
            )
        except Exception as exc:
            # Submission may have crossed the process boundary before raising.
            # Never make that automatically retryable.
            logger.exception("RSS action JDownloader submission failed")
            self.db.mark_hdencode_action_needs_review(
                action_uuid,
                error_code=type(exc).__name__,
            )
            return self.db.get_hdencode_action(action_uuid)

        if not submitted:
            self.db.fail_hdencode_action(
                action_uuid,
                error_code="submission_failed",
            )
            return self.db.get_hdencode_action(action_uuid)

        # An external side effect has happened. A later ownership loss cannot
        # safely trigger a retry because that could duplicate the submission.
        if cancelled():
            self.db.mark_hdencode_action_needs_review(
                action_uuid,
                error_code="owner_expired_after_submit",
            )
            return self.db.get_hdencode_action(action_uuid)

        self.db.complete_hdencode_action_submitted(action_uuid)
        candidate = self.db.get_hdencode_candidate(action["canonical_url"]) or {}
        hdr_formats = candidate.get("hdr_formats") or ""
        if isinstance(hdr_formats, str):
            try:
                parsed = json.loads(hdr_formats)
                if isinstance(parsed, list):
                    hdr_formats = ", ".join(str(value) for value in parsed)
            except (TypeError, ValueError):
                pass
        for link in links:
            try:
                self.download.save_to_history(
                    link,
                    candidate.get("clean_title")
                    or candidate.get("title")
                    or "RSS Candidate",
                    candidate.get("season"),
                    candidate.get("resolution") or "",
                    candidate.get("size_text") or "",
                    status="completed",
                    hdr=str(hdr_formats or ""),
                    dovi=candidate.get("dv_evidence") == "asserted",
                    year=(
                        candidate.get("description_year")
                        or candidate.get("title_year")
                    ),
                    package_name=action.get("package_name"),
                    service_type=action.get("service_type"),
                )
            except Exception:
                logger.exception("Failed to persist RSS action download history")
        return self.db.get_hdencode_action(action_uuid)

    def queue_approved_auto_actions(
        self,
        *,
        limit: int = 1,
        lifespan_generation: Optional[int] = None,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> list[dict]:
        if self.config.get("hdencode_rss_auto_grab_enabled") is not True:
            return []
        readiness = self.db.get_hdencode_rss_readiness(
            min_cycles=self.config.get("hdencode_rss_shadow_min_cycles", 20),
            min_days=self.config.get("hdencode_rss_shadow_min_days", 7),
        )
        if not readiness.get("ready"):
            return []
        queued = []
        for candidate in self.db.list_hdencode_candidates(limit=500):
            if stop_requested and stop_requested():
                break
            if len(queued) >= max(0, min(int(limit), 10)):
                break
            if candidate.get("action_state") not in {
                None,
                "",
                "none",
                "failed",
                "cancelled",
            }:
                continue
            try:
                result = self.queue_action(
                    candidate["canonical_url"],
                    action_kind="grab",
                    requested_by="auto",
                    service_type="Rapidgator",
                    lifespan_generation=lifespan_generation,
                )
            except HDEncodeActionError:
                continue
            queued.append(result)
        return queued

    def _validate_auto_action(self, candidate: dict, action_kind: str) -> None:
        if self.config.get("hdencode_rss_auto_grab_enabled") is not True:
            raise HDEncodeActionError(
                "auto_grab_disabled",
                "RSS automatic grabbing is disabled.",
            )
        if action_kind != "grab":
            raise HDEncodeActionError(
                "auto_action_invalid",
                "Automatic actions may only submit approved grabs.",
            )
        if candidate.get("relevance_state") not in {
            "relevant_missing",
            "relevant_upgrade",
        }:
            raise HDEncodeActionError(
                "auto_not_relevant",
                "The candidate is not approved for automatic action.",
            )
        if candidate.get("identity_state") not in {"exact", "high"}:
            # Raw 'hydrated' is provenance, not confirmed identity — it is not
            # sufficient for an autonomous grab. Identity must be promoted to
            # 'exact'/'high' (external id, unique Plex match, or a complete
            # non-conflicting tuple) by classify_candidate first.
            raise HDEncodeActionError(
                "auto_identity_unknown",
                "The candidate identity is not confirmed.",
            )
        if candidate.get("hydration_state") != "completed":
            raise HDEncodeActionError(
                "auto_hydration_required",
                "Detail hydration is required before automatic action.",
            )
        if not candidate.get("description_complete"):
            raise HDEncodeActionError(
                "auto_evidence_incomplete",
                "Complete detail evidence is required.",
            )
        if (
            candidate.get("title_year")
            and candidate.get("description_year")
            and candidate["title_year"] != candidate["description_year"]
        ):
            raise HDEncodeActionError(
                "auto_year_conflict",
                "Conflicting year evidence requires review.",
            )
        if (
            candidate.get("dv_evidence") == "unknown"
            or candidate.get("hdr_evidence") == "unknown"
        ):
            raise HDEncodeActionError(
                "auto_video_evidence_unknown",
                "Unknown Dolby Vision or HDR evidence requires review.",
            )

    @staticmethod
    def _authorization_evidence(candidate: dict, requested_by: str) -> dict:
        return {
            "requested_by": requested_by,
            "relevance_state": candidate.get("relevance_state"),
            "identity_state": candidate.get("identity_state"),
            "hydration_state": candidate.get("hydration_state"),
            "dv_evidence": candidate.get("dv_evidence"),
            "hdr_evidence": candidate.get("hdr_evidence"),
            "description_complete": bool(candidate.get("description_complete")),
            "title_year": candidate.get("title_year"),
            "description_year": candidate.get("description_year"),
            "discovery_source": candidate.get("discovery_source") or "rss",
        }
