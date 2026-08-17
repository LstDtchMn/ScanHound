"""Single-row removal must invalidate an in-flight poll, like the bulk path.

`remove_packages` (bulk) does its deletes, cache eviction and epoch advance
inside ONE critical section, so a concurrent `poll_results` either persists
BEFORE all of it — and the deletes remove what it wrote — or arrives afterwards,
sees a stale epoch, and declines to write.

`remove_package` (single row) did the delete and eviction with no lock and no
epoch advance, so it kept the exact race the bulk path was fixed for: a poll
holding a pre-delete snapshot writes the row straight back and the user watches
a removed download reappear.

Evicting the caches is NOT sufficient on its own, and that is the subtle part.
Eviction only stops the poll's unchanged-state SKIP branch; a poll that already
holds a snapshot still persists it. Only the epoch tells it not to.
"""
import threading
from unittest.mock import MagicMock

import pytest

from backend.download_service import DownloadService

ROW = {"id": 7, "package_uuid": 1007, "name": "Some.Movie.2024"}


class _JD:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.downloads = self

    def remove_links(self, links, packages):
        self.calls.append(list(packages))
        if self.fail:
            raise RuntimeError("JDownloader unreachable")


@pytest.fixture
def svc():
    s = DownloadService.__new__(DownloadService)
    s.db = MagicMock()
    s._results_cache = {}
    s._uuid_id = {}
    s._best_titles = {}
    s._log = lambda *a, **k: None
    s._invalidate_jd_cache = lambda: None
    s._jd_phase = None
    # The epoch state __init__ would have created.
    s._results_epoch = 0
    s._results_epoch_lock = threading.Lock()
    s.db.get_download_results.return_value = [ROW]
    s.db.delete_download_result.return_value = 1
    s._connect_jd_device = lambda: _JD()
    return s


def test_a_poll_that_started_before_the_removal_is_told_its_snapshot_is_stale(svc):
    """THE test. Without the epoch advance this passes vacuously and the row
    comes back on the next poll."""
    captured = svc._current_epoch()          # a poll begins, snapshot in hand

    svc.remove_package(7)                    # the user removes the row

    assert svc._epoch_is_current(captured) is False, (
        "the in-flight poll still believes its pre-removal snapshot is current, "
        "so it will write the deleted row straight back"
    )


def test_the_epoch_does_not_advance_when_nothing_was_removed(svc):
    """Negative control. A no-op removal must not invalidate every in-flight
    poll — that would turn a missing row into a spurious poll failure."""
    svc.db.delete_download_result.return_value = 0
    captured = svc._current_epoch()

    svc.remove_package(999)

    assert svc._epoch_is_current(captured) is True


def test_the_row_is_still_deleted_and_the_caches_still_evicted(svc):
    """The fix must not have moved the actual work out from under the lock."""
    svc._results_cache[ROW["package_uuid"]] = {"x": 1}
    svc._uuid_id[ROW["package_uuid"]] = 7
    svc._best_titles[ROW["name"]] = "T"

    out = svc.remove_package(7)

    assert out == {"ok": True, "removed": 1}
    svc.db.delete_download_result.assert_called_once_with(7)
    assert not svc._results_cache and not svc._uuid_id and not svc._best_titles


def test_jd_is_still_asked_to_remove_the_package(svc):
    """And the JD call stays OUTSIDE the lock — a wedged JDownloader must never
    hold the lock the poller needs. Asserted by the deadlock test below; here we
    only check the call still happens at all."""
    jd = _JD()
    svc._connect_jd_device = lambda: jd

    svc.remove_package(7)

    assert jd.calls == [[1007]], "JD must be told, with the native int64 uuid"


def test_an_unreachable_jd_still_clears_our_row_and_advances_the_epoch(svc):
    """Idempotent by design: the DB row goes regardless so the UI reflects the
    removal. The epoch must advance in that case too, or the poll resurrects it."""
    svc._connect_jd_device = lambda: _JD(fail=True)
    captured = svc._current_epoch()

    out = svc.remove_package(7)

    assert out["removed"] == 1
    assert svc._epoch_is_current(captured) is False


def test_it_does_not_deadlock_on_the_non_reentrant_lock(svc):
    """_epoch_lock() is a plain Lock, not an RLock, and _results_state() holds
    it. Bumping via _bump_epoch() inside that block would deadlock — the suite
    would HANG rather than fail, which is why this asserts completion explicitly
    instead of relying on the test simply finishing.
    """
    done = threading.Event()

    def run():
        svc.remove_package(7)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(timeout=5), "remove_package deadlocked on the epoch lock"
