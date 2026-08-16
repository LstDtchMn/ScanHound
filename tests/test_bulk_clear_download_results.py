"""Bulk removal of tracked download results.

The mobile "Clear done" button looped the single-row remove and awaited each:
with 578 rows (563 finished) that is 563 sequential HTTP requests, each of which
re-read every row server-side and made its own JDownloader round trip. No busy
state, no completion message -- so it read as a dead button.

The pre-existing `DELETE /download/results` is NOT the fix: it empties our table
and never tells JDownloader, so the next poll re-upserts every package JD still
holds. Removing them at the source is the whole job.
"""
from unittest.mock import MagicMock

import pytest

from backend.download_service import DownloadService


class _JD:
    """Records what was asked of JDownloader, so 'one call' is assertable."""

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
    s.db.delete_download_result.return_value = 1
    return s


def _rows(n, start=1):
    return [{"id": i, "package_uuid": str(1000 + i), "name": "pkg-%d" % i}
            for i in range(start, start + n)]


def _wire(svc, rows, jd=None):
    svc.db.get_download_results.return_value = rows
    jd = jd or _JD()
    svc._connect_jd_device = lambda: jd
    return jd


class TestOneCallNotN:
    def test_every_package_goes_to_JDownloader_in_a_SINGLE_call(self, svc):
        """The entire point. remove_links takes a LIST; looping it is what made
        the button take minutes."""
        rows = _rows(563)
        jd = _wire(svc, rows)
        svc.remove_packages([r["id"] for r in rows])
        assert len(jd.calls) == 1, "made %d JDownloader calls, not 1" % len(jd.calls)
        assert len(jd.calls[0]) == 563

    def test_the_rows_are_read_once_not_once_per_id(self, svc):
        """The single-row path re-read all 578 rows on every call -- O(n^2)."""
        rows = _rows(100)
        _wire(svc, rows)
        svc.remove_packages([r["id"] for r in rows])
        assert svc.db.get_download_results.call_count == 1

    def test_it_reports_what_actually_happened(self, svc):
        rows = _rows(5)
        _wire(svc, rows)
        out = svc.remove_packages([r["id"] for r in rows])
        assert out == {"ok": True, "removed": 5, "requested": 5}


class TestItRemovesFromTheSOURCEOfTruth:
    def test_the_package_uuids_are_the_ones_JD_knows(self, svc):
        rows = _rows(3)
        jd = _wire(svc, rows)
        svc.remove_packages([1, 2, 3])
        assert sorted(jd.calls[0]) == [1001, 1002, 1003]

    def test_ints_not_strings(self, svc):
        """JD expects the native int64; a string silently matches nothing."""
        jd = _wire(svc, _rows(2))
        svc.remove_packages([1, 2])
        assert all(isinstance(u, int) for u in jd.calls[0])

    def test_the_poller_caches_are_evicted(self, svc):
        """Without this an unchanged package still in JD hits the
        unchanged-state skip and re-emits the id we just deleted."""
        rows = _rows(2)
        _wire(svc, rows)
        for r in rows:
            svc._results_cache[r["package_uuid"]] = "x"
            svc._uuid_id[r["package_uuid"]] = r["id"]
            svc._best_titles[r["name"]] = "T"
        svc.remove_packages([1, 2])
        assert not svc._results_cache and not svc._uuid_id and not svc._best_titles


class TestItFailsHonestly:
    def test_an_unreachable_JD_still_clears_the_rows(self, svc):
        """Idempotent, exactly like the single-row path: the user asked for
        these to go, so the list must reflect that rather than silently keeping
        them."""
        rows = _rows(4)
        _wire(svc, rows, _JD(fail=True))
        out = svc.remove_packages([r["id"] for r in rows])
        assert out["removed"] == 4
        assert svc.db.delete_download_result.call_count == 4

    def test_an_empty_request_does_nothing_and_says_so(self, svc):
        jd = _wire(svc, _rows(3))
        out = svc.remove_packages([])
        assert out == {"ok": True, "removed": 0, "requested": 0}
        assert jd.calls == []

    def test_unknown_ids_are_ignored_not_fatal(self, svc):
        rows = _rows(2)
        jd = _wire(svc, rows)
        out = svc.remove_packages([1, 2, 9999])
        assert out["removed"] == 2
        assert sorted(jd.calls[0]) == [1001, 1002]

    def test_a_row_with_no_package_uuid_is_still_deleted(self, svc):
        """A DB row JD no longer knows about must not block the clear."""
        rows = [{"id": 1, "package_uuid": None, "name": "orphan"}]
        jd = _wire(svc, rows)
        out = svc.remove_packages([1])
        assert out["removed"] == 1
        assert jd.calls == [], "nothing to ask JD about"

    def test_an_unparsable_uuid_does_not_abort_the_batch(self, svc):
        rows = [{"id": 1, "package_uuid": "not-a-number", "name": "a"},
                {"id": 2, "package_uuid": "1002", "name": "b"}]
        jd = _wire(svc, rows)
        out = svc.remove_packages([1, 2])
        assert out["removed"] == 2
        assert jd.calls[0] == [1002]

    def test_a_DB_delete_failure_does_not_abort_the_rest(self, svc):
        rows = _rows(3)
        _wire(svc, rows)
        svc.db.delete_download_result.side_effect = [RuntimeError("locked"), 1, 1]
        out = svc.remove_packages([1, 2, 3])
        assert out["removed"] == 2, "one failure ended the whole clear"


class TestTheClearIsNotCosmetic:
    def test_it_does_NOT_use_the_table_only_clear(self, svc):
        """clear_download_results() deletes our rows and tells JDownloader
        nothing, so the next poll re-inserts everything. If this ever starts
        calling it, the button silently goes back to not working."""
        rows = _rows(3)
        _wire(svc, rows)
        svc.remove_packages([1, 2, 3])
        assert not svc.db.clear_download_results.called
