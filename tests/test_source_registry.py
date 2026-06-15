"""Comprehensive tests for backend/sources/registry.py module.

Covers:
- SourceRegistry.__init__: empty dicts
- register: adds source, returns True, notifies callback
- register duplicate: warns but replaces
- register failure (bad class): returns False
- unregister: removes source, configs, enabled, instances
- unregister non-existent: no error
- get_source: returns instance (lazy instantiation), None for unknown
- get_config: returns SourceConfig, None for unknown
- get_all_sources: list of instances
- get_enabled_sources: only enabled ones
- list_sources: returns dicts with expected keys, sorted by priority
- enable_source / disable_source: toggles enabled state, notifies callback
- add_callback: callback receives events
- get_stats: total_sources, enabled_sources count
- fetch_from_source: unknown source returns PageResult with error
- fetch_from_source: known source returns PageResult
"""

import asyncio
import logging
import threading
import time
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from backend.sources.base import (
    SourceBase,
    SourceConfig,
    SourceCapability,
    ParsedRelease,
    PageResult,
)
import backend.sources.registry as registry_module
from backend.sources.registry import SourceRegistry


# ── Mock source class for testing ────────────────────────────────────


class MockSource(SourceBase):
    """A minimal concrete source for testing the registry."""

    @classmethod
    def get_config(cls):
        return SourceConfig(
            name="mock",
            display_name="Mock Source",
            base_url="https://mock.example.com",
        )

    async def fetch_page(self, page=1, mode="movies", **kwargs):
        return PageResult(releases=[])

    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        return None

    async def parse_listing(self, html, mode="movies"):
        return []

    async def parse_detail(self, html, url=""):
        return None


class MockSourceHighPriority(SourceBase):
    """A second mock source with higher priority for ordering tests."""

    @classmethod
    def get_config(cls):
        return SourceConfig(
            name="mock_high",
            display_name="Mock High Priority",
            base_url="https://high.example.com",
            priority=200,
        )

    async def fetch_page(self, page=1, mode="movies", **kwargs):
        release = ParsedRelease(
            title="Test Release",
            url="https://high.example.com/release/1",
            source="mock_high",
        )
        return PageResult(releases=[release])

    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        return None


class MockSourceDisabled(SourceBase):
    """A mock source that is disabled by default."""

    @classmethod
    def get_config(cls):
        return SourceConfig(
            name="mock_disabled",
            display_name="Mock Disabled",
            base_url="https://disabled.example.com",
            enabled=False,
            priority=50,
        )

    async def fetch_page(self, page=1, mode="movies", **kwargs):
        return PageResult(releases=[])

    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        return None


class MockSourceFetchError(SourceBase):
    """A mock source whose fetch_page always raises."""

    @classmethod
    def get_config(cls):
        return SourceConfig(
            name="mock_error",
            display_name="Mock Error Source",
            base_url="https://error.example.com",
        )

    async def fetch_page(self, page=1, mode="movies", **kwargs):
        raise RuntimeError("Simulated fetch failure")

    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        return None


# ======================================================================
# SourceRegistry.__init__ Tests
# ======================================================================


class TestSourceRegistryInit:
    """Tests for SourceRegistry initialization."""

    def test_sources_dict_empty(self):
        """On creation the _sources dict should be empty."""
        reg = SourceRegistry()
        assert reg._sources == {}

    def test_instances_dict_empty(self):
        """On creation the _instances dict should be empty."""
        reg = SourceRegistry()
        assert reg._instances == {}

    def test_configs_dict_empty(self):
        """On creation the _configs dict should be empty."""
        reg = SourceRegistry()
        assert reg._configs == {}

    def test_enabled_dict_empty(self):
        """On creation the _enabled dict should be empty."""
        reg = SourceRegistry()
        assert reg._enabled == {}

    def test_callbacks_list_empty(self):
        """On creation the _callbacks list should be empty."""
        reg = SourceRegistry()
        assert reg._callbacks == []


# ======================================================================
# SourceRegistry.register Tests
# ======================================================================


