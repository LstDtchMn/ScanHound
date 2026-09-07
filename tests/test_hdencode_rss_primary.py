"""Tests for RSS-primary traffic, readiness, and rollback semantics."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.background_scanner import BackgroundScanner
from backend.database import DatabaseManager
from backend.hdencode_rss_service import HDEncodeRSSService


class Scanner:
    def __init__(self):
        self.calls = []
        self._last_crawl_seen_urls = set()
        self._last_crawl_early_stopped = False
        self.scrapers = SimpleNamespace(_detail=None)

    def try_acquire_scan(self):
        return True

    def release_scan(self):
        return None

    def run_scan(self, **kwargs):
        self.calls.append(kwargs)
        return []

    def rematch_cache(self):
        return 0


class Db:
    def get_source_health(self):
        return {}

    def record_source_success(self, _source):
        return None

    def record_source_failure(self, *_args, **_kwargs):
        return None

    def recover_hdencode_hydration_queue(self):
        return 0

    def list_hdencode_candidates(self, **_kwargs):
        return []

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {
            "ready": True,
            "reasons": [],
            "successful_cycles": 20,
            "observed_days": 7,
        }

    def record_hdencode_shadow_comparison(self, **_kwargs):
        # Added by the RSS completion package; the shadow crawl persists a
        # comparison row after each listing cycle.
        return None

    def get_hdencode_feed_state(self, _feed_key):
        # Added by the readiness-corrections package: scan_once now probes
        # feed state before polling to decide restart_recovery. No preexisting
        # state -> restart_recovery is False, the correct default here (these
        # tests exercise listing/shadow behavior, not recovery).
        return None

    def get_background_cache_urls(self):
        return set()

    def touch_background_cache(self, _urls):
        return None

    def upsert_background_cache(self, _rows):
        return None

    def purge_background_cache(self, _days):
        return None

    def count_background_cache(self):
        return 0


class Backend:
    def save_config(self):
        return None


class Registry:
    def __init__(self, mode, *, fallback=False):
        self.config = {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode", "DDLBase"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": mode,
            "hdencode_rss_listing_fallback_enabled": fallback,
            "hdencode_rss_shadow_min_cycles": 20,
            "hdencode_rss_shadow_min_days": 7,
        }
        self.scanner = Scanner()
        self.db = Db()
        self.backend = Backend()
        self.lifespan_generation = 1

    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation


class _LedgerDb(Db):
    """Records what the SCAN actually books and grades."""

    def __init__(self):
        self.batches = []
        self.attempts = []

    def record_request_batch(self, mode, kind, requests, at=None):
        self.batches.append((mode, kind, requests))

    def record_canary_attempt(self, source_key, *, at, next_attempt_at,
                              outcome, reason=None):
        self.attempts.append((source_key, outcome, reason))

    def record_listing_membership(self, cycle_uuid, source_key, rows):
        return None

    def list_listing_membership(self, **_kwargs):
        return []

    def get_canary_state(self, _source_key):
        return {}

    def list_canary_states(self):
        return []

    def get_shadow_cycle_url_sets(self, **_kwargs):
        return {"cycles": [], "evidence_problems": []}


def test_a_poll_only_primary_cycle_books_its_requests(monkeypatch):
    """WIRING (review MEDIUM 1). A mutant that removed the poll-cost call from
    scan_once survived every test, because the tests exercised the helper
    directly and nothing proved scan_once calls it. These are the cheap cycles
    the hybrid's whole cost claim rests on."""
    reg = Registry("rss_primary")
    reg.db = _LedgerDb()
    _authorize_primary(monkeypatch)
    _canary_not_due(reg)
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {"mode": "rss_primary", "coverage_uncertain": False,
                                "fallback_qualified": False, "feeds": [],
                                "requests": 5},
    )

    BackgroundScanner(reg).scan_once()

    assert ("rss_primary", "rss_poll", 5) in reg.db.batches, (
        "the poll's own requests must be booked even when nothing else "
        "happened this cycle")


