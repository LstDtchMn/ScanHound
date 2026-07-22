"""Durable-schema contracts for the non-destructive 4K metadata inventory."""

from backend.database import DatabaseManager


def test_init_db_backfills_seed_rows_without_overwriting_live_scan(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    assert db.upsert_dv_scan("/movies/example.mkv", "fel", source="seed")

    # Existing installations already have seed rows in dv_scan. Re-running
    # initialization must preserve that historical evidence independently of
    # the compatibility cache's later live-scan replacement.
    db.init_db()
    baseline = db.get_dv_seed_baseline("/movies/example.mkv")
    assert baseline["seed_layer"] == "fel"

    assert db.upsert_dv_scan("/movies/example.mkv", "mel", source="scan")
    assert db.get_dv_scan("/movies/example.mkv")["dv_layer"] == "mel"
    assert db.get_dv_seed_baseline("/movies/example.mkv")["seed_layer"] == "fel"


def test_scan_run_manifest_starts_pending_and_is_queryable(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))

    run = db.create_metadata_scan_run(scope="pilot", expected_count=2)
    assert run["status"] == "queued"
    assert run["expected_count"] == 2
    assert run["run_uuid"]

    created = db.create_metadata_scan_items(
        run["run_uuid"],
        [
            {"path": "/movies/a.mkv", "library_name": "Movies", "rating_key": "1"},
            {"path": "/movies/b.mkv", "library_name": "Movies", "rating_key": "2"},
        ],
    )
    assert created == 2

    items = db.list_metadata_scan_items(run["run_uuid"])
    assert [item["path"] for item in items] == ["/movies/a.mkv", "/movies/b.mkv"]
    assert {item["status"] for item in items} == {"pending"}
    assert {item["attempt_count"] for item in items} == {0}
    assert db.get_metadata_scan_run(run["run_uuid"])["expected_count"] == 2


def test_scan_manifest_rejects_unknown_status_and_duplicate_paths(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=1)

    assert db.create_metadata_scan_items(
        run["run_uuid"], [{"path": "/movies/a.mkv"}]
    ) == 1
    assert db.create_metadata_scan_items(
        run["run_uuid"], [{"path": "/movies/a.mkv"}]
    ) == 0
    assert db.update_metadata_scan_item(
        run["run_uuid"], "/movies/a.mkv", status="not-a-real-status"
    ) is False


def test_scan_run_transitions_are_explicit_and_record_terminal_time(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=0)

    assert db.update_metadata_scan_run(run["run_uuid"], status="running")
    assert db.get_metadata_scan_run(run["run_uuid"])["started_at"]
    assert db.update_metadata_scan_run(run["run_uuid"], status="completed")

    saved = db.get_metadata_scan_run(run["run_uuid"])
    assert saved["status"] == "completed"
    assert saved["completed_at"]
    assert db.update_metadata_scan_run(run["run_uuid"], status="not-a-real-status") is False
