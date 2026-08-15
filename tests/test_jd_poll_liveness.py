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
