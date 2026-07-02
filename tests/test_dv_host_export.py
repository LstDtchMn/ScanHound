import json
import logging

from backend.app_service import AppService, export_dv_host_config


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


# ======================================================================
# Fix 1 — fail-safe: a broken export must never break settings save
# ======================================================================


def test_save_config_survives_export_raising(tmp_path, monkeypatch, caplog):
    """save_config() must complete successfully even if the dv_host.json
    export blows up. This is a REAL AppService.save_config() call — not just
    the export_dv_host_config() helper — so it exercises the try/except at
    the actual call site (backend/app_service.py ~768-771).
    """
    import backend.app_service as app_service

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(app_service, "CONFIG_FILE", str(config_file), raising=False)
    monkeypatch.setattr(app_service, "_LEGACY_CONFIG_FILE", str(tmp_path / "legacy.json"), raising=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated dv_host.json export failure")

    monkeypatch.setattr(app_service, "export_dv_host_config", _boom)

    svc = AppService()
    svc.config = {"theme_mode": "dark"}

    with caplog.at_level(logging.WARNING, logger="backend.app_service"):
        svc.save_config()  # must not raise

    # The primary settings save must have completed normally...
    assert config_file.exists()
    assert json.loads(config_file.read_text(encoding="utf-8"))["theme_mode"] == "dark"
    # ...and the failure must have been logged, proving the except branch
    # (not some other code path) is what swallowed the RuntimeError.
    assert any(
        "dv_host.json export failed" in record.message and "simulated dv_host.json export failure" in record.message
        for record in caplog.records
    ), f"expected warning log not found; got: {[r.message for r in caplog.records]}"


# ======================================================================
# Fix 2 — end-to-end: real AppService.save_config() call site
# ======================================================================


def test_save_config_end_to_end_writes_dv_host_json(tmp_path, monkeypatch):
    """Drive a real AppService.save_config() (the actual production call
    site, not just the export_dv_host_config() helper) and confirm
    dv_host.json ends up on disk with exactly the 4 dv_* keys and correct
    values, while CONFIG_FILE itself also gets written as normal.
    """
    import backend.app_service as app_service

    config_file = tmp_path / "config.json"
    dv_host_json = tmp_path / "dv_host.json"
    monkeypatch.setattr(app_service, "CONFIG_FILE", str(config_file), raising=False)
    monkeypatch.setattr(app_service, "_LEGACY_CONFIG_FILE", str(tmp_path / "legacy.json"), raising=False)
    monkeypatch.setattr(app_service, "DV_HOST_JSON", str(dv_host_json), raising=False)

    svc = AppService()
    svc.config = {
        "plex_token": "SECRET",  # must not leak into dv_host.json
        "dv_library_roots": "Y:\\M;E:\\4K",
        "dv_detection": True,
        "dv_file_tagging": True,
        "dv_label_vocab": '{"fel": "DV FEL"}',
    }

    svc.save_config()

    # CONFIG_FILE (the primary settings file) was written as usual.
    assert config_file.exists()

    # dv_host.json was written via the real save_config() call site.
    assert dv_host_json.exists()
    on_disk = json.loads(dv_host_json.read_text(encoding="utf-8"))
    assert set(on_disk) == {
        "dv_library_roots", "dv_detection", "dv_file_tagging", "dv_label_vocab"}
    assert on_disk["dv_library_roots"] == "Y:\\M;E:\\4K"
    assert on_disk["dv_detection"] is True
    assert on_disk["dv_file_tagging"] is True
    assert on_disk["dv_label_vocab"] == '{"fel": "DV FEL"}'
    assert "plex_token" not in on_disk
