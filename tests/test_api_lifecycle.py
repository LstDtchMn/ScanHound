"""Regression tests for repeated API lifespans and registry ownership."""

import pytest
from fastapi.testclient import TestClient

from backend.api.dependencies import ServiceRegistry, registry
from backend.api.main import (
    _REGISTRY_LIFESPAN_FIELDS,
    _clear_registry_lifespan_state,
    _init_services,
    _prepare_registry_for_startup,
    _teardown_services,
    create_app,
)


def _fill_lifespan_fields(reg, value):
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        setattr(reg, field_name, value)
    reg.config = {"stale": True}


def test_prepare_registry_clears_every_lifespan_reference_and_shutdown_event():
    reg = ServiceRegistry()
    stale = object()
    _fill_lifespan_fields(reg, stale)
    reg.request_shutdown()

    _prepare_registry_for_startup(reg)

    assert reg.shutdown_requested is False
    assert reg.config == {}
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        assert getattr(reg, field_name) is None, field_name


def test_init_clears_stale_services_before_appservice_startup(monkeypatch):
    """The synchronous maintenance pass must never see a prior lifespan service."""
    import backend.app_service as app_service_module

    stale = object()
    _fill_lifespan_fields(registry, stale)
    registry.request_shutdown()

    class StartupObserved(Exception):
        pass

    class FakeAppService:
        def __init__(self):
            self.config = {}
            self.db = None

        def startup(self):
            assert registry.shutdown_requested is False
            assert registry.backend is self
            assert registry.db is None
            for field_name in _REGISTRY_LIFESPAN_FIELDS:
                if field_name == "backend":
                    continue
                assert getattr(registry, field_name) is None, field_name
            raise StartupObserved

    monkeypatch.setattr(app_service_module, "AppService", FakeAppService)
    with pytest.raises(StartupObserved):
        _init_services(registry)
    _clear_registry_lifespan_state(registry)


class _Raises:
    def stop(self):
        raise RuntimeError("stop failed")

    def shutdown(self):
        raise RuntimeError("shutdown failed")

    def close(self):
        raise RuntimeError("close failed")


def test_teardown_clears_all_references_even_when_shutdown_hooks_fail():
    reg = ServiceRegistry()
    _fill_lifespan_fields(reg, _Raises())

    _teardown_services(reg)

    assert reg.shutdown_requested is True
    assert reg.config == {}
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        assert getattr(reg, field_name) is None, field_name


def test_repeated_real_lifespans_start_with_empty_results(monkeypatch):
    """Exercise the failure family that previously went green only on CI retry."""
    monkeypatch.setenv("SCANHOUND_ALLOW_OPEN", "1")

    for _ in range(3):
        app = create_app(config_override={"plex_url": "", "plex_token": ""})
        with TestClient(app) as client:
            response = client.get("/results")
            assert response.status_code == 200
            assert response.json()["items"] == []
            assert response.json()["total"] == 0
        for field_name in _REGISTRY_LIFESPAN_FIELDS:
            assert getattr(registry, field_name) is None, field_name
        assert registry.shutdown_requested is True
