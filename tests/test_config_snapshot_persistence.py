"""Tests for AppService.persist_config_snapshot / commit_config_in_place.

Covers the two defects persist_config_snapshot() works around (see the
module-level NOTE above `class AppService` in backend/app_service.py, and
save_config() immediately above persist_config_snapshot() there):

  - save_config()'s sensitive-key preservation mutates LIVE self.config
    (`self.config[key] = disk_val`) before a write is even attempted, so a
    caller that only wanted to validate a candidate -- or whose write later
    fails -- still ends up with mutated live memory;
  - backend/api/main.py:113 does `reg.config = backend.config`, so the
    registry and AppService share ONE dict object. commit_config_in_place()
    must mutate that object in place, never rebind self.config, or every
    pre-existing holder of the shared object goes stale.

Every test here points CONFIG_FILE at tmp_path via monkeypatch, on top of
the autouse `_isolate_config_file` fixture in tests/conftest.py (which
already redirects CONFIG_FILE/_LEGACY_CONFIG_FILE/DV_HOST_JSON for every
test in the suite). The explicit monkeypatch is kept anyway so this file's
isolation is legible on its own, matching the pattern used in
tests/test_dv_host_export.py.
"""

import json

import pytest

from backend.app_service import AppService, ConfigPersistError
import backend.app_service as app_service_module


def _isolated_config_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(
        app_service_module, "CONFIG_FILE", str(config_file), raising=False
    )
    return config_file


def _make_service(initial_config=None):
    svc = AppService()
    svc.config = dict(initial_config or {})
    return svc


# ======================================================================
# 1. Happy path
# ======================================================================


def test_happy_path_persists_new_key_without_touching_live_config(
    tmp_path, monkeypatch
):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    svc = _make_service({"scan_threads": 1})

    candidate = {"scan_threads": 1, "new_feature_flag": True}
    result = svc.persist_config_snapshot(candidate)

    assert config_file.exists()
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk == result
    assert on_disk["new_feature_flag"] is True

    # self.config is untouched until commit_config_in_place() is called.
    assert svc.config == {"scan_threads": 1}
    assert "new_feature_flag" not in svc.config

    svc.commit_config_in_place(result)
    assert svc.config["new_feature_flag"] is True


# ======================================================================
# 2. must_contain mismatch
# ======================================================================


def test_must_contain_mismatch_raises_and_leaves_config_unchanged(
    tmp_path, monkeypatch
):
    _isolated_config_file(tmp_path, monkeypatch)
    svc = _make_service({"scan_threads": 1})

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot(
            {"scan_threads": 5}, must_contain={"scan_threads": 999}
        )

    assert svc.config == {"scan_threads": 1}


# ======================================================================
# 3. Failure injection: os.replace, os.fsync, the verification read
# ======================================================================


def test_os_replace_failure_raises_and_cleans_up(tmp_path, monkeypatch):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    svc = _make_service({"scan_threads": 1})

    def _boom(*args, **kwargs):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(app_service_module.os, "replace", _boom)

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot({"scan_threads": 5})

    assert svc.config == {"scan_threads": 1}
    assert not config_file.exists()
    assert not (tmp_path / "config.json.tmp").exists()


def test_os_fsync_failure_raises_and_cleans_up(tmp_path, monkeypatch):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    svc = _make_service({"scan_threads": 1})

    def _boom(*args, **kwargs):
        raise OSError("simulated os.fsync failure")

    monkeypatch.setattr(app_service_module.os, "fsync", _boom)

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot({"scan_threads": 5})

    assert svc.config == {"scan_threads": 1}
    assert not config_file.exists()
    assert not (tmp_path / "config.json.tmp").exists()


def test_verification_read_failure_raises_and_cleans_up(tmp_path, monkeypatch):
    _isolated_config_file(tmp_path, monkeypatch)
    svc = _make_service({"scan_threads": 1})

    # CONFIG_FILE does not exist yet, so the sensitive-key-preservation read
    # is skipped -- json.load() is called exactly once in this method: the
    # post-write verification read (step 4). Failing only that call exercises
    # verification in isolation; the write itself (step 3) already succeeded
    # by the time this raises.
    def _boom(*args, **kwargs):
        raise json.JSONDecodeError("simulated corrupt read", "", 0)

    monkeypatch.setattr(app_service_module.json, "load", _boom)

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot({"scan_threads": 5})

    assert svc.config == {"scan_threads": 1}
    assert not (tmp_path / "config.json.tmp").exists()


# ======================================================================
# 4. Sensitive-key preservation must not mutate live self.config
# ======================================================================


def test_sensitive_key_preserved_in_result_without_mutating_live_config(
    tmp_path, monkeypatch
):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    config_file.write_text(
        json.dumps({"plex_token": "disk-secret-value", "scan_threads": 1}),
        encoding="utf-8",
    )

    svc = _make_service({"plex_token": "", "scan_threads": 1})
    candidate = {"plex_token": "", "scan_threads": 2}

    result = svc.persist_config_snapshot(candidate)

    # The disk value is preserved in the persisted/returned snapshot...
    assert result["plex_token"] == "disk-secret-value"
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk["plex_token"] == "disk-secret-value"

    # ...but self.config was NEVER touched by the preservation step. This is
    # the specific defect being fixed: save_config() does
    # `self.config[key] = disk_val` at backend/app_service.py:1167, live, in
    # memory, before the write is even attempted. This assertion fails if
    # that mutation reappears in persist_config_snapshot.
    assert svc.config["plex_token"] == ""
    assert svc.config["scan_threads"] == 1


# ======================================================================
# 5. commit_config_in_place preserves object identity
# ======================================================================


def test_commit_config_in_place_mutates_shared_object_in_place():
    svc = _make_service({"a": 1})
    shared_alias = svc.config  # stands in for reg.config = backend.config
    original_id = id(svc.config)

    svc.commit_config_in_place({"a": 2, "b": 3})

    assert svc.config == {"a": 2, "b": 3}
    assert shared_alias == {"a": 2, "b": 3}
    assert shared_alias is svc.config
    assert id(svc.config) == original_id
