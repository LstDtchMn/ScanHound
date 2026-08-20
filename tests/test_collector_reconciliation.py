"""Qualification collector — the readiness cross-check must fail closed.

Imported by path because the collector is a host artifact, not a package
module. Testing it directly rather than reading it is the point: the defect it
guards against survived precisely because nothing exercised the gate.
"""

import importlib.util
from pathlib import Path

import pytest

COLLECTOR = (Path(__file__).resolve().parents[1]
             / "docs" / "feature-pack-review" / "qualification-evidence"
             / "collect_shadow_evidence.py")


def _load():
    spec = importlib.util.spec_from_file_location("collect_shadow_evidence", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collector():
    return _load()


def blockers(collector, app, *, missing_credentials=False, reconciliation=None):
    """Drive the real signature, which takes the VERDICT separately.

    UPDATED 2026-08-19. This helper passed the verdict as `app_readiness`,
    because when it was written the comparison lived in that payload. It no
    longer does, and the reason is in the production code:

        "The COMPARISON lives in `reconciliation`, not in `app_readiness`.
         Reading it from the payload being compared is what broke this."

    A test that keeps handing the verdict to the old parameter cannot see that
    split, and would go on passing while the collector read the wrong field --
    which is the exact defect the split fixed.

    A bare `ready_matches` dict is therefore routed to `reconciliation`, so the
    old call sites keep testing what they were written to test.
    """
    if reconciliation is None and isinstance(app, dict) and "ready_matches" in app:
        app, reconciliation = {}, app
    return collector.reconciliation_blockers(
        app, missing_credentials=missing_credentials,
        reconciliation=reconciliation)


class TestFailsClosed:
    def test_no_auth_token_BLOCKS(self, collector):
        """REGRESSION (review blocker 3). Missing credentials silently
        downgraded the run to DB-only and could still report ready=True.
        Independence is the whole value of the cross-check, so 'we had no
        credentials' is not a lesser failure than 'the check disagreed'."""
        out = blockers(collector, None, missing_credentials=True)
        assert out and "NO AUTH TOKEN" in out[0]

    def test_no_auth_token_blocks_EVEN_WITH_a_passing_app_readiness(self, collector):
        """The credential check comes first: a readiness dict that appears to
        agree cannot have come from a run that had no credentials."""
        out = blockers(collector, {"ready_matches": True}, missing_credentials=True)
        assert out and "NO AUTH TOKEN" in out[0]

    def test_a_failed_cross_check_blocks(self, collector):
        """The state that was actually shipping: connection refused, recorded
        into a field nothing read."""
        out = blockers(collector, {"error": "URLError Connection refused"})
        assert out and "reconciliation failed" in out[0]

    def test_a_disagreeing_cross_check_blocks(self, collector):
        out = blockers(collector, {"ready_matches": False})
        assert out and "DISAGREES" in out[0]

    def test_an_absent_result_blocks(self, collector):
        assert blockers(collector, None)

    def test_a_result_with_no_comparison_blocks(self, collector):
        """A dict that carries neither an error nor a verdict is not agreement."""
        assert blockers(collector, {"successful_cycles": 25})


class TestAgreementOpensTheGate:
    def test_an_agreeing_cross_check_produces_no_blockers(self, collector):
        # Deltas included: agreement now means the two implementations
        # reported the SAME NUMBERS, not merely the same verdict.
        assert blockers(collector, {"ready_matches": True,
                                    "successful_cycles_delta": 0,
                                    "relevant_misses_delta": 0}) == []


class TestNetworking:
    def test_the_collector_joins_the_app_network(self, collector):
        """REGRESSION. Without --network the helper container talked to its OWN
        loopback. Verified 2026-08-01: 127.0.0.1:9721 -> connection refused;
        --network proxy + scanhound:9721 -> HTTP 200."""
        assert collector.APP_NETWORK == "proxy"

    def test_the_base_url_addresses_the_service_by_name(self, collector):
        """ScanHound publishes no host port, so no 127.0.0.1 address could ever
        have worked — publishing one would not have fixed it either."""
        assert "127.0.0.1" not in collector.BASE_URL
        assert collector.BASE_URL == "http://scanhound:9721"


class TestGateFieldsMustAgreeNotJustTheVerdict:
    """Peer review round 11 (Q2).

    The deltas were computed and logged and never enforced, so this passed:

        app.relevant_misses    = 0
        mirror.relevant_misses = 3
        both ready             = False
        -> ready_matches True, relevant_misses_delta -3, no blocker

    Two views reporting different numbers while agreeing on the verdict is not
    corroboration, it is both happening to say no.
    """

    def test_a_numeric_disagreement_blocks_even_when_the_verdict_matches(self, collector):
        out = blockers(collector, {"ready_matches": True,
                                   "successful_cycles_delta": 0,
                                   "relevant_misses_delta": -3})
        assert out and "relevant_misses" in out[0]

    def test_a_cycle_count_disagreement_blocks_too(self, collector):
        out = blockers(collector, {"ready_matches": True,
                                   "successful_cycles_delta": 2,
                                   "relevant_misses_delta": 0})
        assert out and "successful_cycles" in out[0]

    def test_a_missing_delta_is_not_agreement(self, collector):
        """An older evidence script that never emitted a delta must not
        silently satisfy a check it did not run."""
        out = blockers(collector, {"ready_matches": True,
                                   "successful_cycles_delta": 0})
        assert out and "relevant_misses_delta" in out[0]

    def test_full_agreement_still_opens_the_gate(self, collector):
        """POSITIVE CONTROL. Without it, 'always block' would satisfy all three
        assertions above."""
        assert blockers(collector, {"ready_matches": True,
                                    "successful_cycles_delta": 0,
                                    "relevant_misses_delta": 0}) == []