class TestRegister:
    """Tests for SourceRegistry.register()."""

    def test_register_returns_true(self):
        """Registering a valid source should return True."""
        reg = SourceRegistry()
        result = reg.register(MockSource)
        assert result is True

    def test_register_adds_source_class(self):
        """After registration the source class should be in _sources."""
        reg = SourceRegistry()
        reg.register(MockSource)
        assert "mock" in reg._sources
        assert reg._sources["mock"] is MockSource

    def test_register_stores_config(self):
        """After registration the config should be stored."""
        reg = SourceRegistry()
        reg.register(MockSource)
        assert "mock" in reg._configs
        cfg = reg._configs["mock"]
        assert isinstance(cfg, SourceConfig)
        assert cfg.name == "mock"
        assert cfg.display_name == "Mock Source"

    def test_register_stores_enabled_flag(self):
        """After registration the enabled state should match config default."""
        reg = SourceRegistry()
        reg.register(MockSource)
        assert reg._enabled["mock"] is True

    def test_register_disabled_source(self):
        """Registering a disabled-by-default source stores enabled=False."""
        reg = SourceRegistry()
        reg.register(MockSourceDisabled)
        assert reg._enabled["mock_disabled"] is False

    def test_register_notifies_callback(self):
        """Registration should notify all registered callbacks."""
        reg = SourceRegistry()
        events = []
        reg.add_callback(lambda event, data: events.append((event, data)))
        reg.register(MockSource)
        assert len(events) == 1
        assert events[0] == ("registered", "mock")

    def test_register_duplicate_warns_but_replaces(self):
        """Re-registering an existing source should warn and replace."""
        reg = SourceRegistry()
        reg.register(MockSource)

        with patch("backend.sources.registry.logger") as mock_logger:
            result = reg.register(MockSource)
            mock_logger.warning.assert_called()

        assert result is True
        assert reg._sources["mock"] is MockSource

    def test_register_failure_bad_class_returns_false(self):
        """Registering a class whose get_config raises should return False."""

        class BadSource:
            @classmethod
            def get_config(cls):
                raise ValueError("broken config")

        reg = SourceRegistry()
        result = reg.register(BadSource)
        assert result is False

    def test_register_failure_not_a_class_returns_false(self):
        """Registering something that is not a class should return False."""
        reg = SourceRegistry()
        result = reg.register("not_a_class")
        assert result is False


# ======================================================================
# SourceRegistry.unregister Tests
# ======================================================================


class TestUnregister:
    """Tests for SourceRegistry.unregister()."""

    def test_unregister_removes_source(self):
        """Unregistering should remove the source from _sources."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.unregister("mock")
        assert "mock" not in reg._sources

    def test_unregister_removes_config(self):
        """Unregistering should remove the config."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.unregister("mock")
        assert "mock" not in reg._configs

    def test_unregister_removes_enabled(self):
        """Unregistering should remove the enabled flag."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.unregister("mock")
        assert "mock" not in reg._enabled

    def test_unregister_removes_instance(self):
        """Unregistering should remove any cached instance."""
        reg = SourceRegistry()
        reg.register(MockSource)
        # Force instance creation
        reg.get_source("mock")
        assert "mock" in reg._instances
        reg.unregister("mock")
        assert "mock" not in reg._instances

    def test_unregister_notifies_callback(self):
        """Unregistering should notify callbacks with 'unregistered' event."""
        reg = SourceRegistry()
        reg.register(MockSource)
        events = []
        reg.add_callback(lambda event, data: events.append((event, data)))
        reg.unregister("mock")
        assert ("unregistered", "mock") in events

    def test_unregister_nonexistent_no_error(self):
        """Unregistering a name that was never registered should not raise."""
        reg = SourceRegistry()
        reg.unregister("does_not_exist")  # Should not raise


# ======================================================================
# SourceRegistry.get_source Tests
# ======================================================================


class TestGetSource:
    """Tests for SourceRegistry.get_source()."""

    def test_returns_instance_for_registered_source(self):
        """get_source should return a SourceBase instance."""
        reg = SourceRegistry()
        reg.register(MockSource)
        instance = reg.get_source("mock")
        assert isinstance(instance, MockSource)
        assert isinstance(instance, SourceBase)

    def test_lazy_instantiation(self):
        """Instance should not be created until get_source is called."""
        reg = SourceRegistry()
        reg.register(MockSource)
        assert "mock" not in reg._instances
        reg.get_source("mock")
        assert "mock" in reg._instances

    def test_returns_same_instance_on_repeated_calls(self):
        """Subsequent calls should return the same cached instance."""
        reg = SourceRegistry()
        reg.register(MockSource)
        inst1 = reg.get_source("mock")
        inst2 = reg.get_source("mock")
        assert inst1 is inst2

    def test_returns_none_for_unknown(self):
        """get_source for an unregistered name should return None."""
        reg = SourceRegistry()
        assert reg.get_source("nonexistent") is None


# ======================================================================
# SourceRegistry.get_config Tests
# ======================================================================


class TestGetConfig:
    """Tests for SourceRegistry.get_config()."""

    def test_returns_config_for_registered_source(self):
        """get_config should return the SourceConfig for a registered source."""
        reg = SourceRegistry()
        reg.register(MockSource)
        cfg = reg.get_config("mock")
        assert isinstance(cfg, SourceConfig)
        assert cfg.name == "mock"
        assert cfg.base_url == "https://mock.example.com"

    def test_returns_none_for_unknown(self):
        """get_config for an unregistered name should return None."""
        reg = SourceRegistry()
        assert reg.get_config("nonexistent") is None


# ======================================================================
# SourceRegistry.get_all_sources Tests
# ======================================================================


class TestGetAllSources:
    """Tests for SourceRegistry.get_all_sources()."""

    def test_empty_registry_returns_empty_list(self):
        """An empty registry should return an empty list."""
        reg = SourceRegistry()
        assert reg.get_all_sources() == []

    def test_returns_all_instances(self):
        """Should return instances for every registered source."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.register(MockSourceHighPriority)
        sources = reg.get_all_sources()
        assert len(sources) == 2
        names = {s.name for s in sources}
        assert names == {"mock", "mock_high"}

    def test_instances_are_source_base(self):
        """All returned items should be SourceBase instances."""
        reg = SourceRegistry()
        reg.register(MockSource)
        for src in reg.get_all_sources():
            assert isinstance(src, SourceBase)