def test_the_qualification_crawl_actually_records_a_canary_attempt(monkeypatch):
    """WIRING (review HIGH 3). The branch that makes a first promotion possible
    is the one in scan_once; a test that passes canary_observation=True by hand
    cannot show that anything ever sets it."""
    reg = Registry("rss_shadow")
    reg.db = _LedgerDb()
    reg.config["hdencode_listing_membership_full_depth"] = True
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {"mode": "rss_shadow", "coverage_uncertain": False,
                                "fallback_qualified": False, "feeds": [],
                                "requests": 2},
    )

    BackgroundScanner(reg).scan_once()

    assert reg.db.attempts, (
        "the dense qualification crawl is a canary observation; without "
        "recording it, activation's freshness condition can never be met and "
        "there is no legitimate first promotion")


def _patch_candidate_service(monkeypatch):
    monkeypatch.setattr(
        "backend.hdencode_candidate_service."
        "HDEncodeCandidateService.classify_pending",
        lambda self, **kwargs: {"processed": 0, "states": {}},
    )
    monkeypatch.setattr(
        "backend.hdencode_candidate_service."
        "HDEncodeCandidateService.hydrate_pending",
        lambda self, *args, **kwargs: {
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        },
    )




def _authorize_primary(monkeypatch):
    """HDE-1 (2026-09-03): a persisted rss_primary is primary only when the
    shared authority says so, and until the coverage canary exists it never
    does. The tests below describe what an AUTHORIZED primary does, so they
    say so explicitly instead of relying on the old 'persisted == effective'.

    UPDATED 2026-09-06: the authority now answers two questions -- may primary
    be turned on (activation, which still requires shadow readiness) and is it
    in effect this cycle (runtime, which deliberately does not). What runs is
    decided by the runtime answer, so a helper that authorizes "primary" has
    to authorize both, or these tests would describe a primary that the
    scanner never actually enters.
    """
    monkeypatch.setattr(
        "backend.rss_primary_authority.evaluate_rss_primary_authority",
        lambda config, db: {
            "authorized": True, "blockers": [], "provisional": True,
            "readiness": {"ready": True},
            "state": "authorized",
            "canary": {"implemented": True, "last_success": None,
                       "age_seconds": None, "interval_seconds": None},
            "auto_demotion_armed": True,
        },
    )
    monkeypatch.setattr(
        "backend.rss_primary_authority.evaluate_runtime",
        lambda config, db: {
            "state": "authorized", "authorized": True, "blockers": [],
            "suspensions": [], "revocations": [],
            "record": {"at": "2026-09-06T00:00:00+00:00"},
            "contract_hash": "authorized-in-test",
        },
    )


def _canary_not_due(reg):
    """Give every canary source a future next-attempt time.

    Without any state the canary is DUE by design: unreadable or absent
    scheduling state must not let the protection clock age silently while the
    system still calls itself canary-protected.
    """
    reg.db.list_canary_states = lambda: [
        {"source_key": key,
         "next_attempt_at": (datetime.now(timezone.utc)
                             + timedelta(hours=4)).isoformat()}
        for key in _canary_state_keys(reg)
    ]


def _canary_state_keys(reg):
    """The keys canary state is actually stored under.

    CORRECTED 2026-09-07. These helpers keyed the state by the CONFIGURED
    source name ("4k"), which is not what the crawler writes ("hdencode:4k").
    Both sides of the boundary were wrong in the same direction, so the
    fixture agreed with the bug and these tests passed while the scheduler
    could never find a real row -- making the canary due on every cycle.
    """
    from backend.rss_primary_authority import contract_inputs, canary_source_key
    return [canary_source_key(s) for s in
            contract_inputs(reg.config)["hdencode_listing_canary_sources"]]


def _canary_due(reg):
    reg.db.list_canary_states = lambda: [
        {"source_key": key,
         "next_attempt_at": (datetime.now(timezone.utc)
                             - timedelta(hours=1)).isoformat()}
        for key in _canary_state_keys(reg)
    ]


