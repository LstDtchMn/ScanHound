"""Combined promotion gate — acquisition AND decision suitability.

Qualification is split in two, because the two questions have different
evidence:

* **Phase A — acquisition.** Does RSS find the required releases, reliably and
  efficiently? Cycle counts, latency, feed health, request reduction, restart
  and catch-up recovery. This is what ``get_hdencode_rss_readiness`` measures.
* **Phase B — decision suitability.** Does RSS reach the *same actionable
  decision* the listing pipeline would? Nothing measures this yet.

**The split was document-only until this module existed.** Four call sites
accepted Phase A readiness alone as permission to run RSS as primary and to
queue automatic actions:

    backend/api/routes/rss.py:283            the mode-change route
    backend/hdencode_rss_service.py:108      rss_primary polling
    backend/hdencode_rss_service.py:173      listing-fallback authorisation
    backend/hdencode_action_service.py:347   automatic action queuing

So one API call after a green Phase A promoted on half the evidence. A written
gate that a single configuration change can walk through is not a gate. The
governing sentence is:

    A green Phase A result is preserved evidence, not permission to promote.

This module is deliberately pure — no database, no service, no config — so the
rule can be tested exhaustively and cannot fail differently in production than
it does under test.
"""
from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Optional

#: Phase B outcomes. Anything not explicitly PASS blocks, including junk.
PHASE_B_PASS = "pass"
PHASE_B_FAIL = "fail"


class ArtifactBindings(NamedTuple):
    """What a Phase B verdict was measured AGAINST.

    A verdict is only evidence about the artifact that produced it. If any of
    these has moved since, the verdict describes a system that no longer
    exists, and re-using it would be the qualification-window boundary problem
    in a new place.
    """

    acquisition_artifact_digest: str
    decision_bridge_digest: str
    phase_a_corpus_digest: str
    configuration_fingerprint: str
    parser_version: str
    equivalence_contract_version: str


class PromotionDecision(NamedTuple):
    allowed: bool
    #: Every reason promotion is refused, not just the first. An operator who
    #: fixes one blocker should be able to see the rest without another round
    #: trip.
    blockers: tuple


def _binding_mismatches(verdict: Mapping[str, Any],
                        current: ArtifactBindings) -> tuple:
    out = []
    for field in ArtifactBindings._fields:
        expected = getattr(current, field)
        recorded = verdict.get(field)
        if not recorded:
            out.append(f"phase_b_missing_{field}")
        elif str(recorded) != str(expected):
            out.append(f"phase_b_{field}_mismatch")
    return tuple(out)


def evaluate_promotion(
    acquisition: Optional[Mapping[str, Any]],
    phase_b: Optional[Mapping[str, Any]],
    *,
    current_bindings: Optional[ArtifactBindings],
) -> PromotionDecision:
    """Decide whether RSS may act as primary.

    FAILS CLOSED on every kind of absence: no acquisition result, no Phase B
    verdict, no bindings to check against. "We have no evidence" and "the
    evidence says no" must be treated identically — an earlier version of the
    readiness code returned a clean success on an empty window, and that is the
    same mistake one layer up.
    """
    blockers = []

    if not acquisition:
        blockers.append("acquisition_evidence_unavailable")
    elif not acquisition.get("acquisition_ready"):
        blockers.append("acquisition_not_ready")

    if not phase_b:
        # The default state of the world. Until Phase B has actually been run,
        # promotion is refused — which is the entire point of this module.
        blockers.append("phase_b_verdict_absent")
        return PromotionDecision(False, tuple(blockers))

    status = str(phase_b.get("phase_b_status") or "").strip().lower()
    if status != PHASE_B_PASS:
        blockers.append(
            "phase_b_failed" if status == PHASE_B_FAIL else "phase_b_not_passed")

    # Counts are read strictly. A missing count is NOT zero: it means the
    # Phase B run did not report one, which is an incomplete verdict.
    for key in ("material_mismatch_count", "inconclusive_count"):
        value = phase_b.get(key)
        if value is None:
            blockers.append(f"phase_b_missing_{key}")
        else:
            try:
                if int(value) != 0:
                    blockers.append(f"phase_b_{key}_nonzero")
            except (TypeError, ValueError):
                blockers.append(f"phase_b_invalid_{key}")

    if current_bindings is None:
        blockers.append("artifact_bindings_unavailable")
    else:
        blockers.extend(_binding_mismatches(phase_b, current_bindings))

    return PromotionDecision(not blockers, tuple(blockers))