# ======================================================================
# SourceRegistry.get_enabled_sources Tests
# ======================================================================


class TestGetEnabledSources:
    """Tests for SourceRegistry.get_enabled_sources()."""

    def test_returns_only_enabled(self):
        """Should exclude sources marked as disabled."""
        reg = SourceRegistry()
        reg.register(MockSource)  # enabled by default
        reg.register(MockSourceDisabled)  # disabled by default
        enabled = reg.get_enabled_sources()
        names = {s.name for s in enabled}
        assert "mock" in names
        assert "mock_disabled" not in names

    def test_empty_registry_returns_empty_list(self):
        """An empty registry should return an empty list."""
        reg = SourceRegistry()
        assert reg.get_enabled_sources() == []

    def test_all_enabled_returns_all(self):
        """When all sources are enabled, all should be returned."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.register(MockSourceHighPriority)
        enabled = reg.get_enabled_sources()
        assert len(enabled) == 2

    def test_disable_then_get_enabled(self):
        """Disabling a source should remove it from enabled list."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.register(MockSourceHighPriority)
        reg.disable_source("mock")
        enabled = reg.get_enabled_sources()
        names = {s.name for s in enabled}
        assert "mock" not in names
        assert "mock_high" in names


# ======================================================================
# SourceRegistry.list_sources Tests
# ======================================================================


class TestListSources:
    """Tests for SourceRegistry.list_sources()."""

    def test_returns_list_of_dicts(self):
        """list_sources should return a list of dictionaries."""
        reg = SourceRegistry()
        reg.register(MockSource)
        result = reg.list_sources()
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], dict)

    def test_dict_has_expected_keys(self):
        """Each dict should contain the expected keys."""
        reg = SourceRegistry()
        reg.register(MockSource)
        result = reg.list_sources()
        item = result[0]
        expected_keys = {"name", "display_name", "base_url", "capabilities", "enabled", "priority"}
        assert set(item.keys()) == expected_keys

    def test_dict_values_match_config(self):
        """Dict values should match the source config."""
        reg = SourceRegistry()
        reg.register(MockSource)
        item = reg.list_sources()[0]
        assert item["name"] == "mock"
        assert item["display_name"] == "Mock Source"
        assert item["base_url"] == "https://mock.example.com"
        assert item["enabled"] is True

    def test_sorted_by_priority_descending(self):
        """Sources should be sorted by priority, highest first."""
        reg = SourceRegistry()
        reg.register(MockSource)  # priority=100
        reg.register(MockSourceHighPriority)  # priority=200
        reg.register(MockSourceDisabled)  # priority=50
        result = reg.list_sources()
        priorities = [item["priority"] for item in result]
        assert priorities == sorted(priorities, reverse=True)
        assert result[0]["name"] == "mock_high"

    def test_empty_registry(self):
        """An empty registry should return an empty list."""
        reg = SourceRegistry()
        assert reg.list_sources() == []