def test_primary_runs_no_ordinary_listing_when_the_canary_is_not_due(monkeypatch):
    """MIGRATED 2026-09-06 from test_primary_never_runs_ordinary_hdencode_listing.

    Under the hybrid "primary never crawls the listing" is no longer true, and
    must not be: a reduced-frequency canary keeps crawling so coverage gaps
    stay observable after promotion. What survives, and is asserted here, is
    the claim the old test existed for -- primary does not run the ORDINARY
    discovery crawl. Between canaries it crawls nothing at all.
    """
    reg = Registry("rss_primary")
    _authorize_primary(monkeypatch)
    _canary_not_due(reg)
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_primary",
            "coverage_uncertain": False,
            "fallback_qualified": False,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    source_types = [call["source_type"] for call in reg.scanner.calls]
    assert "HDEncode" not in source_types
    assert "DDLBase" in source_types


def test_a_due_canary_crawls_the_listing_at_its_own_depth(monkeypatch):
    """The change the hybrid exists for: promotion no longer stops the listing.

    Before this, promoting to primary skipped the HDEncode listing entirely,
    which also stopped the comparison that produces the readiness evidence --
    the gate opened on evidence its own promoted mode destroyed.
    """
    reg = Registry("rss_primary")
    _authorize_primary(monkeypatch)
    _canary_due(reg)
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_primary",
            "coverage_uncertain": False,
            "fallback_qualified": False,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    hdencode = [c for c in reg.scanner.calls if c["source_type"] == "HDEncode"]
    assert len(hdencode) == 1, "the canary crawls once"
    from backend.rss_primary_authority import contract_inputs
    assert hdencode[0]["pages"] == contract_inputs(
        reg.config)["hdencode_listing_canary_pages"]
    assert hdencode[0]["early_stop"] is False, (
        "a canary claims a depth, so it must traverse it rather than stopping "
        "at the first page with nothing new")


def test_one_crawl_serves_both_a_due_canary_and_a_qualified_fallback(monkeypatch):
    """Two reasons to crawl must not become two crawls.

    The canary's depth strictly covers the fallback's single page, so the
    cycle crawls once at canary depth and still reports that the listing
    fallback acquired for it.
    """
    reg = Registry("rss_primary", fallback=True)
    _authorize_primary(monkeypatch)
    _canary_due(reg)
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_primary",
            "coverage_uncertain": True,
            "fallback_qualified": True,
            "feeds": [],
        },
    )

    scanner = BackgroundScanner(reg)
    scanner.scan_once()

    hdencode = [c for c in reg.scanner.calls if c["source_type"] == "HDEncode"]
    assert len(hdencode) == 1, "one crawl, not one per reason"
    from backend.rss_primary_authority import contract_inputs
    assert hdencode[0]["pages"] == contract_inputs(
        reg.config)["hdencode_listing_canary_pages"]
    # The cycle summary is where the fallback flag is published; scan_once's
    # own return is the per-source count only.
    rss_cycle = (scanner.last_run or {}).get("rss") or {}
    assert rss_cycle.get("listing_fallback_started") is True
    assert rss_cycle.get("canary_run") is True


def test_shadow_keeps_listing_comparison(monkeypatch):
    reg = Registry("rss_shadow")
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_shadow",
            "coverage_uncertain": False,
            "fallback_qualified": False,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    source_types = [call["source_type"] for call in reg.scanner.calls]
    assert "HDEncode" in source_types
    assert "DDLBase" in source_types


def test_primary_fallback_is_one_page_and_explicit(monkeypatch):
    """UPDATED 2026-09-06: the canary is pinned NOT due here, so this still
    describes the transient fallback on its own. When both apply, one crawl
    serves both at canary depth -- see the test above."""
    reg = Registry("rss_primary", fallback=True)
    _authorize_primary(monkeypatch)
    _canary_not_due(reg)
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_primary",
            "coverage_uncertain": True,
            "fallback_qualified": True,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    hdencode = [
        call for call in reg.scanner.calls
        if call["source_type"] == "HDEncode"
    ]
    assert len(hdencode) == 1
    assert hdencode[0]["pages"] == 1


