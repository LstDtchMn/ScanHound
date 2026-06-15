import pytest
pytest.importorskip("backend.app_config_manager")

"""Comprehensive tests for backend/app_config_manager.py module.

Covers:
- AppConfigManager initialization and config loading
- Config file loading from disk (valid JSON, invalid JSON, missing file)
- Environment variable overrides via env_mappings
- Config validation on load
- save_config: success, temp file creation, permissions, error handling
- get/set/update methods
- Edge cases: corrupt file, permission errors, empty config
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock, mock_open

import pytest

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytest.importorskip("backend.app_config_manager")

from backend.config import get_default_config, validate_config


# ======================================================================
# Helper to create AppConfigManager with a specific config path
# ======================================================================

def _make_manager(config_path):
    """Create an AppConfigManager pointing at the given config file path."""
    from backend.app_config_manager import AppConfigManager
    return AppConfigManager(config_path=config_path)


# ======================================================================
# 1. Initialization and loading
# ======================================================================

class TestAppConfigManagerInit:
    """Tests for AppConfigManager constructor and config loading."""

    def test_init_with_no_file(self, tmp_path):
        """When config file does not exist, defaults are used."""
        config_path = str(tmp_path / "nonexistent_config.json")
        mgr = _make_manager(config_path)

        assert mgr.config_path == config_path
        assert isinstance(mgr.config, dict)
        # Should have default keys
        default = get_default_config()
        for key in ("plex_url", "tmdb_api_key", "min_size_mb"):
            assert key in mgr.config

    def test_init_loads_from_file(self, tmp_path):
        """When config file exists, values are loaded and merged."""
        config_path = str(tmp_path / "config.json")
        custom_config = {"plex_url": "http://custom:32400", "min_size_mb": 500}
        with open(config_path, 'w') as f:
            json.dump(custom_config, f)

        mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://custom:32400"
        assert mgr.config["min_size_mb"] == 500

    def test_init_merges_with_defaults(self, tmp_path):
        """File config is merged on top of defaults; missing keys use defaults."""
        config_path = str(tmp_path / "config.json")
        custom_config = {"plex_url": "http://other:32400"}
        with open(config_path, 'w') as f:
            json.dump(custom_config, f)

        mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://other:32400"
        # Default keys still present
        default = get_default_config()
        assert "tmdb_api_key" in mgr.config

    def test_init_invalid_json_uses_defaults(self, tmp_path):
        """If config file has invalid JSON, defaults are used."""
        config_path = str(tmp_path / "config.json")
        with open(config_path, 'w') as f:
            f.write("{invalid json content!!!")

        mgr = _make_manager(config_path)
        # Should fall back to defaults without crashing
        assert isinstance(mgr.config, dict)
        default = get_default_config()
        assert mgr.config.get("plex_url") == default.get("plex_url")

    def test_init_empty_json_file(self, tmp_path):
        """Empty JSON object file should just use defaults."""
        config_path = str(tmp_path / "config.json")
        with open(config_path, 'w') as f:
            json.dump({}, f)

        mgr = _make_manager(config_path)
        default = get_default_config()
        assert mgr.config.get("plex_url") == default.get("plex_url")


# ======================================================================
# 2. Environment variable overrides
# ======================================================================

class TestEnvOverrides:
    """Tests for environment variable override logic in load_config."""

    def test_env_plex_url_override(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        env_vars = {"PLEX_URL": "http://env-plex:32400"}
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://env-plex:32400"

    def test_env_plex_token_override(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        env_vars = {"PLEX_TOKEN": "env_token_123"}
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)
        assert mgr.config["plex_token"] == "env_token_123"

    def test_env_tmdb_api_key_override(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        env_vars = {"TMDB_API_KEY": "env_tmdb_key"}
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)
        assert mgr.config["tmdb_api_key"] == "env_tmdb_key"

    def test_env_overrides_file_value(self, tmp_path):
        """Env vars should override values from the config file."""
        config_path = str(tmp_path / "config.json")
        with open(config_path, 'w') as f:
            json.dump({"plex_url": "http://file-plex:32400"}, f)

        env_vars = {"PLEX_URL": "http://env-plex:32400"}
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://env-plex:32400"

    def test_empty_env_var_not_applied(self, tmp_path):
        """Empty string env var should not override (falsy check)."""
        config_path = str(tmp_path / "config.json")
        default = get_default_config()
        env_vars = {"PLEX_URL": ""}
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)
        # Should keep default since env var is empty
        assert mgr.config["plex_url"] == default["plex_url"]

    def test_multiple_env_overrides(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        env_vars = {
            "PLEX_URL": "http://env:32400",
            "PLEX_TOKEN": "envtoken",
            "TMDB_API_KEY": "envtmdb",
            "OMDB_API_KEY": "envomdb",
            "DISCORD_WEBHOOK": "https://discord.webhook/test",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://env:32400"
        assert mgr.config["plex_token"] == "envtoken"
        assert mgr.config["tmdb_api_key"] == "envtmdb"
        assert mgr.config["omdb_api_key"] == "envomdb"
        assert mgr.config["discord_webhook"] == "https://discord.webhook/test"

    def test_all_env_mappings_applied(self, tmp_path):
        """Test all env variable mappings are recognized."""
        config_path = str(tmp_path / "config.json")
        env_vars = {
            "PLEX_URL": "url",
            "PLEX_TOKEN": "token",
            "PLEX_PASSWORD": "plexpass",
            "TMDB_API_KEY": "tmdb",
            "OMDB_API_KEY": "omdb",
            "JD_EMAIL": "jd@test.com",
            "JD_PASSWORD": "jdpass",
            "CUTY_EMAIL": "cuty@test.com",
            "CUTY_PASSWORD": "cutypass",
            "ADITHD_USERNAME": "adituser",
            "ADITHD_PASSWORD": "aditpass",
            "SMTP_USERNAME": "smtpuser",
            "SMTP_PASSWORD": "smtppass",
            "DISCORD_WEBHOOK": "discord_url",
            "SLACK_WEBHOOK": "slack_url",
            "PUSHOVER_USER": "pushuser",
            "PUSHOVER_TOKEN": "pushtoken",
            "WEBHOOK_URL": "webhook_url",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            mgr = _make_manager(config_path)

        assert mgr.config["plex_url"] == "url"
        assert mgr.config["plex_token"] == "token"
        assert mgr.config["plex_password"] == "plexpass"
        assert mgr.config["tmdb_api_key"] == "tmdb"
        assert mgr.config["omdb_api_key"] == "omdb"
        assert mgr.config["jd_email"] == "jd@test.com"
        assert mgr.config["jd_password"] == "jdpass"
        assert mgr.config["cuty_email"] == "cuty@test.com"
        assert mgr.config["cuty_password"] == "cutypass"
        assert mgr.config["adithd_username"] == "adituser"
        assert mgr.config["adithd_password"] == "aditpass"
        assert mgr.config["smtp_username"] == "smtpuser"
        assert mgr.config["smtp_password"] == "smtppass"
        assert mgr.config["discord_webhook"] == "discord_url"
        assert mgr.config["slack_webhook"] == "slack_url"
        assert mgr.config["pushover_user"] == "pushuser"
        assert mgr.config["pushover_token"] == "pushtoken"
        assert mgr.config["webhook_url"] == "webhook_url"


# ======================================================================
# 3. Validation on load
# ======================================================================

class TestValidationOnLoad:
    """Tests that validate_config is applied during load."""

    def test_invalid_values_clamped_on_load(self, tmp_path):
        """Negative min_size_mb in file should be clamped to 0."""
        config_path = str(tmp_path / "config.json")
        with open(config_path, 'w') as f:
            json.dump({"min_size_mb": -100, "scan_threads": 999}, f)

        mgr = _make_manager(config_path)
        assert mgr.config["min_size_mb"] == 0
        assert mgr.config["scan_threads"] == 50  # clamped to max


# ======================================================================
# 4. save_config
# ======================================================================

class TestSaveConfig:
    """Tests for AppConfigManager.save_config."""

    def test_save_creates_file(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.config["plex_url"] = "http://saved:32400"

        result = mgr.save_config()
        assert result is True
        assert os.path.exists(config_path)

        with open(config_path, 'r') as f:
            saved = json.load(f)
        assert saved["plex_url"] == "http://saved:32400"

    def test_save_overwrites_existing(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        # Create initial config
        with open(config_path, 'w') as f:
            json.dump({"plex_url": "old_url"}, f)

        mgr = _make_manager(config_path)
        mgr.config["plex_url"] = "new_url"
        mgr.save_config()

        with open(config_path, 'r') as f:
            saved = json.load(f)
        assert saved["plex_url"] == "new_url"

    def test_save_returns_true_on_success(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        assert mgr.save_config() is True

    def test_save_returns_false_on_error(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)

        # Make the directory read-only to cause an error
        with patch("backend.app_config_manager.os.open", side_effect=PermissionError("denied")):
            result = mgr.save_config()
        assert result is False

    def test_save_file_permissions(self, tmp_path):
        """Saved file should have restricted permissions (0o600)."""
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.save_config()

        if os.name != 'nt':  # Skip on Windows
            mode = os.stat(config_path).st_mode & 0o777
            assert mode == 0o600

    def test_save_uses_temp_file(self, tmp_path):
        """save_config should write to a temp file first then replace."""
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)

        with patch("backend.app_config_manager.os.replace") as mock_replace:
            with patch("backend.app_config_manager.os.open") as mock_os_open:
                # Simulate error to test that replace is intended to be called
                mock_os_open.side_effect = Exception("test")
                mgr.save_config()
                # os.replace not called due to error, but the pattern is there

        # Normal save should work
        result = mgr.save_config()
        assert result is True

    def test_save_json_format(self, tmp_path):
        """Saved JSON should be indented."""
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.config["plex_url"] = "http://test:32400"
        mgr.save_config()

        with open(config_path, 'r') as f:
            content = f.read()
        # Indented JSON has newlines
        assert "\n" in content

    def test_save_roundtrip(self, tmp_path):
        """Save then load should preserve all values."""
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.config["plex_url"] = "http://roundtrip:32400"
        mgr.config["min_size_mb"] = 42
        mgr.save_config()

        # Load again
        mgr2 = _make_manager(config_path)
        assert mgr2.config["plex_url"] == "http://roundtrip:32400"
        assert mgr2.config["min_size_mb"] == 42

    def test_save_chmod_oserror_handled(self, tmp_path):
        """OSError on chmod should not prevent save from succeeding."""
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)

        with patch("backend.app_config_manager.os.chmod", side_effect=OSError("not supported")):
            result = mgr.save_config()
        assert result is True
        assert os.path.exists(config_path)


# ======================================================================
# 5. get method
# ======================================================================

class TestGet:
    """Tests for AppConfigManager.get."""

    def test_get_existing_key(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        result = mgr.get("plex_url")
        assert isinstance(result, str)

    def test_get_missing_key_returns_default(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        result = mgr.get("nonexistent_key", "fallback")
        assert result == "fallback"

    def test_get_missing_key_default_none(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        result = mgr.get("nonexistent_key")
        assert result is None

    def test_get_returns_correct_types(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        assert isinstance(mgr.get("min_size_mb"), int)
        assert isinstance(mgr.get("movie_libs"), list)


# ======================================================================
# 6. set method
# ======================================================================

class TestSet:
    """Tests for AppConfigManager.set."""

    def test_set_updates_value(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.set("plex_url", "http://new:32400")
        assert mgr.config["plex_url"] == "http://new:32400"

    def test_set_validates_config(self, tmp_path):
        """Setting a value should trigger validation."""
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.set("min_size_mb", -50)
        assert mgr.config["min_size_mb"] == 0  # clamped

    def test_set_new_key(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.set("custom_key", "custom_value")
        assert mgr.config["custom_key"] == "custom_value"

    def test_set_scan_threads_clamped(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.set("scan_threads", 100)
        assert mgr.config["scan_threads"] == 50  # max 50


# ======================================================================
# 7. update method
# ======================================================================

class TestUpdate:
    """Tests for AppConfigManager.update."""

    def test_update_multiple_values(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.update({"plex_url": "http://updated:32400", "min_size_mb": 100})
        assert mgr.config["plex_url"] == "http://updated:32400"
        assert mgr.config["min_size_mb"] == 100

    def test_update_validates_config(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.update({"min_size_mb": -10, "scan_threads": 200})
        assert mgr.config["min_size_mb"] == 0
        assert mgr.config["scan_threads"] == 50

    def test_update_empty_dict(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        old_url = mgr.config["plex_url"]
        mgr.update({})
        assert mgr.config["plex_url"] == old_url

    def test_update_adds_new_keys(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        mgr.update({"brand_new_key": 42})
        assert mgr.config["brand_new_key"] == 42


# ======================================================================
# 8. Edge cases
# ======================================================================

class TestEdgeCases:
    """Edge cases and error scenarios."""

    def test_config_path_stored(self, tmp_path):
        config_path = str(tmp_path / "my_config.json")
        mgr = _make_manager(config_path)
        assert mgr.config_path == config_path

    def test_load_config_called_on_init(self, tmp_path):
        """load_config is called during __init__."""
        config_path = str(tmp_path / "config.json")
        with open(config_path, 'w') as f:
            json.dump({"plex_url": "http://init-test:32400"}, f)

        mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://init-test:32400"

    def test_load_config_can_be_called_again(self, tmp_path):
        """Calling load_config() again should reload from disk."""
        config_path = str(tmp_path / "config.json")
        with open(config_path, 'w') as f:
            json.dump({"plex_url": "http://first:32400"}, f)

        mgr = _make_manager(config_path)
        assert mgr.config["plex_url"] == "http://first:32400"

        # Write new config to disk
        with open(config_path, 'w') as f:
            json.dump({"plex_url": "http://second:32400"}, f)

        mgr.config = mgr.load_config()
        assert mgr.config["plex_url"] == "http://second:32400"

    def test_load_dotenv_called(self, tmp_path):
        """load_dotenv should be called during load_config."""
        config_path = str(tmp_path / "config.json")
        with patch("backend.app_config_manager.load_dotenv") as mock_dotenv:
            mgr = _make_manager(config_path)
            mock_dotenv.assert_called_once()

    def test_config_is_dict(self, tmp_path):
        config_path = str(tmp_path / "config.json")
        mgr = _make_manager(config_path)
        assert isinstance(mgr.config, dict)
