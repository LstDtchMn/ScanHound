"""R-6 boundary tests: the programme gate at the ratified capabilities.

Wiring tests, not pure-gate tests (backend/promotion_gate has its own suite):
each asserts that a FLIPPED CONFIG FLAG without a recorded evidence pass
stays inert, and that a complete recorded pass opens the gate.
"""
import pytest

from backend.capability_gate import (
    BINDING_KEYS, PHASE_A_VERDICT_KEY, PHASE_B_VERDICT_KEY,
    capability_blockers)
from backend.release_grammar import GRAMMAR_VERSION


from tests.tools.gate_pass import full_pass_config as _full_pass_config


class TestTheGateItself:
    def test_empty_config_denies_with_reasons(self):
        blockers = capability_blockers({})
        assert blockers and "phase_b_verdict_absent" in blockers

    def test_a_complete_recorded_pass_opens_it(self):
        assert capability_blockers(_full_pass_config()) == ()

    def test_a_parser_bump_after_the_verdict_closes_it_again(self):
        cfg = _full_pass_config()
        cfg[PHASE_B_VERDICT_KEY]["parser_version"] = "release-grammar-v0-old"
        cfg[PHASE_A_VERDICT_KEY]["parser_version"] = "release-grammar-v0-old"
        assert capability_blockers(cfg) != ()


class TestAutoGrabBoundary:
    def test_enabled_flag_without_a_pass_queues_nothing(self):
        from backend.hdencode_action_service import HDEncodeActionService
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_rss_auto_grab_enabled": True}
        assert svc.queue_approved_auto_actions(limit=5) == []


class TestRenameBoundary:
    def test_enabled_flag_without_a_pass_processes_nothing(self, tmp_path):
        from backend.rename.service import RenameService
        svc = object.__new__(RenameService)
        # _cfg is a read-only property over the registry config
        import types as _types
        svc._reg = _types.SimpleNamespace(config={"auto_rename_enabled": True})
        svc.last_package_failed_db = 0
        assert svc.process_package("pkg", str(tmp_path)) == []


class TestCycleDemotionBoundary:
    def test_primary_mode_without_a_pass_runs_as_shadow(self):
        from backend.hdencode_rss_service import HDEncodeRSSService

        class Db:
            def get_hdencode_rss_readiness(self, **kw):
                return {"ready": True}
            def get_hdencode_feed_state(self, key):
                # every feed recently checked -> nothing due -> no network
                return {"last_checked_at": "2999-01-01T00:00:00+00:00"}
            def list_hdencode_current_feed_urls(self):
                return []

        svc = object.__new__(HDEncodeRSSService)
        svc.config = {"hdencode_enabled": True,
                      "hdencode_discovery_mode": "rss_primary"}
        svc.db = Db()

        class Coord:
            def snapshot(self):
                return {"blocked": False}
        svc.coordinator = Coord()
        svc._first_cycle = False
        svc._last_cycle = None
        cycle = svc.poll_cycle(include_catchup=False)
        assert cycle["mode"] == "rss_shadow"      # demoted, not skipped
        assert cycle.get("reason") != "primary_not_ready"
