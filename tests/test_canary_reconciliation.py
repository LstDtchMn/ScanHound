"""Demoting a promoted system, and refusing to pretend it was demoted.

The reconciliation is the only thing that makes a revocation stick. Everything
here is about the difference between a pause and a durable finding, and about
never reporting a demotion that did not reach disk.
"""
from backend import rss_primary_authority as authority


class _Backend:
    """Stands in for AppService's strict writer."""

    def __init__(self, config, *, fail=False, keep_record=False):
        self.config = config
        self.fail = fail
        self.keep_record = keep_record
        self.persisted = []
        self.committed = []

    def persist_config_snapshot(self, candidate, must_contain=None):
        if self.fail:
            raise RuntimeError("disk full")
        self.persisted.append((dict(candidate), dict(must_contain or {})))
        written = dict(candidate)
        if self.keep_record:
            # A writer that did not actually drop the record: the caller must
            # notice rather than announce a demotion that did not happen.
            written[authority.PROMOTION_KEY] = {"at": "still here"}
        return written

    def commit_config_in_place(self, verified):
        self.committed.append(dict(verified))
        self.config.clear()
        self.config.update(verified)


class _Db:
    """Evidence a promoted primary would read. Revoking by default."""

    def __init__(self, *, stale=True, overlap_losses=0):
        self.stale = stale
        self.overlap_losses = overlap_losses

    def list_canary_states(self):
        return [{"source_key": "hdencode:4k",
                 "last_success_at": None if self.stale else _now(),
                 "consecutive_overlap_losses": self.overlap_losses}]

    def get_shadow_cycle_url_sets(self, **_kwargs):
        return {"cycles": [], "evidence_problems": []}

    def list_listing_membership(self, **_kwargs):
        return []

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {"ready": True, "reasons": []}


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _promoted():
    config = {
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_listing_canary_sources": ["hdencode:4k"],
        authority.EPOCH_KEY: "2026-09-01T00:00:00+00:00",
    }
    config[authority.PROMOTION_KEY] = {
        "at": "2026-09-06T00:00:00+00:00", "by": "operator",
        "canary_version": authority.CANARY_VERSION,
        "canary_contract_hash": authority.canary_contract_hash(config),
    }
    return config


def test_a_durable_finding_persists_shadow_and_deletes_the_promotion():
    config = _promoted()
    backend = _Backend(config)
    out = authority.reconcile_requested_primary(config, _Db(stale=True), backend)

    assert out["acted"] is True
    assert authority.BLOCKER_CANARY_STALE in out["reason"]
    written, must_contain = backend.persisted[0]
    assert written["hdencode_discovery_mode"] == "rss_shadow"
    assert authority.PROMOTION_KEY not in written, (
        "returning to primary must require a fresh explicit promotion")
    assert must_contain["hdencode_discovery_mode"] == "rss_shadow"
    assert config["hdencode_discovery_mode"] == "rss_shadow"
    assert authority.PROMOTION_KEY not in config
    assert config[authority.LAST_DEMOTION_KEY]["reason"], "it records WHY"


def test_a_suspension_changes_nothing_durable():
    """An outage is not a finding against the promotion. It stops primary
    running, and leaves the record for when the evidence can be read again."""
    config = _promoted()
    backend = _Backend(config)
    out = authority.reconcile_requested_primary(config, None, backend)

    assert out["acted"] is False
    assert backend.persisted == []
    assert config["hdencode_discovery_mode"] == "rss_primary"
    assert config[authority.PROMOTION_KEY], "the promotion survives a pause"


def test_it_runs_on_the_requested_mode_not_the_effective_one():
    """The distinction the whole function exists for. The runtime here has
    already dropped to shadow, so a reconciliation that only ran 'while
    primary' would never fire, and the record would survive to authorize
    primary again the moment the blocker cleared."""
    config = _promoted()
    db = _Db(stale=True)
    assert authority.effective_discovery_mode(config, db)[0] == "rss_shadow"

    backend = _Backend(config)
    out = authority.reconcile_requested_primary(config, db, backend)
    assert out["acted"] is True


def test_a_write_that_fails_reports_it_and_changes_nothing():
    config = _promoted()
    backend = _Backend(config, fail=True)
    out = authority.reconcile_requested_primary(config, _Db(stale=True), backend)

    assert out["acted"] is False
    assert out["persist_failed"] is True
    assert config["hdencode_discovery_mode"] == "rss_primary"
    assert backend.committed == []
    # Safe because the trigger is derived from evidence, not memory: the next
    # cycle reaches the same verdict and tries again.
    again = authority.reconcile_requested_primary(config, _Db(stale=True),
                                                  _Backend(config))
    assert again["acted"] is True


def test_a_writer_that_keeps_the_record_is_not_called_a_demotion():
    config = _promoted()
    backend = _Backend(config, keep_record=True)
    out = authority.reconcile_requested_primary(config, _Db(stale=True), backend)

    assert out["acted"] is False
    assert out["persist_failed"] is True
    assert backend.committed == [], (
        "a promotion still on disk after a demotion is not a demotion")


def test_nothing_happens_when_primary_was_never_requested():
    config = {"hdencode_discovery_mode": "rss_shadow"}
    backend = _Backend(config)
    out = authority.reconcile_requested_primary(config, _Db(), backend)
    assert out["acted"] is False
    assert backend.persisted == []


def test_a_healthy_promoted_system_is_left_alone():
    config = _promoted()
    backend = _Backend(config)
    out = authority.reconcile_requested_primary(
        config, _Db(stale=False), backend)
    assert out["acted"] is False
    assert config["hdencode_discovery_mode"] == "rss_primary"
    assert backend.persisted == []