# ======================================================================
# SourceRegistry.enable_source / disable_source Tests
# ======================================================================


class TestEnableDisableSource:
    """Tests for enable_source() and disable_source()."""

    def test_disable_source(self):
        """disable_source should set enabled to False."""
        reg = SourceRegistry()
        reg.register(MockSource)
        assert reg._enabled["mock"] is True
        reg.disable_source("mock")
        assert reg._enabled["mock"] is False

    def test_enable_source(self):
        """enable_source should set enabled to True."""
        reg = SourceRegistry()
        reg.register(MockSourceDisabled)
        assert reg._enabled["mock_disabled"] is False
        reg.enable_source("mock_disabled", True)
        assert reg._enabled["mock_disabled"] is True

    def test_enable_source_toggle(self):
        """enable_source with False should disable the source."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.enable_source("mock", False)
        assert reg._enabled["mock"] is False

    def test_disable_notifies_callback(self):
        """Disabling a source should trigger a 'disabled' callback event."""
        reg = SourceRegistry()
        reg.register(MockSource)
        events = []
        reg.add_callback(lambda event, data: events.append((event, data)))
        reg.disable_source("mock")
        assert ("disabled", "mock") in events

    def test_enable_notifies_callback(self):
        """Enabling a source should trigger an 'enabled' callback event."""
        reg = SourceRegistry()
        reg.register(MockSourceDisabled)
        events = []
        reg.add_callback(lambda event, data: events.append((event, data)))
        reg.enable_source("mock_disabled", True)
        assert ("enabled", "mock_disabled") in events

    def test_enable_nonexistent_does_nothing(self):
        """Enabling a non-existent source should not raise."""
        reg = SourceRegistry()
        reg.enable_source("nonexistent", True)  # Should not raise

    def test_disable_nonexistent_does_nothing(self):
        """Disabling a non-existent source should not raise."""
        reg = SourceRegistry()
        reg.disable_source("nonexistent")  # Should not raise


# ======================================================================
# SourceRegistry.add_callback Tests
# ======================================================================


class TestAddCallback:
    """Tests for SourceRegistry.add_callback()."""

    def test_callback_added_to_list(self):
        """add_callback should append to _callbacks."""
        reg = SourceRegistry()
        cb = MagicMock()
        reg.add_callback(cb)
        assert cb in reg._callbacks

    def test_callback_receives_register_event(self):
        """Callback should receive ('registered', name) on source registration."""
        reg = SourceRegistry()
        cb = MagicMock()
        reg.add_callback(cb)
        reg.register(MockSource)
        cb.assert_called_with("registered", "mock")

    def test_callback_receives_unregister_event(self):
        """Callback should receive ('unregistered', name) on source unregistration."""
        reg = SourceRegistry()
        reg.register(MockSource)
        cb = MagicMock()
        reg.add_callback(cb)
        reg.unregister("mock")
        cb.assert_called_with("unregistered", "mock")

    def test_multiple_callbacks(self):
        """Multiple callbacks should all be called."""
        reg = SourceRegistry()
        cb1 = MagicMock()
        cb2 = MagicMock()
        reg.add_callback(cb1)
        reg.add_callback(cb2)
        reg.register(MockSource)
        cb1.assert_called_once_with("registered", "mock")
        cb2.assert_called_once_with("registered", "mock")

    def test_callback_error_does_not_block_others(self):
        """If one callback raises, remaining callbacks should still run."""
        reg = SourceRegistry()
        bad_cb = MagicMock(side_effect=RuntimeError("callback error"))
        good_cb = MagicMock()
        reg.add_callback(bad_cb)
        reg.add_callback(good_cb)
        reg.register(MockSource)
        bad_cb.assert_called_once()
        good_cb.assert_called_once()


# ======================================================================
# SourceRegistry.get_stats Tests
# ======================================================================


class TestGetStats:
    """Tests for SourceRegistry.get_stats()."""

    def test_empty_registry_stats(self):
        """Empty registry should show zero counts."""
        reg = SourceRegistry()
        stats = reg.get_stats()
        assert stats["total_sources"] == 0
        assert stats["enabled_sources"] == 0
        assert stats["sources"] == []

    def test_stats_with_sources(self):
        """Stats should reflect registered and enabled counts."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.register(MockSourceHighPriority)
        reg.register(MockSourceDisabled)
        stats = reg.get_stats()
        assert stats["total_sources"] == 3
        # MockSource and MockSourceHighPriority are enabled; MockSourceDisabled is not
        assert stats["enabled_sources"] == 2

    def test_stats_sources_list(self):
        """stats['sources'] should match list_sources output."""
        reg = SourceRegistry()
        reg.register(MockSource)
        stats = reg.get_stats()
        assert len(stats["sources"]) == 1
        assert stats["sources"][0]["name"] == "mock"

    def test_stats_after_disable(self):
        """Disabling a source should reduce enabled_sources count."""
        reg = SourceRegistry()
        reg.register(MockSource)
        reg.register(MockSourceHighPriority)
        reg.disable_source("mock")
        stats = reg.get_stats()
        assert stats["total_sources"] == 2
        assert stats["enabled_sources"] == 1


