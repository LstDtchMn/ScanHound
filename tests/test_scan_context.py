"""Tests for the Phase 2 scan-operation attribution instrument.

The instrument has to be trustworthy before its output can be used as
evidence, so these cover the properties the peer round asked for: acceptance
vs entry ownership, origin coverage, executor attribution, explicit context
propagation, a bounded trace, and that turning it on changes no behaviour.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend import scan_context as sc


@pytest.fixture(autouse=True)
def _clean_registry():
    sc.reset_recent_operations()
    yield
    sc.reset_recent_operations()


@pytest.fixture
def tracing_on(monkeypatch):
    monkeypatch.setenv("SCANHOUND_SCAN_TRACE", "1")
    yield


# ── gating ────────────────────────────────────────────────────────────

def test_tracing_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SCANHOUND_SCAN_TRACE", raising=False)
    assert sc.tracing_enabled() is False
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    context.record(sc.RUN_SCAN_ENTERED)
    assert context.trace.events() == []


def test_tracing_records_when_enabled(tracing_on):
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    context.record(sc.RUN_SCAN_ENTERED)
    assert sc.RUN_SCAN_ENTERED in context.trace.stages()


def test_identity_is_captured_even_when_tracing_is_off(monkeypatch):
    """The gate suppresses the trace, never the identity.

    Ownership questions must stay answerable without turning tracing on.
    """
    monkeypatch.delenv("SCANHOUND_SCAN_TRACE", raising=False)
    scanner = object()
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=1, scanner=scanner)
    assert context.accepted_owner == (1, id(scanner))
    assert context.scan_uuid


# ── acceptance vs entry ownership ─────────────────────────────────────

def test_owner_crossing_is_detected(tracing_on):
    """Deterministic, not timing-dependent.

    The real race is: the request is accepted under one scanner, the worker
    thread is scheduled later, and by then a lifespan rollover has replaced
    it. Simulated here by mutating the owner between the two snapshots, so
    the assertion never depends on winning a race.
    """
    accepted_scanner = SimpleNamespace(name="scanner-A")
    later_scanner = SimpleNamespace(name="scanner-B")

    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=1, scanner=accepted_scanner)
    # ... lifespan rolls over before the worker runs ...
    context.snapshot_entry(lifespan_generation=2, scanner=later_scanner)

    assert context.accepted_lifespan_generation != context.entered_lifespan_generation
    assert context.accepted_scanner_id != context.entered_scanner_id
    assert context.crossed_ownership is True
    assert sc.ENTRY_OWNER_SNAPSHOTTED in context.trace.stages()


def test_same_owner_normal_path_does_not_report_crossing(tracing_on):
    scanner = SimpleNamespace(name="scanner-A")
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=7, scanner=scanner)
    context.snapshot_entry(lifespan_generation=7, scanner=scanner)

    assert context.accepted_owner == context.entered_owner
    assert context.crossed_ownership is False


def test_no_crossing_claim_before_entry_snapshot():
    """An un-entered operation must not look like a crossed one."""
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=1, scanner=object())
    assert context.entered_at_ns is None
    assert context.crossed_ownership is False


def test_generation_alone_distinguishes_recycled_object_ids(tracing_on):
    """id() can repeat across lifespans; the generation is what saves it."""
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=1, scanner=None)
    context.snapshot_entry(lifespan_generation=2, scanner=None)
    # Both scanner ids are None — only the generation differs.
    assert context.accepted_scanner_id == context.entered_scanner_id
    assert context.crossed_ownership is True


# ── origin coverage ───────────────────────────────────────────────────

def test_origins_are_distinct():
    origins = {
        sc.ORIGIN_API_MANUAL,
        sc.ORIGIN_API_SCHEDULER,
        sc.ORIGIN_BACKGROUND_PERIODIC,
        sc.ORIGIN_BACKGROUND_MANUAL,
        sc.ORIGIN_QT_SCAN_WORKER,
        sc.ORIGIN_DIRECT,
        sc.ORIGIN_UNKNOWN,
    }
    assert len(origins) == 7


def test_parent_operation_is_recorded():
    context = sc.new_operation(
        sc.ORIGIN_BACKGROUND_PERIODIC, parent_operation="background-cycle-3")
    assert context.parent_operation == "background-cycle-3"


# ── executor attribution ──────────────────────────────────────────────

def test_executor_prefixes_are_scan_specific_and_distinct():
    a = sc.new_operation(sc.ORIGIN_DIRECT)
    b = sc.new_operation(sc.ORIGIN_DIRECT)

    assert a.executor_prefix(sc.EXECUTOR_LISTING) != a.executor_prefix(sc.EXECUTOR_DETAIL)
    assert a.executor_prefix(sc.EXECUTOR_LISTING) != b.executor_prefix(sc.EXECUTOR_LISTING)
    for kind in (sc.EXECUTOR_LISTING, sc.EXECUTOR_DETAIL, sc.EXECUTOR_METADATA):
        assert kind in a.executor_prefix(kind)


def test_executor_prefix_reaches_real_thread_names(tracing_on):
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    prefix = context.executor_prefix(sc.EXECUTOR_DETAIL)
    seen = []

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix=prefix) as ex:
        for _ in range(2):
            ex.submit(lambda: seen.append(threading.current_thread().name))

    assert seen and all(name.startswith(prefix) for name in seen)


# ── explicit context propagation ──────────────────────────────────────

def test_submitted_work_carries_the_parent_scan_uuid(tracing_on):
    """Propagation is explicit; contextvars are deliberately not relied on."""
    context = sc.new_operation(sc.ORIGIN_DIRECT)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [
            ex.submit(
                sc.run_with_scan_context,
                context,
                sc.EXECUTOR_DETAIL,
                sc.DETAIL_STARTED,
                lambda value: value * 2,
                n,
            )
            for n in range(4)
        ]
        results = sorted(f.result() for f in futures)

    assert results == [0, 2, 4, 6], "wrapper must not alter the return value"
    started = [e for e in context.trace.events() if e.stage == sc.DETAIL_STARTED]
    assert len(started) == 4
    assert all(e.scan_uuid == context.scan_uuid for e in started)
    assert all(e.executor_kind == sc.EXECUTOR_DETAIL for e in started)


def test_wrapper_propagates_exceptions_unchanged():
    context = sc.new_operation(sc.ORIGIN_DIRECT)

    def boom():
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        sc.run_with_scan_context(
            context, sc.EXECUTOR_DETAIL, sc.DETAIL_STARTED, boom)


def test_wrapper_works_without_a_context():
    assert sc.run_with_scan_context(None, None, None, lambda: 42) == 42


# ── bounded trace ─────────────────────────────────────────────────────

def test_trace_is_bounded(tracing_on):
    context = sc.ScanOperationContext(
        scan_uuid="bounded", origin=sc.ORIGIN_DIRECT,
        trace=sc.ScanTrace(maxlen=10))
    for _ in range(500):
        context.record(sc.DETAIL_STARTED)
    assert len(context.trace) == 10


def test_retained_operations_are_bounded():
    for _ in range(sc._MAX_RETAINED_OPERATIONS * 3):
        sc.new_operation(sc.ORIGIN_DIRECT)
    assert len(sc.recent_operations()) == sc._MAX_RETAINED_OPERATIONS


def test_trace_is_thread_safe(tracing_on):
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    # new_operation() already recorded ACCEPTED, so measure the delta rather
    # than assuming an empty trace.
    baseline = len(context.trace)

    def hammer():
        for _ in range(200):
            context.record(sc.DETAIL_STARTED)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(context.trace) == baseline + 1600


def test_trace_carries_no_urls_or_credentials(tracing_on):
    """The trace should stay safe to paste into a review document."""
    context = sc.new_operation(sc.ORIGIN_DIRECT, source_kind="hdencode")
    context.record(sc.LISTING_TRANSPORT_CONSTRUCTED,
                   executor_kind=sc.EXECUTOR_LISTING)
    for event in context.trace.events():
        rendered = str(event.as_dict())
        assert "http" not in rendered
        assert "?" not in rendered


# ── sequencing ────────────────────────────────────────────────────────

def test_sequence_numbers_are_monotonic(tracing_on):
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    for _ in range(20):
        context.record(sc.DETAIL_STARTED)
    sequences = [e.sequence for e in context.trace.events()]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_describe_reports_crossing(tracing_on):
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=1, scanner=object())
    context.snapshot_entry(lifespan_generation=2, scanner=object())
    rendered = sc.describe([context])
    assert "crossed=True" in rendered
    assert sc.ORIGIN_API_MANUAL in rendered


# ── round 4 follow-up: post-entry crossing and completion markers ──────

def test_entry_crossing_property_is_narrowly_scoped(tracing_on):
    """The acceptance-to-entry comparison must not be read as a lifespan claim.

    Round 4 P1: reporting `crossed_ownership` as "no scan crossed a lifespan"
    overstates it. An operation can enter under its own owner and only later
    outlive a rollover.
    """
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=1, scanner=None)
    context.snapshot_entry(lifespan_generation=1, scanner=None)

    assert context.crossed_ownership_at_entry is False
    assert context.observed_post_entry_crossing is False
    assert context.crossed_lifespan is False

    # ... the lifespan rolls over while this operation is still running ...
    context.record(sc.PUBLISH_LAST_SCAN_ITEMS, active_lifespan_generation=2)

    assert context.crossed_ownership_at_entry is False, "entry check unchanged"
    assert context.observed_post_entry_crossing is True
    assert context.crossed_lifespan is True, "the wider question is now answered"


def test_live_generation_is_not_copied_from_the_entry_snapshot(tracing_on):
    context = sc.new_operation(
        sc.ORIGIN_API_MANUAL, lifespan_generation=4, scanner=None)
    context.snapshot_entry(lifespan_generation=4, scanner=None)
    context.record(sc.PUBLISH_CONFIG, active_lifespan_generation=9,
                   still_owns_lifespan=False)

    event = [e for e in context.trace.events() if e.stage == sc.PUBLISH_CONFIG][0]
    assert event.active_lifespan_generation == 9
    assert event.still_owns_lifespan is False
    assert event.lifespan_generation == 4, "entry snapshot preserved separately"


def test_worker_completion_is_recorded_unconditionally(tracing_on):
    context = sc.new_operation(sc.ORIGIN_DIRECT)

    sc.run_with_scan_context(
        context, sc.EXECUTOR_LISTING, sc.LISTING_STARTED, lambda: None)
    with pytest.raises(ValueError):
        sc.run_with_scan_context(
            context, sc.EXECUTOR_LISTING, sc.LISTING_STARTED,
            lambda: (_ for _ in ()).throw(ValueError("boom")))

    finished = [e for e in context.trace.events() if e.stage == sc.WORKER_FINISHED]
    assert len(finished) == 2, "recorded on the failing path too"


def test_worker_completion_is_timestamped_after_start(tracing_on):
    """Ordering must be decidable on the monotonic clock, not inferred."""
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    sc.run_with_scan_context(
        context, sc.EXECUTOR_LISTING, sc.LISTING_STARTED, lambda: None)

    events = {e.stage: e for e in context.trace.events()}
    assert events[sc.WORKER_FINISHED].monotonic_ns >= events[sc.LISTING_STARTED].monotonic_ns


def test_current_operation_is_bound_inside_worker_and_cleared_after(tracing_on):
    context = sc.new_operation(sc.ORIGIN_DIRECT)
    assert sc.current_operation_uuid() is None

    seen = sc.run_with_scan_context(
        context, sc.EXECUTOR_DETAIL, sc.DETAIL_STARTED,
        sc.current_operation_uuid)

    assert seen == context.scan_uuid, "netwatch can attribute without thread names"
    assert sc.current_operation_uuid() is None, "binding must not leak"


def test_binding_is_per_thread(tracing_on):
    outer = sc.new_operation(sc.ORIGIN_DIRECT)
    seen = {}

    def worker():
        seen["inside"] = sc.current_operation_uuid()

    with sc.bind_current_operation(outer):
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        seen["outer"] = sc.current_operation_uuid()

    assert seen["outer"] == outer.scan_uuid
    assert seen["inside"] is None, "a different thread must not inherit the binding"
