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


def _is_exact_zero(value: Any) -> bool:
    """Whether ``value`` is a genuine integer zero.

    Deliberately strict, because the permissive version shipped and failed
    open. ``int(0.9)`` truncates to 0, and ``bool`` subclasses ``int`` so
    ``int(False)`` is 0 — a verdict reporting ``material_mismatch_count=0.9``
    or ``inconclusive_count=False`` was read as a clean sheet.
    """
    if isinstance(value, bool):        # bool before int: True/False are ints
        return False
    return isinstance(value, int) and value == 0


def _binding_mismatches(verdict: Mapping[str, Any],
                        current: ArtifactBindings) -> tuple:
    """Compare bindings without cross-type coercion.

    The first version stringified both sides, so values of different types
    could compare equal. A binding is an exact identifier; anything that is not
    a non-empty string of the expected value is a mismatch, not a near-miss.
    """
    out = []
    for field in ArtifactBindings._fields:
        expected = getattr(current, field)
        recorded = verdict.get(field)
        if not isinstance(recorded, str) or not recorded.strip():
            out.append(f"phase_b_missing_{field}")
        elif not isinstance(expected, str) or recorded != expected:
            out.append(f"phase_b_{field}_mismatch")
    return tuple(out)


def evaluate_promotion(
    acquisition: Optional[Mapping[str, Any]],
    phase_b: Optional[Mapping[str, Any]],
    *,
    current_bindings: Optional[ArtifactBindings],
) -> PromotionDecision:
    """Decide whether RSS may act as primary.

    FAILS CLOSED on absence AND on malformation. "We have no evidence", "the
    evidence says no", and "the evidence is unreadable" are treated
    identically.

    The malformation half was missing from the first version and it failed
    open: ``acquisition_ready="false"`` is truthy, ``int(0.9)`` truncates to
    zero, and ``False`` is an ``int``, so a verdict built from those returned
    ``allowed=True``. Forty-one tests covered absence and non-zero counts and
    never covered garbage. A gate that fails closed on absence and open on
    malformed input is not a fail-closed gate.
    """
    blockers = []

    if not acquisition:
        blockers.append("acquisition_evidence_unavailable")
    elif acquisition.get("acquisition_ready") is not True:
        # `is not True`, not falsiness: the string "false" is truthy, and so is
        # "0", 1, and any non-empty object. Readiness is a decision, and only
        # the literal boolean records one.
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

    # Counts are read strictly, in three separate ways:
    #   absent      -> the run did not report one; an incomplete verdict
    #   malformed   -> not a genuine integer (0.9, False, "0", None-like)
    #   non-zero    -> a real finding
    # Only an exact integer zero passes. See _is_exact_zero for why int() and
    # truthiness are both unsafe here.
    for key in ("material_mismatch_count", "inconclusive_count"):
        if key not in phase_b or phase_b.get(key) is None:
            blockers.append(f"phase_b_missing_{key}")
            continue
        value = phase_b[key]
        if isinstance(value, bool) or not isinstance(value, int):
            blockers.append(f"phase_b_invalid_{key}")
        elif not _is_exact_zero(value):
            blockers.append(f"phase_b_{key}_nonzero")

    if current_bindings is None:
        blockers.append("artifact_bindings_unavailable")
    else:
        blockers.extend(_binding_mismatches(phase_b, current_bindings))

    return PromotionDecision(not blockers, tuple(blockers))