# ======================================================================
# SourceRegistry.fetch_from_source Tests
# ======================================================================


class TestFetchFromSource:
    """Tests for SourceRegistry.fetch_from_source()."""

    def test_unknown_source_returns_error_page_result(self):
        """Fetching from an unknown source should return PageResult with error."""
        reg = SourceRegistry()
        result = asyncio.run(reg.fetch_from_source("nonexistent"))
        assert isinstance(result, PageResult)
        assert result.releases == []
        assert len(result.errors) == 1
        assert "nonexistent" in result.errors[0]

    def test_known_source_returns_page_result(self):
        """Fetching from a known source should return its PageResult."""
        reg = SourceRegistry()
        reg.register(MockSource)
        result = asyncio.run(reg.fetch_from_source("mock"))
        assert isinstance(result, PageResult)
        assert result.releases == []
        assert result.errors == []

    def test_known_source_with_releases(self):
        """Fetching from a source that returns releases should include them."""
        reg = SourceRegistry()
        reg.register(MockSourceHighPriority)
        result = asyncio.run(reg.fetch_from_source("mock_high"))
        assert isinstance(result, PageResult)
        assert len(result.releases) == 1
        assert result.releases[0].title == "Test Release"

    def test_source_fetch_error_returns_error_page_result(self):
        """If the source raises during fetch, PageResult with error is returned."""
        reg = SourceRegistry()
        reg.register(MockSourceFetchError)
        result = asyncio.run(reg.fetch_from_source("mock_error"))
        assert isinstance(result, PageResult)
        assert result.releases == []
        assert len(result.errors) == 1
        assert "Simulated fetch failure" in result.errors[0]

    def test_fetch_passes_page_and_mode(self):
        """Page and mode arguments should be forwarded to the source."""
        reg = SourceRegistry()

        class TrackingSource(SourceBase):
            call_args = None

            @classmethod
            def get_config(cls):
                return SourceConfig(
                    name="tracking",
                    display_name="Tracking",
                    base_url="https://tracking.example.com",
                )

            async def fetch_page(self, page=1, mode="movies", **kwargs):
                TrackingSource.call_args = (page, mode, kwargs)
                return PageResult(releases=[])

            def parse_release(self, raw_data):
                return None

        reg.register(TrackingSource)
        asyncio.run(reg.fetch_from_source("tracking", page=3, mode="tv"))
        assert TrackingSource.call_args[0] == 3
        assert TrackingSource.call_args[1] == "tv"


class TestGlobalRegistry:
    """Tests for the module-level get_registry() singleton."""

    def test_get_registry_initializes_once_under_concurrency(self, monkeypatch):
        original_registry = registry_module._registry
        registry_module._registry = None

        created = []
        barrier = threading.Barrier(8)

        class StubRegistry:
            def __init__(self):
                created.append("created")

            def discover_sources(self):
                time.sleep(0.02)

        monkeypatch.setattr(registry_module, "SourceRegistry", StubRegistry)

        results = []

        def worker():
            barrier.wait()
            results.append(registry_module.get_registry())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        try:
            assert len(created) == 1
            assert len(results) == 8
            assert len({id(result) for result in results}) == 1
        finally:
            registry_module._registry = original_registry
