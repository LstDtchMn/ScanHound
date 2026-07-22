from types import SimpleNamespace

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
