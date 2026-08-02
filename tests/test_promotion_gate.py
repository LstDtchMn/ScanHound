"""The combined promotion gate must refuse everything except a complete pass.

Until this gate existed, four call sites accepted Phase A acquisition readiness
alone as permission to run RSS as primary and to queue automatic actions. The
Phase A/Phase B split lived only in a planning document, so one API call after
a green Phase A promoted on half the evidence.

These are mostly NEGATIVE tests on purpose. A gate is defined by what it
refuses; the single allow case is one test among many.
"""

import pytest

from backend.promotion_gate import (
    ArtifactBindings,
    evaluate_promotion,
)

BINDINGS = ArtifactBindings(
    acquisition_artifact_digest="sha256:acq",
    decision_bridge_digest="sha256:bridge",
    phase_a_corpus_digest="sha256:corpus",
    configuration_fingerprint="cfg-1",
    parser_version="grammar-1",
    equivalence_contract_version="contract-1",
)

READY = {"acquisition_ready": True}


def passing_phase_b(**overrides):
    verdict = {
        "phase_b_status": "pass",
        "material_mismatch_count": 0,
        "inconclusive_count": 0,
        **BINDINGS._asdict(),
    }
    verdict.update(overrides)
    return verdict


class TestTheOnlyWayThrough:
    def test_everything_present_and_matching_is_allowed(self):
        decision = evaluate_promotion(READY, passing_phase_b(),
                                      current_bindings=BINDINGS)
        assert decision.allowed is True
        assert decision.blockers == ()


class TestAbsenceFailsClosed:
    """"We have no evidence" and "the evidence says no" must behave the same.
    An earlier version of the readiness code returned a clean success on an
    empty window; this is that mistake one layer up."""

    def test_phase_b_absent_blocks(self):
        decision = evaluate_promotion(READY, None, current_bindings=BINDINGS)
        assert decision.allowed is False
        assert "phase_b_verdict_absent" in decision.blockers

    def test_phase_b_absent_is_the_DEFAULT_state_of_the_world(self):
        """Nothing has run Phase B yet. Promotion must be refused today,
        without anyone having to remember to configure a refusal."""
        assert evaluate_promotion(READY, {}, current_bindings=BINDINGS).allowed is False

    def test_acquisition_absent_blocks(self):
        decision = evaluate_promotion(None, passing_phase_b(),
                                      current_bindings=BINDINGS)
        assert decision.allowed is False
        assert "acquisition_evidence_unavailable" in decision.blockers

    def test_bindings_absent_blocks(self):
        decision = evaluate_promotion(READY, passing_phase_b(),
                                      current_bindings=None)
        assert decision.allowed is False
        assert "artifact_bindings_unavailable" in decision.blockers

    def test_a_missing_count_is_not_zero(self):
        """An absent count means Phase B did not report one — an incomplete
        verdict, not a clean sheet."""
        verdict = passing_phase_b()
        del verdict["material_mismatch_count"]
        decision = evaluate_promotion(READY, verdict, current_bindings=BINDINGS)
        assert decision.allowed is False
        assert "phase_b_missing_material_mismatch_count" in decision.blockers


class TestPhaseAAloneIsNotEnough:
    """The whole reason this module exists."""

    def test_phase_A_pass_plus_phase_B_absent_is_rejected(self):
        assert evaluate_promotion(READY, None, current_bindings=BINDINGS).allowed is False

    def test_phase_A_pass_plus_phase_B_fail_is_rejected(self):
        decision = evaluate_promotion(READY, passing_phase_b(phase_b_status="fail"),
                                      current_bindings=BINDINGS)
        assert decision.allowed is False
        assert "phase_b_failed" in decision.blockers

    def test_phase_B_pass_does_not_rescue_a_failed_phase_A(self):
        decision = evaluate_promotion({"acquisition_ready": False},
                                      passing_phase_b(), current_bindings=BINDINGS)
        assert decision.allowed is False
        assert "acquisition_not_ready" in decision.blockers

    @pytest.mark.parametrize("status", ["", "PASSED", "ok", "true", "yes",
                                        "pending", "unknown", None])
    def test_only_the_exact_word_pass_counts(self, status):
        """Anything else — including plausible near-misses and junk — blocks.
        A gate that accepts 'ok' will eventually accept a typo."""
        decision = evaluate_promotion(READY, passing_phase_b(phase_b_status=status),
                                      current_bindings=BINDINGS)
        assert decision.allowed is False

    @pytest.mark.parametrize("status", ["pass", "PASS", " Pass "])
    def test_case_and_padding_are_tolerated_on_the_exact_word(self, status):
        assert evaluate_promotion(READY, passing_phase_b(phase_b_status=status),
                                  current_bindings=BINDINGS).allowed is True


class TestMismatchCounts:
    @pytest.mark.parametrize("field", ["material_mismatch_count",
                                       "inconclusive_count"])
    def test_any_nonzero_count_blocks(self, field):
        decision = evaluate_promotion(READY, passing_phase_b(**{field: 1}),
                                      current_bindings=BINDINGS)
        assert decision.allowed is False
        assert f"phase_b_{field}_nonzero" in decision.blockers

    @pytest.mark.parametrize("value", ["many", "", [], {}])
    def test_an_uninterpretable_count_blocks(self, value):
        decision = evaluate_promotion(
            READY, passing_phase_b(material_mismatch_count=value),
            current_bindings=BINDINGS)
        assert decision.allowed is False


class TestArtifactBindings:
    """A verdict is evidence about the artifact that produced it. If anything
    has moved since, it describes a system that no longer exists."""

    @pytest.mark.parametrize("field", ArtifactBindings._fields)
    def test_any_binding_mismatch_blocks(self, field):
        decision = evaluate_promotion(
            READY, passing_phase_b(**{field: "something-else"}),
            current_bindings=BINDINGS)
        assert decision.allowed is False
        assert f"phase_b_{field}_mismatch" in decision.blockers

    @pytest.mark.parametrize("field", ArtifactBindings._fields)
    def test_any_missing_binding_blocks(self, field):
        verdict = passing_phase_b()
        del verdict[field]
        decision = evaluate_promotion(READY, verdict, current_bindings=BINDINGS)
        assert decision.allowed is False
        assert f"phase_b_missing_{field}" in decision.blockers

    def test_a_rebuilt_image_invalidates_a_previous_pass(self):
        """The exact-digest rule, enforced rather than documented: rebuilding
        the acquisition artifact voids a Phase B verdict measured against the
        old one."""
        rebuilt = BINDINGS._replace(acquisition_artifact_digest="sha256:acq-v2")
        assert evaluate_promotion(READY, passing_phase_b(),
                                  current_bindings=rebuilt).allowed is False


class TestBlockerReporting:
    def test_every_blocker_is_reported_not_just_the_first(self):
        """An operator who fixes one blocker should see the rest without
        another round trip."""
        decision = evaluate_promotion(
            {"acquisition_ready": False},
            passing_phase_b(phase_b_status="fail", material_mismatch_count=3,
                            parser_version="grammar-0"),
            current_bindings=BINDINGS)
        assert decision.allowed is False
        assert len(decision.blockers) >= 4
        assert "acquisition_not_ready" in decision.blockers
        assert "phase_b_failed" in decision.blockers
        assert "phase_b_material_mismatch_count_nonzero" in decision.blockers
        assert "phase_b_parser_version_mismatch" in decision.blockers

    def test_an_allowed_decision_carries_no_blockers(self):
        assert evaluate_promotion(READY, passing_phase_b(),
                                  current_bindings=BINDINGS).blockers == ()
