"""A stalled JDownloader poll must never again be silent.

2026-08-15: JDownloader's process restarted at 21:36. ScanHound's cached device
handle went stale; the failing poll invalidated the cache correctly, but every
subsequent failed RECONNECT hit

    try:
        device = self._connect_jd_device()
    except Exception:
        return []

which logged nothing, and the surrounding poller loop logs at debug (off in
production). The poller kept running and failing invisibly for ~15 hours --
ONE warning in the entire log -- until the container was restarted. The
downloads list simply stopped changing, which is how it was eventually noticed.

The fix tracks the ABSENCE OF SUCCESS rather than any particular failure.
Enumerating failure modes guarantees missing the next one; "nothing has
succeeded for N minutes" catches stale handles, JD being down, network drops,
expired credentials and whatever comes next.

These tests pin the three properties that make a stall detectable:
its failures are LOGGED, its liveness is READABLE, and recovery RESETS it.
"""
import logging
from unittest.mock import MagicMock

import pytest

from backend.download_service import DownloadService


@pytest.fixture
def svc():
    s = DownloadService.__new__(DownloadService)   # no external deps needed
    import threading
    s._jd_lock = threading.Lock()
    s._jd = None
    s._jd_device = None
    s._jd_conn_ts = 0.0
    s._JD_CONN_TTL = 90.0
    s._jd_poll_lock = threading.Lock()
    s._jd_last_poll_ok_ts = None
    s._jd_last_poll_ok_wall = None
    s._jd_poll_fail_streak = 0
    s._jd_last_poll_error = None
    s._jd_last_log_ts = 0.0
    s._JD_LOG_EVERY = 300.0
    s._jd_phase = "idle"
    s._poll_iters_started = 0
    s._poll_iters_completed = 0
    s._poll_iter_start_ts = None
    s._cycles_started = 0
    s._cycles_completed = 0
    s._cycle_start_ts = None
    s._cycle_end_ts = None
    return s


class TestFailuresAreVisible:
    def test_a_connect_failure_is_LOGGED(self, svc, caplog):
        """The exact hole: this path used to produce no output at all."""
        with caplog.at_level(logging.WARNING, logger="backend.download_service"):
            svc._note_poll_failure(RuntimeError("No connection established"))
        # getMessage() renders the args; r.message is only the format string,
        # so asserting on it would pass even if the cause were never included.
        assert any("No connection established" in r.getMessage()
                   for r in caplog.records), caplog.text

    def test_repeat_failures_are_rate_limited_not_silenced(self, svc, caplog):
        """Both failure modes are bugs: a flood buries the log, silence hid a
        15-hour outage. One line per window, and the streak count carries the
        rest of the story."""
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="backend.download_service"):
            for _ in range(50):
                svc._note_poll_failure(RuntimeError("boom"))
        assert len(caplog.records) == 1, "expected exactly one log inside the window"
        assert svc.jd_poll_health()["consecutive_failures"] == 50

    def test_the_window_reopens(self, svc, caplog):
        """A persistent outage must keep leaving a trail, not go quiet after one."""
        svc._note_poll_failure(RuntimeError("boom"))
        svc._jd_last_log_ts -= (svc._JD_LOG_EVERY + 1)     # simulate elapsed time
        caplog.clear()          # caplog is active before at_level; drop the first
        with caplog.at_level(logging.WARNING, logger="backend.download_service"):
            svc._note_poll_failure(RuntimeError("boom"))
        assert len(caplog.records) == 1


class TestLivenessIsReadable:
    def test_no_success_yet_reports_None_not_zero(self, svc):
        """None means 'never succeeded'; 0 would read as 'succeeded just now'.

        A watcher comparing stalled_seconds against a threshold must not treat
        a poller that has NEVER worked as perfectly healthy.
        """
        h = svc.jd_poll_health()
        assert h["stalled_seconds"] is None
        assert h["last_success_at"] is None

    def test_success_records_a_timestamp(self, svc):
        svc._note_poll_success()
        h = svc.jd_poll_health()
        assert h["last_success_at"] is not None
        assert h["stalled_seconds"] is not None and h["stalled_seconds"] < 5
        assert h["consecutive_failures"] == 0

    def test_stall_time_GROWS_after_the_last_success(self, svc):
        svc._note_poll_success()
        svc._jd_last_poll_ok_ts -= 3600            # an hour ago
        assert svc.jd_poll_health()["stalled_seconds"] >= 3600

    def test_the_last_error_is_kept_for_diagnosis(self, svc):
        svc._note_poll_failure(RuntimeError("No connection established"))
        assert "No connection established" in svc.jd_poll_health()["last_error"]


