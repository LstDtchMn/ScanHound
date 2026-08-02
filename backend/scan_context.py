"""Scan-operation identity and tracing.

Deliberately framework-agnostic: ``ScannerService`` is driven by the FastAPI
routes, the background pre-cache scanner, the Qt controller and direct tests,
so this module must not import or depend on any of them. The FastAPI layer
supplies registry-generation data when it constructs a context; the scan engine
only ever sees the neutral interface below.

**Why this exists.** A foreground scan thread does not own the scanner that
accepted its request. ``_run_scan`` receives the mutable ``ServiceRegistry``
and dereferences ``reg.scanner`` whenever the OS thread happens to be
scheduled, which may be after a lifespan rollover has replaced it. Recording
the owner at *acceptance* and again at *entry* makes that crossing visible: a
mismatch is the evidence.

**Identity only.** The context stores ``id(scanner)`` and the lifespan
generation, never a strong reference to the scanner. Holding one would keep the
accepted scanner alive and change the very old/new/inert distribution being
measured — and it would be the beginning of the ownership fix rather than
neutral instrumentation. Phase 3 replaces identity capture with a real owned
reference; this phase must not.

**Off by default.** Constructing a context is cheap (a UUID and a few ints) and
always happens. Recording trace events happens only when
``SCANHOUND_SCAN_TRACE=1``, so production pays essentially nothing but can be
switched on against a live container without a code change.
"""
from __future__ import annotations

import itertools
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Iterable, Optional

# ── origins ───────────────────────────────────────────────────────────
# Which caller started this scan. "unknown"/"direct" keep direct callers and
# older test doubles observable without forcing them to understand lifespans.
ORIGIN_API_MANUAL = "api_manual"
ORIGIN_API_SCHEDULER = "api_scheduler"
ORIGIN_BACKGROUND_PERIODIC = "background_periodic"
ORIGIN_BACKGROUND_MANUAL = "background_manual"
ORIGIN_QT_SCAN_WORKER = "qt_scan_worker"
ORIGIN_DIRECT = "direct"
ORIGIN_UNKNOWN = "unknown"

# ── executor kinds ────────────────────────────────────────────────────
EXECUTOR_LISTING = "listing"
EXECUTOR_DETAIL = "detail"
EXECUTOR_METADATA = "metadata"

# ── milestones ────────────────────────────────────────────────────────
ACCEPTED = "accepted"
THREAD_STARTED = "thread_started"
ENTRY_OWNER_SNAPSHOTTED = "entry_owner_snapshotted"
SLOT_ATTEMPTED = "slot_attempted"
SLOT_ACQUIRED = "slot_acquired"
SLOT_REJECTED = "slot_rejected"
RUN_SCAN_ENTERED = "run_scan_entered"
EVENT_LOOP_CREATED = "event_loop_created"

LISTING_EXECUTOR_CREATED = "listing_executor_created"
LISTING_SUBMITTED = "listing_submitted"
LISTING_STARTED = "listing_started"
LISTING_TRANSPORT_CONSTRUCTED = "listing_transport_constructed"
LISTING_FINISHED = "listing_finished"

DETAIL_EXECUTOR_CREATED = "detail_executor_created"
DETAIL_SUBMITTED = "detail_submitted"
DETAIL_STARTED = "detail_started"
DETAIL_FINISHED = "detail_finished"

METADATA_EXECUTOR_CREATED = "metadata_executor_created"
METADATA_SUBMITTED = "metadata_submitted"
METADATA_STARTED = "metadata_started"
METADATA_FINISHED = "metadata_finished"

STOP_REQUESTED = "stop_requested"
RESULTS_READY = "results_ready"

PUBLISH_LAST_SCAN_ITEMS = "publish_last_scan_items_attempted"
PUBLISH_WEBSOCKET = "publish_websocket_attempted"
PUBLISH_CONFIG = "publish_config_attempted"
PUBLISH_NOTIFICATION = "publish_notification_attempted"
PUBLISH_AUTOGRAB = "publish_autograb_attempted"

