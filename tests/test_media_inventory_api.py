from types import SimpleNamespace

from backend.api.routes import plex as plex_routes
from backend.api.routes.plex import plex_media_inventory, plex_media_inventory_facets
from backend.database import DatabaseManager


def test_inventory_route_filters_local_fel_metadata(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    db.upsert_media_inventory({
        "path": "/movies/fel.mkv", "title": "FEL Film", "resolution": "2160p",
        "dv_layer": "fel", "hdr10plus_state": "present", "scan_state": "current",
    })
    reg = SimpleNamespace(db=db)

    result = plex_media_inventory(dv_layer="fel", hdr10plus_state="present", reg=reg)

    assert result["total"] == 1
    assert result["items"][0]["path"] == "/movies/fel.mkv"
    assert plex_media_inventory_facets(reg=reg)["dv_layer"] == [{"value": "fel", "count": 1}]


def test_inventory_csv_neutralizes_spreadsheet_formulas(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    db.upsert_media_inventory({
        "path": "/movies/formula.mkv", "title": '=HYPERLINK("https://invalid")',
        "resolution": "2160p", "scan_state": "current",
    })

    response = plex_routes.plex_media_inventory_export(reg=SimpleNamespace(db=db))
    text = response.body.decode("utf-8")

    assert "'=HYPERLINK" in text
    assert response.media_type == "text/csv"


def test_durable_scan_endpoint_accepts_only_cached_4k_targets(monkeypatch):
    calls = []

    class Job:
        def start_run(self, scope, targets):
            calls.append((scope, targets))
            return {"run_uuid": "run-1", "status": "queued"}

    monkeypatch.setattr(plex_routes, "_movie_targets_for_scope", lambda *_args: [
        {"path": "/movies/uhd.mkv", "resolution": "2160p"},
        {"path": "/movies/hd.mkv", "resolution": "1080p"},
    ])
    reg = SimpleNamespace(plex_metadata_scan_job=Job())

    result = plex_routes.plex_start_durable_metadata_scan(
        plex_routes.DurableMetadataScanRequest(scope="full"), reg=reg
    )

    assert result["run_uuid"] == "run-1"
    assert calls == [("full", [{"path": "/movies/uhd.mkv", "resolution": "2160p"}])]


def test_durable_scan_control_routes_delegate_by_run_uuid():
    calls = []

    class Job:
        def pause(self, run_uuid):
            calls.append(("pause", run_uuid))
            return {"status": "pausing", "run_uuid": run_uuid}

        def resume(self, run_uuid):
            calls.append(("resume", run_uuid))
            return {"status": "running", "run_uuid": run_uuid}

        def cancel(self, run_uuid):
            calls.append(("cancel", run_uuid))
            return {"status": "cancelling", "run_uuid": run_uuid}

        def retry_failures(self, run_uuid):
            calls.append(("retry", run_uuid))
            return {"status": "running", "run_uuid": run_uuid}

    reg = SimpleNamespace(plex_metadata_scan_job=Job())

    assert plex_routes.plex_pause_metadata_scan("run-1", reg=reg)["status"] == "pausing"
    assert plex_routes.plex_resume_metadata_scan("run-1", reg=reg)["status"] == "running"
    assert plex_routes.plex_cancel_durable_metadata_scan("run-1", reg=reg)["status"] == "cancelling"
    assert plex_routes.plex_retry_metadata_scan_failures("run-1", reg=reg)["status"] == "running"
    assert calls == [
        ("pause", "run-1"), ("resume", "run-1"),
        ("cancel", "run-1"), ("retry", "run-1"),
    ]