def test_listing_mode_is_one_setting_rollback(monkeypatch):
    reg = Registry("listing")
    called = []
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: called.append(True),
    )

    BackgroundScanner(reg).scan_once()

    assert called == []
    assert any(
        call["source_type"] == "HDEncode"
        for call in reg.scanner.calls
    )


def _authorize_runtime(monkeypatch):
    """Make the runtime authority say primary is in effect this cycle."""
    monkeypatch.setattr(
        "backend.rss_primary_authority.evaluate_runtime",
        lambda config, db: {
            "state": "authorized", "authorized": True, "blockers": [],
            "suspensions": [], "revocations": [],
            "record": {"at": "2026-09-06T00:00:00+00:00"},
            "contract_hash": "authorized-in-test",
        },
    )


class _NotReadyButPolling(Db):
    """Readiness says no; the feeds are fresh, so nothing needs fetching."""

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {"ready": False, "reasons": ["miss_resolution_pending"],
                "successful_cycles": 40, "observed_days": 30}

    def get_hdencode_feed_state(self, _feed_key):
        return {"last_checked_at": datetime.now(timezone.utc).isoformat()}

    def list_hdencode_current_feed_urls(self):
        return []


def test_an_authorized_primary_keeps_polling_when_raw_readiness_is_false(monkeypatch):
    """The inverse of the migrated readiness tests, and the one that was
    missing (PR #116 review, PR1-R2).

    Readiness blocks on not_yet_assessable rows, and after promotion the canary
    is what resolves them. If the poll re-asked raw readiness, one ordinary
    pending miss would stop RSS polling altogether -- reinstating the conjunct
    the activation/runtime split removed, at a different consumer.
    """
    _authorize_runtime(monkeypatch)
    config = {
        "hdencode_enabled": True,
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_rss_shadow_min_cycles": 20,
        "hdencode_rss_shadow_min_days": 7,
    }
    service = HDEncodeRSSService(
        config, _NotReadyButPolling(),
        client=SimpleNamespace(fetch=lambda *_a, **_k: None),
    )
    cycle = service.poll_cycle()

    assert cycle["mode"] == "rss_primary", "the runtime authorized primary for this cycle"
    assert cycle.get("reason") != "primary_not_ready"
    assert not cycle.get("skipped"), "an authorized primary must not skip its own poll"
    assert cycle["readiness"]["ready"] is False, (
        "readiness is still reported, as diagnostic information")


def test_fallback_can_qualify_for_an_authorized_primary_with_readiness_false(monkeypatch):
    """The transient listing fallback must not be suppressed by raw readiness
    either: that would remove the recovery path exactly when a feed is failing."""
    _authorize_runtime(monkeypatch)
    monkeypatch.setattr(
        HDEncodeRSSService, "poll_feed",
        lambda self, feed, **_k: {"feed": feed.key, "outcome": "failed",
                                  "requested": True},
    )
    config = {
        "hdencode_enabled": True,
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_rss_listing_fallback_enabled": True,
        "hdencode_rss_shadow_min_cycles": 20,
        "hdencode_rss_shadow_min_days": 7,
    }

    class _Due(_NotReadyButPolling):
        def get_hdencode_feed_state(self, _feed_key):
            return {"last_checked_at": (datetime.now(timezone.utc)
                                        - timedelta(days=2)).isoformat()}

    service = HDEncodeRSSService(
        config, _Due(), client=SimpleNamespace(fetch=lambda *_a, **_k: None),
    )
    cycle = service.poll_cycle()

    assert cycle["coverage_uncertain"] is True
    assert cycle["fallback_qualified"] is True, (
        "a failing normal feed must still qualify the listing fallback when the "
        "runtime authorized primary, whatever raw readiness says")


