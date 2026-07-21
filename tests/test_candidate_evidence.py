"""Tests for claim-aware candidate evidence."""
from backend.candidate_evidence import (
    CandidateDecisionEngine,
    CandidateEvidence,
    EvidenceState,
)


def test_missing_claim_is_unknown_not_false():
    evidence = CandidateEvidence.from_mapping({})
    assert evidence.dv is EvidenceState.UNKNOWN
    assert "dovi" not in evidence.as_legacy_web_fields()
    assert evidence.as_legacy_web_fields()["dovi_evidence"] == "unknown"


def test_asserted_and_negated_claims_bridge_deliberately():
    asserted = CandidateEvidence.from_mapping({"dv": "asserted"})
    negated = CandidateEvidence.from_mapping({"dv": "negated"})
    assert asserted.as_legacy_web_fields()["dovi"] is True
    assert negated.as_legacy_web_fields()["dovi"] is False


def test_unknown_dv_cannot_replace_known_dv_copy():
    decision = CandidateDecisionEngine().decide(
        CandidateEvidence(
            resolution="2160p",
            size_gb=80,
            dv=EvidenceState.UNKNOWN,
            identity_confidence="exact",
        ),
        existing={"resolution": "2160p", "size_gb": 60, "dovi": True},
    )
    assert decision.state == "detail_required"
    assert decision.reason == "dolby_vision_unknown"
    assert decision.safe_to_auto_act is False


def test_asserted_dv_is_upgrade_at_same_resolution():
    decision = CandidateDecisionEngine().decide(
        CandidateEvidence(
            resolution="2160p",
            size_gb=50,
            dv=EvidenceState.ASSERTED,
            identity_confidence="exact",
        ),
        existing={"resolution": "2160p", "size_gb": 60, "dovi": False},
    )
    assert decision.state == "relevant_upgrade"
    assert decision.reason == "dolby_vision_gain"


def test_lower_resolution_without_asserted_dv_is_conclusive_skip():
    decision = CandidateDecisionEngine().decide(
        CandidateEvidence(
            resolution="1080p",
            dv=EvidenceState.NEGATED,
            identity_confidence="exact",
        ),
        existing={"resolution": "2160p", "dovi": False},
    )
    assert decision.state == "irrelevant_conclusive"


def test_year_conflict_is_preserved_and_blocks_auto_action():
    evidence = CandidateEvidence(
        resolution="2160p",
        dv=EvidenceState.ASSERTED,
        title_year=2025,
        description_year=2024,
        identity_confidence="exact",
    )
    assert evidence.observed_years == (2024, 2025)
    assert evidence.year_conflict is True
    decision = CandidateDecisionEngine().decide(
        evidence,
        existing={"resolution": "1080p", "dovi": False},
    )
    assert decision.state == "relevant_upgrade"
    assert decision.safe_to_auto_act is False


def test_exact_url_history_is_conclusive_skip():
    decision = CandidateDecisionEngine().decide(
        CandidateEvidence(identity_confidence="unknown"),
        existing=None,
        exact_url_downloaded=True,
    )
    assert decision.reason == "exact_url_already_downloaded"
