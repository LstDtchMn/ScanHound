"""Claim-aware evidence and conservative candidate classification."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


class EvidenceState(str, Enum):
    ASSERTED = "asserted"
    NEGATED = "negated"
    UNKNOWN = "unknown"


def coerce_evidence_state(value: Any) -> EvidenceState:
    if isinstance(value, EvidenceState):
        return value
    normalized = str(value or "").strip().lower()
    if normalized == EvidenceState.ASSERTED.value:
        return EvidenceState.ASSERTED
    if normalized == EvidenceState.NEGATED.value:
        return EvidenceState.NEGATED
    return EvidenceState.UNKNOWN


@dataclass(frozen=True)
class CandidateEvidence:
    """Normalized claims without pretending missing fields are false."""

    resolution: Optional[str] = None
    size_gb: Optional[float] = None
    dv: EvidenceState = EvidenceState.UNKNOWN
    hdr: EvidenceState = EvidenceState.UNKNOWN
    hevc: EvidenceState = EvidenceState.UNKNOWN
    hdr_formats: tuple[str, ...] = field(default_factory=tuple)
    title_year: Optional[int] = None
    description_year: Optional[int] = None
    identity_confidence: str = "unknown"
    description_complete: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateEvidence":
        formats = tuple(
            dict.fromkeys(
                str(item).strip().upper()
                for item in (value.get("hdr_formats") or ())
                if str(item).strip()
            )
        )
        return cls(
            resolution=(str(value.get("resolution") or "").strip() or None),
            size_gb=_float_or_none(value.get("size_gb")),
            dv=coerce_evidence_state(value.get("dv")),
            hdr=coerce_evidence_state(value.get("hdr")),
            hevc=coerce_evidence_state(value.get("hevc")),
            hdr_formats=formats,
            title_year=_int_or_none(value.get("title_year")),
            description_year=_int_or_none(value.get("description_year")),
            identity_confidence=str(
                value.get("identity_confidence") or "unknown"
            ),
            description_complete=bool(value.get("description_complete")),
        )

    @property
    def observed_years(self) -> tuple[int, ...]:
        years = []
        for year in (self.description_year, self.title_year):
            if year is not None and year not in years:
                years.append(year)
        return tuple(years)

    @property
    def year_conflict(self) -> bool:
        return (
            self.title_year is not None
            and self.description_year is not None
            and self.title_year != self.description_year
        )

    def as_legacy_web_fields(self) -> dict[str, Any]:
        """Populate Boolean fields only when the evidence is authoritative."""
        out: dict[str, Any] = {
            "dovi_evidence": self.dv.value,
            "hdr_evidence": self.hdr.value,
            "hdr_formats": list(self.hdr_formats),
        }
        if self.dv is EvidenceState.ASSERTED:
            out["dovi"] = True
        elif self.dv is EvidenceState.NEGATED:
            out["dovi"] = False
        if self.hdr is EvidenceState.ASSERTED:
            out["hdr"] = "HDR"
        elif self.hdr is EvidenceState.NEGATED:
            out["hdr"] = "SDR"
        return out


@dataclass(frozen=True)
class CandidateDecision:
    state: str
    reason: str
    requires_detail: bool = False
    safe_to_auto_act: bool = False


class CandidateDecisionEngine:
    """Conservative feed-first classification.

    Existing matching rules remain authoritative after hydration. This layer
    only decides whether RSS evidence is already sufficient to skip, surface,
    or hydrate a candidate.
    """

    _RESOLUTION_RANK = {
        "2160p": 4,
        "4k": 4,
        "uhd": 4,
        "1080p": 3,
        "720p": 2,
        "480p": 1,
    }

    def resolution_rank(self, value: Optional[str]) -> int:
        return self._RESOLUTION_RANK.get(str(value or "").strip().lower(), 0)

    def decide(
        self,
        evidence: CandidateEvidence,
        *,
        existing: Optional[Mapping[str, Any]],
        exact_url_downloaded: bool = False,
        minimum_size_gain: float = 0.10,
    ) -> CandidateDecision:
        if exact_url_downloaded:
            return CandidateDecision(
                "irrelevant_conclusive",
                "exact_url_already_downloaded",
            )

        if existing is None:
            if evidence.identity_confidence in {"exact", "high"}:
                return CandidateDecision(
                    "relevant_missing",
                    "not_in_library",
                    safe_to_auto_act=not evidence.year_conflict,
                )
            return CandidateDecision(
                "detail_required",
                "identity_unresolved",
                requires_detail=True,
            )

        existing_resolution = existing.get("resolution") or existing.get("res")
        incoming_rank = self.resolution_rank(evidence.resolution)
        existing_rank = self.resolution_rank(existing_resolution)

        if incoming_rank and existing_rank and incoming_rank > existing_rank:
            return CandidateDecision(
                "relevant_upgrade",
                "higher_resolution",
                safe_to_auto_act=not evidence.year_conflict,
            )

        existing_dv = bool(existing.get("dovi"))
        if (
            incoming_rank == existing_rank
            and evidence.dv is EvidenceState.ASSERTED
            and not existing_dv
        ):
            return CandidateDecision(
                "relevant_upgrade",
                "dolby_vision_gain",
                safe_to_auto_act=not evidence.year_conflict,
            )

        # Unknown is never interpreted as explicit loss. It also cannot support
        # an automatic replacement of a known-DV library copy.
        if existing_dv and evidence.dv is EvidenceState.UNKNOWN:
            return CandidateDecision(
                "detail_required",
                "dolby_vision_unknown",
                requires_detail=True,
            )

        incoming_size = evidence.size_gb
        existing_size = _float_or_none(existing.get("size_gb") or existing.get("size"))
        same_resolution = incoming_rank and incoming_rank == existing_rank

        if (
            same_resolution
            and incoming_size is not None
            and existing_size
            and incoming_size > existing_size * (1.0 + minimum_size_gain)
        ):
            if existing_dv and evidence.dv is EvidenceState.NEGATED:
                return CandidateDecision(
                    "detail_required",
                    "explicit_dv_loss_needs_full_rules",
                    requires_detail=True,
                )
            if evidence.dv is EvidenceState.UNKNOWN:
                return CandidateDecision(
                    "detail_required",
                    "size_upgrade_with_unknown_dv",
                    requires_detail=True,
                )
            return CandidateDecision(
                "relevant_upgrade",
                "same_resolution_size_gain",
                safe_to_auto_act=not evidence.year_conflict,
            )

        if (
            incoming_rank
            and existing_rank
            and incoming_rank < existing_rank
            and evidence.dv is not EvidenceState.ASSERTED
        ):
            return CandidateDecision(
                "irrelevant_conclusive",
                "lower_resolution_non_dv",
            )

        if (
            same_resolution
            and incoming_size is not None
            and existing_size is not None
            and incoming_size <= existing_size * (1.0 + minimum_size_gain)
            and evidence.dv in {EvidenceState.NEGATED, EvidenceState.ASSERTED}
        ):
            return CandidateDecision(
                "irrelevant_conclusive",
                "same_or_smaller_known_claims",
            )

        return CandidateDecision(
            "detail_required",
            "insufficient_evidence",
            requires_detail=True,
        )


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