EXECUTOR_SHUTDOWN_STARTED = "executor_shutdown_started"
EXECUTOR_SHUTDOWN_FINISHED = "executor_shutdown_finished"
SLOT_RELEASED = "slot_released"
THREAD_FINISHED = "thread_finished"

_ENV_ENABLE = "SCANHOUND_SCAN_TRACE"

# Bounded so a long scan cannot grow an unbounded process-global list.
_MAX_EVENTS_PER_OPERATION = 2000
_MAX_RETAINED_OPERATIONS = 64

_sequence = itertools.count(1)


def tracing_enabled() -> bool:
    """Read the env gate on every call so tests can toggle it per-test."""
    return os.environ.get(_ENV_ENABLE, "") == "1"


@dataclass(frozen=True)
class ScanTraceEvent:
    sequence: int
    monotonic_ns: int
    scan_uuid: str
    stage: str
    origin: str
    thread_name: str
    thread_ident: Optional[int]
    thread_native_id: Optional[int]
    lifespan_generation: Optional[int]
    scanner_id: Optional[int]
    executor_kind: Optional[str]
    source_kind: Optional[str]

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "monotonic_ns": self.monotonic_ns,
            "scan_uuid": self.scan_uuid,
            "stage": self.stage,
            "origin": self.origin,
            "thread_name": self.thread_name,
            "thread_ident": self.thread_ident,
            "thread_native_id": self.thread_native_id,
            "lifespan_generation": self.lifespan_generation,
            "scanner_id": self.scanner_id,
            "executor_kind": self.executor_kind,
            "source_kind": self.source_kind,
        }


