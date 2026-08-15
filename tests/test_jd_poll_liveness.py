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