class TestRecovery:
    def test_success_CLEARS_the_streak_and_the_error(self, svc):
        """Control: without this the fix would report a permanent outage after
        one blip, and a watcher keyed on it would alert forever."""
        for _ in range(5):
            svc._note_poll_failure(RuntimeError("boom"))
        assert svc.jd_poll_health()["consecutive_failures"] == 5

        svc._note_poll_success()

        h = svc.jd_poll_health()
        assert h["consecutive_failures"] == 0
        assert h["last_error"] is None

    def test_recovery_is_announced(self, svc, caplog):
        svc._note_poll_failure(RuntimeError("boom"))
        with caplog.at_level(logging.INFO, logger="backend.download_service"):
            svc._note_poll_success()
        assert any("recovered" in r.message for r in caplog.records), caplog.text

    def test_a_clean_success_does_not_spam_recovery(self, svc, caplog):
        with caplog.at_level(logging.INFO, logger="backend.download_service"):
            svc._note_poll_success()
            svc._note_poll_success()
        assert not [r for r in caplog.records if "recovered" in r.message]


class TestProductionWiring:
    """The helpers are useless unless poll_results ACTUALLY calls them.

    Peer review 2026-08-15 (LOW): every test above drives _note_poll_failure /
    _note_poll_success / jd_poll_health directly, so deleting those calls from
    poll_results() would leave the whole class green while liveness silently
    stopped being recorded -- the same consumer-vs-component gap that produced
    the outage being fixed here.
    """

    def _svc(self, svc):
        """Give the bare instance the few attributes poll_results touches."""
        svc.db = None
        svc.config = {}
        svc._results_cache = {}
        svc._log = lambda *a, **k: None
        # The rest of poll_results' collaborators. Stubbed rather than mocked
        # wholesale so the REAL control flow runs -- a MagicMock service would
        # make the liveness calls unobservable, which is the whole point here.
        svc._best_titles = {}          # dict, per __init__
        svc._uuid_id = {}              # dict, per __init__
        svc._scraped_titles_normalized = lambda: {}   # method
        svc._resolve_title = lambda *a, **k: ""       # staticmethod
        return svc

    def test_a_failed_CONNECT_increments_the_failure_state(self, svc):
        svc = self._svc(svc)
        svc._connect_jd_device = MagicMock(side_effect=RuntimeError("No connection established"))

        assert svc.poll_results(record=False) == []

        h = svc.jd_poll_health()
        assert h["consecutive_failures"] == 1
        assert "No connection established" in (h["last_error"] or "")

    def test_a_failed_PACKAGE_QUERY_increments_and_invalidates(self, svc):
        svc = self._svc(svc)
        device = MagicMock()
        device.downloads.query_packages.side_effect = RuntimeError("boom")
        svc._connect_jd_device = MagicMock(return_value=device)
        svc._jd_device = device                      # pretend a cache exists

        assert svc.poll_results(record=False) == []

        assert svc.jd_poll_health()["consecutive_failures"] == 1
        assert svc._jd_device is None, "the stale handle must be dropped"

    def test_a_SUCCESSFUL_query_records_liveness(self, svc):
        """Control: without this, a fix that only ever counts failures passes."""
        svc = self._svc(svc)
        device = MagicMock()
        device.downloads.query_packages.return_value = []
        device.downloads.query_links.return_value = []
        svc._connect_jd_device = MagicMock(return_value=device)

        svc.poll_results(record=False)

        h = svc.jd_poll_health()
        assert h["consecutive_failures"] == 0
        assert h["last_success_at"] is not None
        assert h["stalled_seconds"] is not None


class TestFailurePhase:
    """Which STEP failed, so a recurrence is attributable.

    Peer review 2026-08-15: the silence is fixed but the CAUSE of the 15-hour
    stall is still unproven, because one generic exception cannot say whether
    MyJDownloader auth, the device listing, or the package query is what kept
    failing.
    """

    def test_a_connect_failure_names_the_connect_phase(self, svc):
        svc._jd_phase = "connect"
        svc._note_poll_failure(RuntimeError("401"))
        assert svc.jd_poll_health()["failure_phase"] == "connect"

    def test_a_package_query_failure_names_that_phase(self, svc):
        svc._jd_phase = "query_packages"
        svc._note_poll_failure(RuntimeError("boom"))
        assert svc.jd_poll_health()["failure_phase"] == "query_packages"

    def test_the_phase_is_LOGGED_not_just_stored(self, svc, caplog):
        svc._jd_phase = "update_devices"
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="backend.download_service"):
            svc._note_poll_failure(RuntimeError("boom"))
        assert "update_devices" in caplog.records[0].getMessage()

    def test_a_healthy_poller_reports_no_phase(self, svc):
        """Control: a stale phase from an old failure must not read as current."""
        svc._jd_phase = "connect"
        svc._note_poll_failure(RuntimeError("boom"))
        svc._note_poll_success()
        assert svc.jd_poll_health()["failure_phase"] is None


