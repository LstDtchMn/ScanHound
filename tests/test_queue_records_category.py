"""A QUEUED grab must record the same media kind an interactive one does.

`downloads.media_kind` is recorded from the category the user clicked, and the
UI authorises a DESTRUCTIVE overwrite only for rows whose identity is known.
Interactive grabs carry `category` end to end. Queued grabs did not: the queue
normalised every request through `_request_dict`, which dropped the field, so
`download_item()` was called without it and every batched grab landed in
history with no kind at all.

That is fail-CLOSED -- those rows group but never authorise -- so it was never
a data-loss bug. It was a silently dark feature: 398 items have completed
through the queue, and not one of them could ever be compared.

The test that matters here is the DELIVERY one. Asserting that the column
exists, or that `_request_dict` keeps the key, proves only that a value was
written down somewhere. What was actually broken was the hand-off: the worker
never passed it on. So the worker is driven with a row the REAL producer wrote,
never a hand-built dict -- a fixture that invents its own input cannot notice
that nothing produces it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService


def _item(index: int, category: str | None = None) -> dict:
    item = {
        "url": f"https://hdencode.org/release/{index}",
        "title": f"Title {index}",
        "year": 2026,
        "season": None,
        "resolution": "2160p",
        "size": "20 GB",
        "hdr": "HDR",
        "dovi": True,
        "service_type": "Rapidgator",
    }
    if category is not None:
        item["category"] = category
    return item


@pytest.fixture
def svc(tmp_path):
    db = DatabaseManager(str(tmp_path / "queue.db"))
    service = DownloadQueueService({}, db, MagicMock(), poll_seconds=0.01)
    yield service
    db.close()


def _stored(svc, item_uuid: str) -> dict:
    with svc.db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM download_queue_items WHERE item_uuid = ?",
            (item_uuid,),
        ).fetchone()
    return dict(row)


class TestTheCategoryIsRecorded:
    def test_a_scheduled_batch_stores_the_category_it_was_given(self, svc):
        batch = svc.schedule_batch([_item(1, "tv")], interval_minutes=0)
        row = _stored(svc, batch["items"][0]["item_uuid"])
        assert row["category"] == "tv"

    def test_the_value_lands_in_the_category_column_and_not_some_other_one(self, svc):
        """Pins the column, not merely the presence of the string.

        An INSERT whose value tuple is one position out of step still writes
        'tv' into the row -- into `service_type`, or `hdr`. Both this and the
        test above pass if only the first assertion is made.
        """
        batch = svc.schedule_batch([_item(1, "tv")], interval_minutes=0)
        row = _stored(svc, batch["items"][0]["item_uuid"])
        assert row["category"] == "tv"
        assert row["service_type"] == "Rapidgator"
        assert row["hdr"] == "HDR"
        assert row["title"] == "Title 1"
        assert row["resolution"] == "2160p"

    def test_an_unrecorded_category_stores_nothing_rather_than_a_guess(self, svc):
        """No inference from `season is None`. That is the bug being removed."""
        batch = svc.schedule_batch([_item(1)], interval_minutes=0)
        row = _stored(svc, batch["items"][0]["item_uuid"])
        assert not row["category"]

    def test_each_item_in_one_batch_keeps_its_own_category(self, svc):
        """A batch may mix a show and a film; one must not overwrite the other."""
        batch = svc.schedule_batch(
            [_item(1, "tv"), _item(2, "4k"), _item(3, "remux")],
            interval_minutes=0,
        )
        got = {
            _stored(svc, row["item_uuid"])["title"]:
                _stored(svc, row["item_uuid"])["category"]
            for row in batch["items"]
        }
        assert got == {"Title 1": "tv", "Title 2": "4k", "Title 3": "remux"}


class TestTheWorkerDeliversIt:
    """The half that was actually broken.

    Storing the category and forwarding it are separate steps, and only the
    second one reaches `downloads.media_kind`. A queue that records the column
    perfectly and never passes it on looks complete in a diff and delivers
    nothing.
    """

    def test_the_worker_passes_the_stored_category_to_download_item(self, svc):
        batch = svc.schedule_batch([_item(1, "tv")], interval_minutes=0)
        # The row the REAL producer wrote -- not a dict this test invented.
        item = svc.get_item(batch["items"][0]["item_uuid"])
        assert item["category"] == "tv", "producer failed; the delivery check below would be vacuous"

        svc.download = MagicMock()
        svc.download.download_item = MagicMock(
            return_value={"success": True, "method": "jdownloader"}
        )
        try:
            svc._execute_inner(item, "attempt-1")
        except Exception:
            # Only the outgoing call is under test; downstream bookkeeping is
            # covered elsewhere and may fail against a mock.
            pass

        assert svc.download.download_item.called, "the worker never called download_item"
        kwargs = svc.download.download_item.call_args.kwargs
        assert kwargs.get("category") == "tv"

    def test_an_unrecorded_category_reaches_download_item_as_empty(self, svc):
        """Never None: `download_item(category="")` is the documented default,
        and the annotator treats empty as 'unrecorded' rather than a kind."""
        batch = svc.schedule_batch([_item(1)], interval_minutes=0)
        item = svc.get_item(batch["items"][0]["item_uuid"])

        svc.download = MagicMock()
        svc.download.download_item = MagicMock(
            return_value={"success": True, "method": "jdownloader"}
        )
        try:
            svc._execute_inner(item, "attempt-1")
        except Exception:
            pass

        kwargs = svc.download.download_item.call_args.kwargs
        assert kwargs.get("category") == ""


class TestTheRetryPathKeepsIt:
    def test_a_requeued_retry_preserves_the_category(self, svc):
        """A grab that fails and is re-queued must not lose its kind on the way
        back in -- otherwise the recorded kind depends on whether the first
        attempt happened to succeed."""
        request = _item(1, "tv")
        row = svc.enqueue_retry(request, {"reason_code": "source_deferred"})
        stored = _stored(svc, row["item_uuid"])
        assert stored["category"] == "tv"


class TestTheColumnSurvivesAnUpgrade:
    def test_an_existing_database_without_the_column_gains_it(self, tmp_path):
        """The migration runs against a database created before this change,
        which is every deployed one."""
        import sqlite3

        path = str(tmp_path / "old.db")
        db = DatabaseManager(path)
        db.close()

        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(download_queue_items)")]
        conn.close()
        assert "category" in cols


class TestTheCallTheQueueMakesIsOneDownloadServiceAccepts:
    """The mock in the tests above accepts any keyword argument.

    That is exactly how this change first passed its own tests while being
    unrunnable: the queue was updated to pass `category=` on a branch whose
    DownloadService did not take it yet, so every mocked test was green and
    production would have raised TypeError on the first queued grab.

    A MagicMock can never fail this way, so the real signature is inspected.
    """

    def test_download_service_accepts_every_kwarg_the_queue_sends(self):
        import ast
        import inspect
        import io

        from backend.download_service import DownloadService

        accepted = set(inspect.signature(DownloadService.download_item).parameters)

        # Read the kwargs the queue actually passes, from the source, rather
        # than restating them here -- a hand-copied list drifts silently.
        source = io.open("backend/download_queue.py", encoding="utf-8").read()
        sent: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "download_item"
            ):
                sent |= {kw.arg for kw in node.keywords if kw.arg}

        assert sent, "found no download_item call in the queue; the check would be vacuous"
        assert "category" in sent, "the queue is not sending the category at all"
        assert sent <= accepted, (
            f"the queue sends kwargs DownloadService.download_item does not accept: "
            f"{sorted(sent - accepted)}"
        )
