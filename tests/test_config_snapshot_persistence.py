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


def _staged_temp_files(tmp_path):
    """Any leftover staged file from persist_config_snapshot's write path.

    Named `config.json.<uuid hex>.tmp` (a unique name per attempt, so
    concurrent snapshots never collide on one staged path) rather than the
    fixed `config.json.tmp` the pre-R1 implementation used -- glob for the
    pattern instead of a literal name so this keeps checking the real thing
    even if the exact naming scheme changes again later.
    """
    return list(tmp_path.glob("config.json.*.tmp"))


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
    assert not _staged_temp_files(tmp_path)


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
    assert not _staged_temp_files(tmp_path)


def test_verification_read_failure_raises_and_cleans_up(tmp_path, monkeypatch):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    svc = _make_service({"scan_threads": 1})

    # CONFIG_FILE does not exist yet, so the sensitive-key-preservation read
    # is skipped -- json.load() is called exactly once in this method: the
    # post-write verification read of the STAGED file (step 3). Failing only
    # that call exercises verification in isolation; the write to the staged
    # file already succeeded by the time this raises.
    def _boom(*args, **kwargs):
        raise json.JSONDecodeError("simulated corrupt read", "", 0)

    monkeypatch.setattr(app_service_module.json, "load", _boom)

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot({"scan_threads": 5})

    assert svc.config == {"scan_threads": 1}
    # R1: verification happens BEFORE os.replace() now, so a failed
    # verification read must never have touched CONFIG_FILE at all -- it
    # must not even exist yet, since nothing was ever there before this call
    # and the (only) write landed on the staged file, never the real path.
    assert not config_file.exists()
    assert not _staged_temp_files(tmp_path)


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
# 4b. R1: a failed verification must leave the file ON DISK exactly as it
#     was before the call -- not just self.config. Each case here seeds
#     CONFIG_FILE with a known prior state first, then asserts that exact
#     state (including the ABSENCE of a promotion record) is still there,
#     verbatim, after persist_config_snapshot() raises. This is the R1
#     defect directly: the previous implementation called os.replace() --
#     the durable commit -- BEFORE verifying, so a failed verification
#     still left the unverified candidate as the config on disk, and the
#     caller's 503 ("nothing was changed") was false of the actual file.
# ======================================================================

# backend/rss_primary_authority.py:162 (PROMOTION_KEY) -- the supervisor's
# lane owns that module concurrently in this worktree, so the key is
# hardcoded here rather than imported, to keep this test file decoupled
# from concurrent edits to a file this lane does not own.
_PROMOTION_KEY = "hdencode_rss_primary_promotion"


def _seed_prior_state(config_file):
    prior_state = {"hdencode_discovery_mode": "rss_shadow", "scan_threads": 1}
    config_file.write_text(json.dumps(prior_state), encoding="utf-8")
    return prior_state


def test_must_contain_mismatch_leaves_prior_state_on_disk_untouched(
    tmp_path, monkeypatch
):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    prior_state = _seed_prior_state(config_file)
    svc = _make_service(dict(prior_state))

    candidate = {
        "hdencode_discovery_mode": "rss_primary",
        "scan_threads": 1,
        _PROMOTION_KEY: {"at": "2026-09-06T00:00:00+00:00", "by": "operator"},
    }

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot(candidate, must_contain={"scan_threads": 999})

    assert svc.config == prior_state
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk == prior_state
    assert _PROMOTION_KEY not in on_disk
    assert not _staged_temp_files(tmp_path)


def test_verification_read_failure_leaves_prior_state_on_disk_untouched(
    tmp_path, monkeypatch
):
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    prior_state = _seed_prior_state(config_file)
    svc = _make_service(dict(prior_state))

    candidate = {
        "hdencode_discovery_mode": "rss_primary",
        "scan_threads": 1,
        _PROMOTION_KEY: {"at": "2026-09-06T00:00:00+00:00", "by": "operator"},
    }

    # CONFIG_FILE already exists here (prior_state, seeded above), so
    # json.load() is called TWICE before this method would otherwise
    # return: once for the sensitive-key-preservation read of the existing
    # file, once for the post-write verification read of the STAGED file.
    # Let the first call through for real and fail only the second, so this
    # exercises a verification-read failure in isolation, with a real prior
    # config in play (not the no-prior-file case the simpler test above
    # already covers).
    real_load = json.load
    calls = {"n": 0}

    def _load_second_call_boom(fp, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_load(fp, *args, **kwargs)
        raise json.JSONDecodeError("simulated corrupt verification read", "", 0)

    monkeypatch.setattr(app_service_module.json, "load", _load_second_call_boom)

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot(candidate)

    assert svc.config == prior_state
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert on_disk == prior_state
    assert _PROMOTION_KEY not in on_disk
    assert not _staged_temp_files(tmp_path)


def test_unreadable_existing_config_raises_without_replacing_it(
    tmp_path, monkeypatch
):
    """Strict sensitive-key preservation: an existing CONFIG_FILE that
    cannot be parsed must raise, not silently proceed with candidate-only
    values -- proceeding could overwrite a preserved credential with a
    blank the candidate happens to carry. This is the other half of R1's
    strictness requirement, distinct from save_config()'s own unrelated
    fail-soft (json.JSONDecodeError, IOError): pass, which is untouched."""
    config_file = _isolated_config_file(tmp_path, monkeypatch)
    corrupt_text = "{not valid json,,,"
    config_file.write_text(corrupt_text, encoding="utf-8")
    svc = _make_service({"scan_threads": 1})

    with pytest.raises(ConfigPersistError):
        svc.persist_config_snapshot({"scan_threads": 2})

    # The real file on disk is untouched -- still the corrupt text, never
    # replaced -- and no staged file was even created, since this raises
    # before the write step is reached.
    assert config_file.read_text(encoding="utf-8") == corrupt_text
    assert svc.config == {"scan_threads": 1}
    assert not _staged_temp_files(tmp_path)


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