class TestHeartbeat:
    """Was the poller cycling at all? Nothing recorded that before.

    Peer review 2026-08-15 named this THE unanswered question about the
    15-hour stall. "One warning then silence" is equally consistent with
    cycling-and-failing-silently, blocked-inside-one-call, and
    thread-stopped -- and those want different fixes, so backoff alone would
    have been built on an unproven cause. failure_phase cannot separate them
    either: it comes from an EXCEPTION, and a blocked call raises nothing.
    """

    def test_a_completed_iteration_advances_BOTH_counters(self, svc):
        svc.note_poll_iteration_start()
        svc.note_poll_iteration_end()
        h = svc.jd_poll_health()
        assert h["iterations_started"] == 1
        assert h["iterations_completed"] == 1
        assert h["current_iteration_seconds"] is None

    def test_an_IN_FLIGHT_iteration_is_visible_as_such(self, svc):
        """The blocked case: started but never completed, and the age grows."""
        svc.note_poll_iteration_start()
        h = svc.jd_poll_health()
        assert h["iterations_started"] == 1
        assert h["iterations_completed"] == 0
        assert h["current_iteration_seconds"] is not None

    def test_a_stuck_iteration_shows_a_GROWING_age(self, svc):
        svc.note_poll_iteration_start()
        svc._poll_iter_start_ts -= 3600
        assert svc.jd_poll_health()["current_iteration_seconds"] >= 3600

    def test_poll_results_completes_the_heartbeat_even_when_it_FAILS(self, svc):
        """The wiring pin: a failing poll must still close its iteration, or a
        fast-failing poller would be misread as blocked."""
        svc.db = None
        svc.config = {}
        svc._results_cache = {}
        svc._log = lambda *a, **k: None
        svc._connect_jd_device = MagicMock(side_effect=RuntimeError("boom"))

        svc.poll_results(record=False)

        h = svc.jd_poll_health()
        assert h["iterations_started"] == 1 and h["iterations_completed"] == 1
        assert h["consecutive_failures"] == 1

    def test_the_three_states_are_distinguishable(self, svc):
        """The whole point, stated as the operator would read it."""
        # cycling: both advance together
        for _ in range(3):
            svc.note_poll_iteration_start(); svc.note_poll_iteration_end()
        h = svc.jd_poll_health()
        assert h["iterations_started"] == h["iterations_completed"] == 3

        # blocked: started runs ahead and stays there
        svc.note_poll_iteration_start()
        h = svc.jd_poll_health()
        assert h["iterations_started"] - h["iterations_completed"] == 1
        assert h["current_iteration_seconds"] is not None


class TestOuterCycleHeartbeat:
    """The PRIMARY heartbeat, per design review P1-1.

    Wrapping poll_results alone cannot answer "is the thread cycling?". The
    loop does more after poll_results returns -- source-link annotation, the
    WebSocket broadcast, the rename hand-off -- and a block in any of those
    leaves the INNER counters equal and stationary, which the documented table
    read as "thread stopped". The thread is alive and blocked outside the
    measured span: the same unsupported conclusion this instrumentation exists
    to prevent.
    """

    def test_a_block_AFTER_poll_results_is_visible_as_blocked(self, svc):
        """The exact case the inner span cannot see.

        poll_results completed cleanly -- its counters are equal -- yet the
        cycle is still open and ageing. Without the outer heartbeat this reads
        as a stopped thread.
        """
        svc.note_cycle_start()
        svc.note_poll_iteration_start()
        svc.note_poll_iteration_end()          # the poll finished fine
        svc._cycle_start_ts -= 3600            # then something after it blocked

        h = svc.jd_poll_health()

        assert h["iterations_started"] == h["iterations_completed"] == 1, \
            "the inner span looks perfectly healthy"
        assert h["cycles_started"] - h["cycles_completed"] == 1, \
            "but the outer cycle never closed"
        assert h["current_cycle_seconds"] >= 3600

    def test_a_completed_cycle_advances_both_and_starts_the_age_clock(self, svc):
        svc.note_cycle_start()
        svc.note_cycle_end()
        h = svc.jd_poll_health()
        assert h["cycles_started"] == h["cycles_completed"] == 1
        assert h["current_cycle_seconds"] is None
        assert h["seconds_since_cycle_completed"] is not None

    def test_a_STOPPED_thread_is_distinguishable_from_a_blocked_one(self, svc):
        """Equal counters carry no age on their own -- hence the clock.

        A single snapshot must separate a healthy idle poller from a dead one,
        because the host checker only ever takes one snapshot.
        """
        svc.note_cycle_start()
        svc.note_cycle_end()
        svc._cycle_end_ts -= 7200              # nothing has started since

        h = svc.jd_poll_health()

        assert h["cycles_started"] == h["cycles_completed"]     # not blocked
        assert h["current_cycle_seconds"] is None
        assert h["seconds_since_cycle_completed"] >= 7200       # but long dead

    def test_a_healthy_poller_reports_a_SMALL_age(self, svc):
        """Control: without this, 'stopped' would match a working poller."""
        svc.note_cycle_start()
        svc.note_cycle_end()
        assert svc.jd_poll_health()["seconds_since_cycle_completed"] < 5

    def test_a_never_started_poller_reports_None_not_zero(self, svc):
        h = svc.jd_poll_health()
        assert h["cycles_started"] == 0
        assert h["seconds_since_cycle_completed"] is None
        assert h["current_cycle_seconds"] is None
