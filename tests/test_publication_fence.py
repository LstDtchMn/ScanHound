"""Phase 3 — foreground publication authority.

Encodes the two authority failures measured in Phase 2, against the real
production path rather than a simulation:

  * same-generation teardown  — shutdown requested, generation unchanged
  * generation rollover       — scan accepted under 10, publishing under 16

Both were observed on all five foreground scans in the attribution run. A fence
built on generation equality alone passes the first case, which is why the
predicate must include shutdown admission and why admission must be atomic.
"""
from __future__ import annotations

import threading

import pytest

from backend import scan_context as sc
from backend.api.dependencies import ServiceRegistry
from backend.api.routes.scanner import _fenced


@pytest.fixture(autouse=True)
def _tracing(monkeypatch):
    monkeypatch.setenv("SCANHOUND_SCAN_TRACE", "1")
    sc.reset_recent_operations()
    yield
    sc.reset_recent_operations()


def _accepted(reg):
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=reg.lifespan_generation)
    context.snapshot_entry(lifespan_generation=reg.lifespan_generation)
    return context


# ── the two measured failures ─────────────────────────────────────────

def test_no_publication_after_same_generation_shutdown():
    """Cause 1: generation is UNCHANGED, so equality alone would let it through."""
    reg = ServiceRegistry()
    reg.begin_lifespan()
    context = _accepted(reg)
    reg.request_shutdown()

    assert reg.lifespan_generation == context.accepted_lifespan_generation, (
        "the point of this case is that the generation still matches")

    published = []
    admitted = _fenced(reg, context, sc.PUBLISH_LAST_SCAN_ITEMS,
                       lambda: published.append("written"))

    assert admitted is False
    assert published == [], "must not publish during teardown"


def test_no_publication_after_generation_rollover():
    """Cause 2: scan c0b9ab57 was accepted under 10 and published under 16."""
    reg = ServiceRegistry()
    reg.begin_lifespan()
    context = _accepted(reg)
    for _ in range(6):
        reg.begin_lifespan()

    published = []
    admitted = _fenced(reg, context, sc.PUBLISH_WEBSOCKET,
                       lambda: published.append("written"))

    assert admitted is False
    assert published == [], "must not publish across a rollover"


def test_publication_is_admitted_under_its_own_live_generation():
    """The fence must not be a blanket refusal."""
    reg = ServiceRegistry()
    reg.begin_lifespan()
    context = _accepted(reg)

    published = []
    admitted = _fenced(reg, context, sc.PUBLISH_CONFIG,
                       lambda: published.append("written"))

    assert admitted is True
    assert published == ["written"]


# ── the refusal is recorded, not silently dropped ─────────────────────

def test_refused_publication_is_visible_in_the_trace():
    reg = ServiceRegistry()
    reg.begin_lifespan()
    context = _accepted(reg)
    reg.request_shutdown()

    _fenced(reg, context, sc.PUBLISH_NOTIFICATION, lambda: None)

    events = [e for e in context.trace.events()
              if e.stage == sc.PUBLISH_NOTIFICATION]
    assert len(events) == 1
    assert events[0].still_owns_lifespan is False, (
        "a refused publication must be auditable")


# ── lease semantics ───────────────────────────────────────────────────

def test_admission_closes_atomically_with_shutdown():
    reg = ServiceRegistry()
    generation = reg.begin_lifespan()

    with reg.acquire_publication(generation) as admitted:
        assert admitted is True
        assert reg.active_publications == 1
    assert reg.active_publications == 0

    reg.request_shutdown()
    with reg.acquire_publication(generation) as admitted:
        assert admitted is False
    assert reg.active_publications == 0, "a refused lease is not counted"


def test_lease_is_released_even_when_the_body_raises():
    reg = ServiceRegistry()
    generation = reg.begin_lifespan()

    with pytest.raises(RuntimeError):
        with reg.acquire_publication(generation) as admitted:
            assert admitted is True
            raise RuntimeError("publish blew up")

    assert reg.active_publications == 0, "a failed publish must not leak a lease"


def test_lease_does_not_hold_the_lock_across_the_body():
    """A slow publish must not block lifespan rollover.

    Round 5: the lease is the synchronisation token; holding the raw state lock
    across a WebSocket or auto-grab call would stall teardown.
    """
    reg = ServiceRegistry()
    generation = reg.begin_lifespan()
    rolled = threading.Event()

    with reg.acquire_publication(generation) as admitted:
        assert admitted is True

        def roll():
            reg.begin_lifespan()
            rolled.set()

        t = threading.Thread(target=roll)
        t.start()
        assert rolled.wait(timeout=5), "rollover blocked by a held publication"
        t.join(timeout=5)


def test_owns_lifespan_reads_generation_and_shutdown_coherently():
    """Both reads under one acquisition, so the answer is one instant."""
    reg = ServiceRegistry()
    generation = reg.begin_lifespan()
    assert reg.owns_lifespan(generation) is True
    reg.request_shutdown()
    assert reg.owns_lifespan(generation) is False
    reg.begin_lifespan()
    assert reg.owns_lifespan(generation) is False, "old generation stays refused"


# ── registries without the lease keep working ─────────────────────────

def test_registry_without_the_lease_still_publishes():
    """Test doubles predating acquire_publication must not silently stop."""
    class _OldRegistry:
        lifespan_generation = 3

    context = sc.new_operation(sc.ORIGIN_API_MANUAL, lifespan_generation=3)
    published = []
    admitted = _fenced(_OldRegistry(), context, sc.PUBLISH_LAST_SCAN_ITEMS,
                       lambda: published.append("written"))

    assert admitted is True
    assert published == ["written"]
