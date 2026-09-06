"""rss_primary is refused until the coverage canary exists -- by ONE authority.

Round-7 review HDE-1, round-7b owner decision (2026-09-03). The accepted
decision record (2026-08-11, PR #61) says blind rss_primary is NO-GO: the
supported state is canary-protected provisional primary, and no canary exists.
The route used to allow primary on readiness alone, and a persisted
rss_primary bypassed even that. These tests pin: the route refuses; the
runtime refuses a persisted value the same way; both consult the same
function; /rss/status says which mode is requested, which is in effect and why.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend import rss_primary_authority as authority
from backend.api.routes import rss as rss_routes
from backend.background_scanner import BackgroundScanner


class _Scanner:
    def __init__(self):
        self.calls = []
        self._last_crawl_early_stopped = False

    def try_acquire_scan(self):
        return True

    def release_scan(self):
        pass

    def run_scan(self, **kwargs):
        self.calls.append(kwargs)
        return [], None

    def rematch_cache(self):
        return None


class _Db:
    ready = True

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {"ready": self.ready, "pending": 0}

    def get_source_health(self):
        return {}

    def record_source_success(self, _source):
        pass

    def record_source_failure(self, *_a, **_k):
        pass

    def recover_hdencode_hydration_queue(self):
        return 0

    def list_hdencode_candidates(self, **_k):
        return []

    def record_hdencode_shadow_comparison(self, **_k):
        pass

    def get_hdencode_feed_state(self, _k):
        return {"last_checked_at": "2026-09-01T00:00:00+00:00"}

    def get_background_cache_urls(self):
        return set()

    def touch_background_cache(self, _u):
        pass

    def upsert_background_cache(self, _r):
        pass

    def purge_background_cache(self, _d):
        return 0

    def count_background_cache(self):
        return 0

    def list_hdencode_feed_states(self):
        return []

    def list_hdencode_current_feed_urls(self):
        return []


class _Backend:
    """Stands in for AppService.

    Since 2026-09-06 a mode change that creates or removes a promotion is
    persisted through the strict writer and then committed into the shared
    config object, so the stub carries both halves and records what it was
    asked to do. ``fail_persist`` makes the durable write raise, which is how
    the fail-closed path is exercised.
    """

    def __init__(self, config=None):
        self.saved = 0
        self.persisted = []
        self.committed = []
        self.fail_persist = False
        self.config = config if config is not None else {}

    def save_config(self):
        self.saved += 1

    def persist_config_snapshot(self, candidate, must_contain=None):
        if self.fail_persist:
            raise RuntimeError("disk full")
        self.persisted.append((dict(candidate), dict(must_contain or {})))
        return dict(candidate)

    def commit_config_in_place(self, verified):
        self.committed.append(dict(verified))
        self.config.clear()
        self.config.update(verified)


class _Reg:
    def __init__(self, mode):
        self.config = {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode", "DDLBase"],
            "background_scan_pages": 1,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": mode,
            "hdencode_rss_listing_fallback_enabled": False,
            "hdencode_rss_shadow_min_cycles": 20,
            "hdencode_rss_shadow_min_days": 7,
        }
        self.scanner = _Scanner()
        self.db = _Db()
        # The backend shares the registry's dict, as production does
        # (backend/api/main.py:113), so an in-place commit is visible to both.
        self.backend = _Backend(self.config)
        self.lifespan_generation = 1
        self.background_scanner = None

    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation


def _quiet_candidate_service(monkeypatch):
    monkeypatch.setattr("backend.hdencode_candidate_service.HDEncodeCandidateService.classify_pending",
                        lambda self, **kw: {"processed": 0, "states": {}})
    monkeypatch.setattr("backend.hdencode_candidate_service.HDEncodeCandidateService.hydrate_pending",
                        lambda self, *a, **kw: {"claimed": 0, "completed": 0, "failed": 0, "cancelled": 0})
    monkeypatch.setattr("backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
                        lambda self, **kw: {"mode": "rss_shadow", "coverage_uncertain": False,
                                            "fallback_qualified": False, "feeds": []})


# ------------------------------------------------------------- the route --

def test_the_route_refuses_primary_even_when_readiness_is_green():
    reg = _Reg("listing")
    with pytest.raises(HTTPException) as exc:
        rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_primary"), reg)
    assert exc.value.status_code == 409
    assert authority.BLOCKER_NO_CANARY in exc.value.detail
    assert reg.config["hdencode_discovery_mode"] == "listing", "the refusal persisted the mode anyway"
    assert reg.backend.saved == 0


def test_the_route_still_allows_shadow_and_listing():
    reg = _Reg("listing")
    assert rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_shadow"), reg) == {"mode": "rss_shadow"}
    assert reg.config["hdencode_discovery_mode"] == "rss_shadow" and reg.backend.saved == 1


# ----------------------------------------------------------- the runtime --

def test_a_persisted_primary_runs_as_shadow_without_the_route_ever_being_called(monkeypatch):
    """The bypass the reviewer named: hdencode_discovery_mode already says
    rss_primary on disk. The runtime must not become blind primary."""
    reg = _Reg("rss_primary")
    effective, why = authority.effective_discovery_mode(reg.config, reg.db)
    assert effective == "rss_shadow"
    assert authority.BLOCKER_NO_CANARY in why["blockers"]

    _quiet_candidate_service(monkeypatch)
    BackgroundScanner(reg).scan_once()
    sources = [c["source_type"] for c in reg.scanner.calls]
    assert "HDEncode" in sources, "the listing crawl was skipped: the runtime treated a refused primary as primary"


def test_the_poll_cycle_reports_the_effective_mode():
    from backend.hdencode_rss_service import HDEncodeRSSService
    reg = _Reg("rss_primary")
    svc = HDEncodeRSSService(reg.config, reg.db)
    monkeypatch_free_status = svc.status()
    assert monkeypatch_free_status["mode"] == "rss_primary"
    assert monkeypatch_free_status["effective_mode"] == "rss_shadow"


# ------------------------------------------------------ the same authority --

def test_each_consumer_asks_the_authority_its_own_question(monkeypatch):
    """MIGRATED 2026-09-06 from test_route_and_runtime_consult_the_same_function.

    That test asserted the route and the runtime call the SAME function. The
    design review of 2026-09-05 (RHC-1) split the question in two on purpose:
    turning primary ON requires shadow readiness, while STAYING primary must
    not, or an ordinary canary sighting RSS has not carried yet would bounce a
    promoted system back to shadow before the next canary could resolve it.

    The intent is unchanged and is what this still enforces: neither consumer
    may carry a rule of its own. Swap the authority's answer to a consumer's
    question and that consumer follows it; swap the other one and it does not
    move, because it asks something else.
    """
    reg = _Reg("rss_primary")
    asked = []

    def activation_yes(config, db):
        asked.append("activation")
        return {"authorized": True, "blockers": [], "provisional": True,
                "readiness": {"ready": True}, "state": authority.STATE_AUTHORIZED,
                "canary": {"implemented": True, "last_success": None,
                           "age_seconds": None, "interval_seconds": None},
                "auto_demotion_armed": True}

    def runtime_yes(config, db):
        asked.append("runtime")
        return {"state": authority.STATE_AUTHORIZED, "authorized": True,
                "blockers": [], "suspensions": [], "revocations": [],
                "record": {"at": "now"}, "contract_hash": "x"}

    monkeypatch.setattr(authority, "evaluate_rss_primary_authority", activation_yes)
    monkeypatch.setattr(authority, "evaluate_runtime", runtime_yes)
    assert rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_primary"), reg) == {"mode": "rss_primary"}
    assert authority.effective_discovery_mode(reg.config, reg.db)[0] == "rss_primary"
    assert asked == ["activation", "runtime"], (
        "the route must ask activation and the runtime must ask runtime; "
        "got %r" % (asked,))

    def runtime_no(config, db):
        return {"state": authority.STATE_REVOKED, "authorized": False,
                "blockers": [authority.BLOCKER_CANARY_STALE],
                "suspensions": [], "revocations": [authority.BLOCKER_CANARY_STALE],
                "record": {"at": "now"}, "contract_hash": "x"}

    # The runtime refuses while activation would still say yes: the runtime
    # answer is the one that decides what actually runs.
    monkeypatch.setattr(authority, "evaluate_runtime", runtime_no)
    assert authority.effective_discovery_mode(reg.config, reg.db)[0] == "rss_shadow"

    def activation_no(config, db):
        return {"authorized": False, "blockers": [authority.BLOCKER_NOT_READY],
                "provisional": False, "readiness": {"ready": False},
                "state": authority.STATE_REVOKED,
                "canary": {"implemented": True, "last_success": None,
                           "age_seconds": None, "interval_seconds": None},
                "auto_demotion_armed": True}

    monkeypatch.setattr(authority, "evaluate_rss_primary_authority", activation_no)
    with pytest.raises(HTTPException):
        rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_primary"), reg)


def test_a_promotion_is_persisted_and_verified_before_it_is_visible(monkeypatch):
    """The fail-closed promotion write (design review R2-1 of 2026-09-06).

    save_config cannot be used for this: it serialises live state, preserves
    sensitive keys by mutating that live state, and swallows write errors, so
    a route calling it cannot learn that the promotion never reached disk.
    """
    reg = _Reg("rss_shadow")
    monkeypatch.setattr(
        authority, "evaluate_rss_primary_authority",
        lambda config, db: {"authorized": True, "blockers": [], "provisional": True,
                            "readiness": {"ready": True}, "state": authority.STATE_AUTHORIZED,
                            "canary": {"implemented": True, "last_success": None,
                                       "age_seconds": None, "interval_seconds": None},
                            "auto_demotion_armed": True})

    assert rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_primary"), reg) == {"mode": "rss_primary"}

    assert reg.backend.saved == 0, "the legacy writer must not be used for a promotion"
    assert len(reg.backend.persisted) == 1
    candidate, must_contain = reg.backend.persisted[0]
    record = candidate[authority.PROMOTION_KEY]
    assert record["canary_version"] == authority.CANARY_VERSION
    assert record["canary_contract_hash"] == authority.canary_contract_hash(reg.config)
    assert record["at"], "the promotion records when it was made"
    assert must_contain["hdencode_discovery_mode"] == "rss_primary"
    assert must_contain[authority.PROMOTION_KEY] == record, (
        "the durable write must be verified to contain the record, not just the mode")
    assert reg.config["hdencode_discovery_mode"] == "rss_primary"
    assert reg.config[authority.PROMOTION_KEY] == record


def test_a_promotion_that_cannot_be_saved_changes_nothing(monkeypatch):
    reg = _Reg("rss_shadow")
    reg.backend.fail_persist = True
    monkeypatch.setattr(
        authority, "evaluate_rss_primary_authority",
        lambda config, db: {"authorized": True, "blockers": [], "provisional": True,
                            "readiness": {"ready": True}, "state": authority.STATE_AUTHORIZED,
                            "canary": {"implemented": True, "last_success": None,
                                       "age_seconds": None, "interval_seconds": None},
                            "auto_demotion_armed": True})

    with pytest.raises(HTTPException) as raised:
        rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_primary"), reg)
    assert raised.value.status_code == 503
    assert reg.config["hdencode_discovery_mode"] == "rss_shadow", (
        "a failed durable write must never leave primary visible in memory")
    assert authority.PROMOTION_KEY not in reg.config
    assert reg.backend.committed == []


def test_leaving_primary_drops_the_promotion_so_returning_must_be_fresh():
    reg = _Reg("rss_primary")
    reg.config[authority.PROMOTION_KEY] = {
        "at": "2026-09-06T00:00:00+00:00", "by": "operator",
        "canary_version": authority.CANARY_VERSION,
        "canary_contract_hash": authority.canary_contract_hash(reg.config),
    }
    assert rss_routes.set_rss_mode(rss_routes.ModeRequest(mode="rss_shadow"), reg) == {"mode": "rss_shadow"}
    assert authority.PROMOTION_KEY not in reg.config
    candidate, _ = reg.backend.persisted[0]
    assert authority.PROMOTION_KEY not in candidate


def test_readiness_gates_activation_but_never_the_runtime():
    """The RHC-1 correction, pinned in both directions.

    A not-ready shadow must stop a promotion, and must NOT stop a system that
    is already promoted -- the canary keeps producing the comparison that
    resolves a pending row, so pending is a row to resolve, not a revocation.
    """
    db = _Db()
    db.ready = False
    assert authority.BLOCKER_NOT_READY in authority.evaluate_activation({}, db, None)["blockers"]

    promoted = {
        "hdencode_discovery_mode": "rss_primary",
        authority.PROMOTION_KEY: {
            "at": "2026-09-06T00:00:00+00:00", "by": "operator",
            "canary_version": authority.CANARY_VERSION,
            "canary_contract_hash": authority.canary_contract_hash({}),
        },
    }
    runtime = authority.evaluate_runtime(promoted, db)
    assert authority.BLOCKER_NOT_READY not in runtime["blockers"], (
        "shadow readiness must not be a runtime conjunct")


# ---------------------------------------------------------------- status --

def test_status_says_requested_effective_and_why():
    reg = _Reg("rss_primary")
    fields = authority.status_fields(reg.config, reg.db)
    assert fields["requested_mode"] == "rss_primary"
    assert fields["effective_mode"] == "rss_shadow"
    assert fields["primary_authorized"] is False
    assert authority.BLOCKER_NO_CANARY in fields["promotion_blockers"]
    for key in ("provisional", "canary_implemented", "canary_last_success", "canary_age_seconds",
                "canary_interval_seconds", "auto_demotion_armed"):
        assert key in fields


def test_the_authority_never_raises_and_treats_no_answer_as_a_blocker():
    class Broken(_Db):
        def get_hdencode_rss_readiness(self, **_k):
            raise RuntimeError("db gone")
    out = authority.evaluate_rss_primary_authority({}, Broken())
    assert out["authorized"] is False
    assert authority.BLOCKER_NO_DB in out["blockers"]
    out2 = authority.evaluate_rss_primary_authority({}, None)
    assert authority.BLOCKER_NO_DB in out2["blockers"]


def test_readiness_is_still_a_blocker_on_its_own():
    db = _Db()
    db.ready = False
    out = authority.evaluate_rss_primary_authority({}, db)
    assert authority.BLOCKER_NOT_READY in out["blockers"]
