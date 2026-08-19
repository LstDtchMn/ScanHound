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
    """The ALTER path, not the CREATE path.

    The first version of this test opened a fresh DatabaseManager and asserted
    `category` was present. Peer review (round 10, L2) pointed out that the
    current CREATE already includes the column, so the assertion passed without
    the migration existing at all. Reproduced: pointing the ALTER at a different
    column name left all nine tests green.

    So the old schema is now built BY HAND -- the pre-change CREATE, with no
    category column -- and the assertion is that opening it with the current
    DatabaseManager adds one.
    """

    #: The download_queue_items CREATE as it stood before this change. Copied
    #: rather than derived, deliberately: deriving it from the current schema
    #: would reintroduce exactly the tautology this test exists to avoid.
    OLD_CREATE = """
        CREATE TABLE download_queue_items (
            item_uuid TEXT PRIMARY KEY,
            batch_uuid TEXT NOT NULL,
            sequence_number INTEGER NOT NULL,
            source TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER,
            season INTEGER,
            resolution TEXT,
            size_text TEXT,
            hdr TEXT,
            dovi INTEGER NOT NULL DEFAULT 0,
            service_type TEXT NOT NULL,
            queue_reason TEXT NOT NULL,
            state TEXT NOT NULL,
            scheduled_for TEXT,
            cooldown_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            automated_retry_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            last_reason_code TEXT,
            last_cause_code TEXT,
            last_message TEXT,
            transport_attempted INTEGER,
            claimed_by TEXT,
            claim_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            cancelled_at TEXT
        )
    """

    def _old_database(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "pre_change.db")
        conn = sqlite3.connect(path)
        conn.executescript(self.OLD_CREATE)
        conn.commit()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(download_queue_items)")]
        conn.close()
        assert "category" not in cols, "the fixture is not an OLD schema"
        return path

    def test_a_database_without_the_column_gains_it_on_open(self, tmp_path):
        import sqlite3

        path = self._old_database(tmp_path)
        db = DatabaseManager(path)
        db.close()

        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(download_queue_items)")]
        conn.close()
        assert "category" in cols, "the ALTER did not run against a pre-change schema"

    def test_the_migrated_column_actually_holds_a_value(self, tmp_path):
        """A column that exists but cannot be written is not a migration.

        The ALTER runs inside a guard that swallows "duplicate column"; a
        migration that half-applied would still satisfy PRAGMA.
        """
        path = self._old_database(tmp_path)
        db = DatabaseManager(path)
        try:
            service = DownloadQueueService({}, db, MagicMock(), poll_seconds=0.01)
            batch = service.schedule_batch([_item(1, "tv")], interval_minutes=0)
            with db.transaction() as conn:
                row = conn.execute(
                    "SELECT category FROM download_queue_items WHERE item_uuid = ?",
                    (batch["items"][0]["item_uuid"],)).fetchone()
            assert dict(row)["category"] == "tv"
        finally:
            db.close()

    def test_a_current_database_is_unaffected(self, tmp_path):
        """The CREATE path still works; the ALTER is additive, not a rewrite."""
        import sqlite3

        path = str(tmp_path / "fresh.db")
        db = DatabaseManager(path)
        db.close()
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(download_queue_items)")]
        conn.close()
        assert "category" in cols


class TestTheCallTheQueueMakesIsOneDownloadServiceAccepts:
    """The mock in the tests above accepts any keyword argument.

    That is how this change first passed its own tests while being unrunnable:
    the queue passed `category=` on a branch whose DownloadService did not take
    it, so every mocked test was green and production would have raised
    TypeError on the first queued grab.

    The first version of this test compared a UNION of kwargs across every call
    site against the accepted parameter set. Peer review (round 10, L1) showed
    two mutations survive that:

      * deleting a REQUIRED argument -- `url=item["canonical_url"]` -- still
        satisfies `sent <= accepted`, while production raises
        "missing a required argument: 'url'". Reproduced: all 9 tests passed
        with the argument removed.
      * unioning hides a second call site that omits `category`, because the
        first call site already put it in the set.

    Both are fixed by binding the REAL signature to EACH call separately, which
    is what the interpreter does at runtime.
    """

    @staticmethod
    def _download_item_calls():
        """Every `download_item(...)` call in the queue, as (args, kwarg-names)."""
        import ast
        import io as _io

        source = _io.open("backend/download_queue.py", encoding="utf-8").read()
        calls = []
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "download_item"):
                calls.append((node.args, [kw.arg for kw in node.keywords], node.lineno))
        return calls

    def test_every_call_binds_against_the_real_signature(self):
        """Catches unknown kwargs AND missing required ones, per call site."""
        import inspect

        from backend.download_service import DownloadService

        sig = inspect.signature(DownloadService.download_item)
        calls = self._download_item_calls()
        assert calls, "found no download_item call in the queue; the check would be vacuous"

        for args, kwargs, lineno in calls:
            assert not args, (
                f"download_queue.py:{lineno} passes positional arguments; this check "
                f"only models keyword calls")
            assert all(k is not None for k in kwargs), (
                f"download_queue.py:{lineno} uses **kwargs; the real arguments cannot "
                f"be checked statically")
            # `None` stands in for self. Placeholder values: bind() checks arity
            # and names, never types.
            sig.bind(None, **{k: object() for k in kwargs})

    def test_every_call_sends_the_category(self):
        """Per call, not unioned. A second call site that forgets it must fail
        even though the first one remembers."""
        calls = self._download_item_calls()
        for _args, kwargs, lineno in calls:
            assert "category" in kwargs, (
                f"download_queue.py:{lineno} calls download_item without a category; "
                f"grabs made through it record no media kind")

    def test_the_bind_check_rejects_a_missing_required_argument(self):
        """Proves the check above is not vacuous.

        This is the exact mutation that survived the previous version.
        """
        import inspect

        import pytest as _pytest

        from backend.download_service import DownloadService

        sig = inspect.signature(DownloadService.download_item)
        without_url = {"title": "t", "year": 2026, "season": None, "resolution": "",
                       "size": "", "hdr": "", "dovi": False,
                       "service_type": "Rapidgator", "category": "tv"}
        with _pytest.raises(TypeError):
            sig.bind(None, **without_url)

    def test_the_bind_check_rejects_an_unknown_argument(self):
        """The original failure class, still covered."""
        import inspect

        import pytest as _pytest

        from backend.download_service import DownloadService

        sig = inspect.signature(DownloadService.download_item)
        with _pytest.raises(TypeError):
            sig.bind(None, url="u", title="t", not_a_real_parameter=1)
