"""A PAUSED durable metadata scan must be resumable.

The bug these tests pin (2026-08-12 review, H3, `database.py:4852`): a user pause
leaves every unprocessed row in status ``pending`` — the worker writes that state
deliberately — but ``prepare_metadata_scan_resume`` only reset
``interrupted``/``cancelled`` rows and then returned the UPDATE's rowcount. A
paused run therefore reset 0 rows, returned 0, and never got requeued, so
``MetadataScanJob.resume`` raised "metadata scan has no retryable items".

The Resume button was dead for exactly the state it exists to serve, and the only
way forward was discarding a multi-hour manifest and rescanning from scratch —
with no cached reuse of the dovi_tool/HDR10+ work.

Peer adjudication (Q4) confirmed the reader-side remedy: ``pending`` is the
semantically correct durable state for unfinished work, so resume must accept it
rather than have pause rewrite rows to ``interrupted`` (which means "the runtime
died") and race the in-flight item.
"""
from backend.database import DatabaseManager


def _paused_run(db, *, done=1, todo=2):
    """A run paused mid-way: some items scanned, the rest still pending."""
    run = db.create_metadata_scan_run(scope="pilot", expected_count=done + todo)
    paths = [{"path": "/generated/done-%d.mkv" % i} for i in range(done)]
    paths += [{"path": "/generated/todo-%d.mkv" % i} for i in range(todo)]
    db.create_metadata_scan_items(run["run_uuid"], paths)
    for i in range(done):
        db.update_metadata_scan_item(
            run["run_uuid"], "/generated/done-%d.mkv" % i, status="current"
        )
    # A pause leaves the remaining rows untouched -> still 'pending'.
    db.update_metadata_scan_run(run["run_uuid"], status="paused")
    return run


def test_paused_run_is_resumable(tmp_path):
    """THE bug: resetting 0 rows must not mean 'nothing to resume'."""
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = _paused_run(db, done=1, todo=2)

    resumable = db.prepare_metadata_scan_resume(run["run_uuid"])

    assert resumable == 2, "the two pending items are the work to resume"
    assert db.get_metadata_scan_run(run["run_uuid"])["status"] == "queued", \
        "the run must be requeued, not left paused forever"


def test_resume_does_not_disturb_already_scanned_items(tmp_path):
    """Completed work stays immutable across a resume."""
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = _paused_run(db, done=2, todo=1)

    db.prepare_metadata_scan_resume(run["run_uuid"])

    items = {i["path"]: i["status"] for i in db.list_metadata_scan_items(run["run_uuid"])}
    assert items["/generated/done-0.mkv"] == "current"
    assert items["/generated/done-1.mkv"] == "current"
    assert items["/generated/todo-0.mkv"] == "pending"


def test_interrupted_rows_are_still_repaired_and_counted(tmp_path):
    """Positive control for the original behaviour: an interrupted row is reset
    to pending AND counted, so the reader-side change did not regress crash
    recovery (the case that always worked)."""
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=2)
    db.create_metadata_scan_items(run["run_uuid"], [
        {"path": "/generated/a.mkv"}, {"path": "/generated/b.mkv"},
    ])
    db.update_metadata_scan_item(run["run_uuid"], "/generated/a.mkv", status="current")
    db.update_metadata_scan_item(run["run_uuid"], "/generated/b.mkv", status="interrupted")
    db.update_metadata_scan_run(run["run_uuid"], status="interrupted")

    assert db.prepare_metadata_scan_resume(run["run_uuid"]) == 1

    items = {i["path"]: i["status"] for i in db.list_metadata_scan_items(run["run_uuid"])}
    assert items["/generated/b.mkv"] == "pending"
    assert db.get_metadata_scan_run(run["run_uuid"])["status"] == "queued"


def test_a_fully_completed_run_has_nothing_to_resume(tmp_path):
    """The guard must still fail closed: no pending work -> not resumable, and
    the run is NOT requeued. Without this the fix could requeue finished runs."""
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=1)
    db.create_metadata_scan_items(run["run_uuid"], [{"path": "/generated/done.mkv"}])
    db.update_metadata_scan_item(run["run_uuid"], "/generated/done.mkv", status="current")
    db.update_metadata_scan_run(run["run_uuid"], status="completed")

    assert db.prepare_metadata_scan_resume(run["run_uuid"]) == 0
    assert db.get_metadata_scan_run(run["run_uuid"])["status"] == "completed"


def test_failed_rows_still_need_the_explicit_retry_opt_in(tmp_path):
    """A terminal probe failure is only retried when the operator asks."""
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=1)
    db.create_metadata_scan_items(run["run_uuid"], [{"path": "/generated/bad.mkv"}])
    db.update_metadata_scan_item(
        run["run_uuid"], "/generated/bad.mkv", status="failed",
        failure_stage="ffprobe", error_code="probe_unavailable",
    )
    db.update_metadata_scan_run(run["run_uuid"], status="completed")

    assert db.prepare_metadata_scan_resume(run["run_uuid"]) == 0
    assert db.prepare_metadata_scan_resume(run["run_uuid"], retry_failed=True) == 1