class ScanTrace:
    """Thread-safe, bounded, monotonic event sink for one scan operation.

    Never stores URLs, query strings, tokens or headers — a short source label
    is all the ownership question needs, and the trace should stay safe to dump
    into a log or a review document.
    """

    def __init__(self, maxlen: int = _MAX_EVENTS_PER_OPERATION):
        self._events: Deque[ScanTraceEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, event: ScanTraceEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self) -> list:
        with self._lock:
            return list(self._events)

    def stages(self) -> list:
        return [e.stage for e in self.events()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


@dataclass
class ScanOperationContext:
    """Identity of one scan operation, from acceptance through completion."""

    scan_uuid: str
    origin: str
    parent_operation: Optional[str] = None

    # Snapshot taken where the scan was accepted / invoked.
    accepted_at_ns: int = 0
    accepted_lifespan_generation: Optional[int] = None
    accepted_scanner_id: Optional[int] = None

    # Snapshot taken when the worker actually begins executing.
    entered_at_ns: Optional[int] = None
    entered_lifespan_generation: Optional[int] = None
    entered_scanner_id: Optional[int] = None

    source_kind: Optional[str] = None
    trace: ScanTrace = field(default_factory=ScanTrace)

    # ── owner tuples ──────────────────────────────────────────────────

    @property
    def accepted_owner(self) -> tuple:
        return (self.accepted_lifespan_generation, self.accepted_scanner_id)

    @property
    def entered_owner(self) -> tuple:
        return (self.entered_lifespan_generation, self.entered_scanner_id)

    @property
    def crossed_ownership(self) -> bool:
        """True when the worker began under a different owner than accepted it.

        Only meaningful once :meth:`snapshot_entry` has run; before that the
        entry tuple is ``(None, None)`` and no claim should be made.
        """
        if self.entered_at_ns is None:
            return False
        return self.accepted_owner != self.entered_owner

    # ── recording ─────────────────────────────────────────────────────

    def record(
        self,
        stage: str,
        *,
        executor_kind: Optional[str] = None,
        lifespan_generation: Optional[int] = None,
        scanner_id: Optional[int] = None,
        source_kind: Optional[str] = None,
    ) -> None:
        if not tracing_enabled():
            return
        current = threading.current_thread()
        self.trace.record(ScanTraceEvent(
            sequence=next(_sequence),
            monotonic_ns=time.monotonic_ns(),
            scan_uuid=self.scan_uuid,
            stage=stage,
            origin=self.origin,
            thread_name=current.name,
            thread_ident=current.ident,
            thread_native_id=getattr(current, "native_id", None),
            lifespan_generation=(
                lifespan_generation
                if lifespan_generation is not None
                else self.entered_lifespan_generation
            ),
            scanner_id=(
                scanner_id if scanner_id is not None
                else self.entered_scanner_id
            ),
            executor_kind=executor_kind,
            source_kind=source_kind or self.source_kind,
        ))

    def snapshot_entry(
        self,
        *,
        lifespan_generation: Optional[int] = None,
        scanner: Any = None,
    ) -> None:
        """Capture the owner as seen by the worker thread, identity only."""
        self.entered_at_ns = time.monotonic_ns()
        self.entered_lifespan_generation = lifespan_generation
        self.entered_scanner_id = id(scanner) if scanner is not None else None
        self.record(ENTRY_OWNER_SNAPSHOTTED)

    def executor_prefix(self, kind: str) -> str:
        """Thread-name prefix for an executor owned by this operation.

        An evidence aid only. Ownership is carried by passing this context into
        submitted callables, never by parsing a thread name.
        """
        return f"scan-{self.scan_uuid[:8]}-{kind}"


# ── construction ──────────────────────────────────────────────────────

_recent: Deque[ScanOperationContext] = deque(maxlen=_MAX_RETAINED_OPERATIONS)
_recent_lock = threading.Lock()


def new_operation(
    origin: str,
    *,
    parent_operation: Optional[str] = None,
    lifespan_generation: Optional[int] = None,
    scanner: Any = None,
    source_kind: Optional[str] = None,
) -> ScanOperationContext:
    """Create a context at the point a scan is accepted or invoked."""
    context = ScanOperationContext(
        scan_uuid=str(uuid.uuid4()),
        origin=origin,
        parent_operation=parent_operation,
        accepted_at_ns=time.monotonic_ns(),
        accepted_lifespan_generation=lifespan_generation,
        accepted_scanner_id=id(scanner) if scanner is not None else None,
        source_kind=source_kind,
    )
    context.record(ACCEPTED)
    with _recent_lock:
        _recent.append(context)
    return context


def recent_operations() -> list:
    """Recently created contexts, newest last. For tests and diagnostics."""
    with _recent_lock:
        return list(_recent)


def reset_recent_operations() -> None:
    """Drop retained contexts. Tests use this for isolation."""
    with _recent_lock:
        _recent.clear()


def run_with_scan_context(
    context: Optional[ScanOperationContext],
    executor_kind: Optional[str],
    started_stage: Optional[str],
    fn: Callable,
    *args,
    **kwargs,
):
    """Wrapper for work submitted to an executor.

    Context propagation is explicit on purpose: neither
    ``loop.run_in_executor`` nor ``ThreadPoolExecutor.submit`` carries
    contextvars in every supported path, so the operation is passed rather
    than inferred.
    """
    if context is not None and started_stage:
        context.record(started_stage, executor_kind=executor_kind)
    return fn(*args, **kwargs)


def describe(contexts: Optional[Iterable[ScanOperationContext]] = None) -> str:
    """Human-readable dump of contexts, for test failures and review docs."""
    contexts = list(contexts) if contexts is not None else recent_operations()
    lines = []
    for c in contexts:
        lines.append(
            f"scan {c.scan_uuid[:8]} origin={c.origin} "
            f"parent={c.parent_operation} "
            f"accepted_owner={c.accepted_owner} "
            f"entered_owner={c.entered_owner} "
            f"crossed={c.crossed_ownership}"
        )
        for event in c.trace.events():
            lines.append(
                f"    {event.sequence:6d} {event.stage:34s} "
                f"thread={event.thread_name} "
                f"executor={event.executor_kind}"
            )
    return "\n".join(lines)
