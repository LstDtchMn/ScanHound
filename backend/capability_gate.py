"""Program-level capability admission — R-6, the round-10 boundaries.

Two capabilities consult this gate, and only these two:

1. **Listing demotion** — permission to rely on RSS as primary and stand
   down the listing safety net (the mode admission, plus a per-cycle check
   that DEMOTES to shadow behaviour when a once-valid gate goes invalid:
   polling continues, evidence keeps flowing, the safety net returns).
2. **Autonomous side effects** — auto-grab and auto-rename admission. A
   flipped config flag without a recorded evidence pass stays inert.

RSS *polling* is deliberately ungated: it gathers the evidence any recovery
or requalification needs (gating it would starve the gate of its own
inputs — round-10 verdict).

Today no instrument writes the verdict blobs, so every check denies —
byte-identical outcomes to the current flag guards, but enforced by
evidence rather than by a flag someone could flip early. The Phase A/B
graders will persist their verdicts under the config keys below; the deploy
pipeline records the digests.
"""
from typing import Optional, Tuple

from backend.promotion_gate import ArtifactBindings, evaluate_promotion
from backend.release_grammar import GRAMMAR_VERSION

#: Where the (future) instruments persist their outputs.
PHASE_A_VERDICT_KEY = "hdencode_phase_a_verdict"
PHASE_B_VERDICT_KEY = "hdencode_phase_b_verdict"
BINDING_KEYS = {
    "acquisition_artifact_digest": "hdencode_acquisition_artifact_digest",
    "decision_bridge_digest": "hdencode_decision_bridge_digest",
    "phase_a_corpus_digest": "hdencode_phase_a_corpus_digest",
    "configuration_fingerprint": "hdencode_configuration_fingerprint",
    "equivalence_contract_version": "hdencode_equivalence_contract_version",
}


def current_bindings(config) -> Optional[ArtifactBindings]:
    """The live artifact identity, or None when any component is unrecorded.

    parser_version is NEVER read from config: it comes from the running
    grammar itself, so a stale recorded verdict cannot vouch for a parser it
    was not measured against.
    """
    cfg = config or {}
    values = {field: cfg.get(key) for field, key in BINDING_KEYS.items()}
    if any(not isinstance(v, str) or not v for v in values.values()):
        return None
    return ArtifactBindings(parser_version=GRAMMAR_VERSION, **values)


def capability_blockers(config) -> Tuple[str, ...]:
    """Empty tuple = the capability may act. Anything else lists why not."""
    cfg = config or {}
    decision = evaluate_promotion(
        cfg.get(PHASE_A_VERDICT_KEY) or None,
        cfg.get(PHASE_B_VERDICT_KEY) or None,
        current_bindings=current_bindings(cfg),
    )
    return () if decision.allowed else tuple(decision.blockers)
