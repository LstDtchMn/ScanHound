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


def blockers(collector, app, *, missing_credentials=False):
    return collector.reconciliation_blockers(
        app, missing_credentials=missing_credentials)


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
        assert blockers(collector, {"ready_matches": True}) == []


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
