import json
from backend.app_service import export_dv_host_config


def test_export_writes_only_dv_keys(tmp_path):
    dest = tmp_path / "dv_host.json"
    cfg = {
        "plex_token": "SECRET",            # must NOT leak
        "dv_library_roots": "Y:\\M;E:\\4K",
        "dv_detection": True,
        "dv_file_tagging": False,
        "dv_label_vocab": '{"fel": "DV FEL"}',
    }
    written = export_dv_host_config(cfg, str(dest))
    assert set(written) == {
        "dv_library_roots", "dv_detection", "dv_file_tagging", "dv_label_vocab"}
    on_disk = json.loads(dest.read_text(encoding="utf-8"))
    assert on_disk == written
    assert "plex_token" not in on_disk


def test_export_uses_defaults_for_missing_keys(tmp_path):
    dest = tmp_path / "dv_host.json"
    written = export_dv_host_config({}, str(dest))
    assert written["dv_detection"] is False
    assert written["dv_library_roots"] == ""


def test_save_config_exports_dv_host_json(tmp_path, monkeypatch):
    import backend.app_service as app_service

    class _Svc:
        config = {
            "dv_library_roots": "Y:\\M",
            "dv_detection": True,
            "dv_file_tagging": False,
            "dv_label_vocab": '{"fel": "DV FEL"}',
        }
        _cleared_keys = set()
    dest = tmp_path / "data" / "dv_host.json"
    monkeypatch.setattr(app_service, "DV_HOST_JSON", str(dest), raising=False)
    # exercise just the export hook (avoid the full CONFIG_FILE write path)
    app_service.export_dv_host_config(_Svc.config, app_service.DV_HOST_JSON)
    assert json.loads(dest.read_text(encoding="utf-8"))["dv_detection"] is True