def test_primary_service_refuses_before_shadow_gate():
    class NotReadyDb(Db):
        def get_hdencode_rss_readiness(self, **_kwargs):
            return {
                "ready": False,
                "reasons": ["insufficient_days"],
                "successful_cycles": 20,
                "observed_days": 2,
            }

    NotReadyDb.list_hdencode_feed_states = lambda self: []
    config = {
        "hdencode_enabled": True,
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_rss_shadow_min_cycles": 20,
        "hdencode_rss_shadow_min_days": 7,
    }
    # HDE-1: an unauthorized primary is no longer skipped at the poll; it
    # RUNS AS SHADOW, which acquires nothing and keeps every observation
    # flowing.
    #
    # UPDATED 2026-09-06 (design review RHC-1). Readiness is now an ACTIVATION
    # condition, not a runtime one: it must stop a promotion being made, and
    # must not stop a system already promoted, because after promotion the
    # canary keeps producing the comparison that resolves a pending row. So
    # the safety claim this test exists for -- a not-ready shadow never runs
    # as primary -- is asserted on the effective mode, and readiness is
    # asserted where it now lives.
    from backend.rss_primary_authority import (
        BLOCKER_NOT_READY, effective_discovery_mode, evaluate_activation,
    )
    effective, authority = effective_discovery_mode(config, NotReadyDb())
    assert effective == "rss_shadow"
    assert authority["blockers"], "the runtime must say why it refused"
    assert BLOCKER_NOT_READY in evaluate_activation(config, NotReadyDb(), None)["blockers"]
    service = HDEncodeRSSService(
        config, NotReadyDb(),
        client=SimpleNamespace(fetch=lambda *_args, **_kwargs: None),
    )
    status = service.status()
    assert status["mode"] == "rss_primary"          # what is persisted
    assert status["effective_mode"] == "rss_shadow"  # what runs


def test_readiness_requires_cycles_days_and_two_healthy_normal_feeds(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    now = datetime.now(timezone.utc)
    # The RSS completion readiness gate reads hdencode_shadow_cycles (via
    # get_hdencode_shadow_summary), not hdencode_ingest_cycles: it requires >=20
    # complete comparison cycles spanning >=7 days, zero relevant misses, proven
    # request reduction (listing_requests > rss_requests), and >=1 restart/catchup
    # recovery cycle.
    for index in range(20):
        completed = (now - timedelta(days=8) + timedelta(days=index * 0.4)).isoformat()
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO hdencode_shadow_cycles (
                    cycle_uuid, started_at, completed_at, normal_feeds_complete,
                    rss_requests, listing_requests, rss_count, listing_count,
                    duplicate_count, feed_only_count, listing_only_count,
                    relevant_miss_count, request_reduction_pct, catchup_used,
                    restart_recovery, outcome
                ) VALUES (?, ?, ?, 1, 1, 2, 5, 5, 5, 0, 0, 0, 50.0, ?, ?, 'success')
                """,
                (
                    f"cycle-{index}",
                    completed,
                    completed,
                    1 if index == 0 else 0,
                    1 if index == 1 else 0,
                ),
            )
    with db.transaction() as conn:
        for feed in ("movies_all", "tv_all"):
            conn.execute(
                """
                INSERT INTO hdencode_feed_state (
                    feed_key, feed_url, last_checked_at, last_status,
                    consecutive_failures
                ) VALUES (?, ?, ?, 304, 0)
                """,
                (
                    feed,
                    f"https://hdencode.org/{feed}/",
                    now.isoformat(),
                ),
            )

    readiness = db.get_hdencode_rss_readiness(
        min_cycles=20,
        min_days=7,
    )
    assert readiness["ready"] is True
    assert readiness["normal_feeds_healthy"] is True

    with db.transaction() as conn:
        conn.execute(
            "UPDATE hdencode_feed_state SET consecutive_failures = 1 "
            "WHERE feed_key = 'tv_all'"
        )
    readiness = db.get_hdencode_rss_readiness(
        min_cycles=20,
        min_days=7,
    )
    assert readiness["ready"] is False
    assert "normal_feeds_unhealthy_or_stale" in readiness["reasons"]
