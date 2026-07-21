"""Process-wide HDEncode request authorization, pacing, and health."""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Callable, Iterator, Optional


class HDEncodeTrafficDenied(RuntimeError):
    """An HDEncode operation was refused before transport activity."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HDEncodeRequestCancelled(HDEncodeTrafficDenied):
    def __init__(self):
        super().__init__("cancelled", "HDEncode request cancelled before start")


_AUTHORIZED_CLASS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "scanhound_hdencode_authorized_class",
    default=None,
)


def require_transport_authorization(expected_class: Optional[str] = None) -> str:
    """Prove that a transport constructor is inside an approved request."""
    actual = _AUTHORIZED_CLASS.get()
    if actual is None:
        raise HDEncodeTrafficDenied(
            "unauthorized_transport",
            "HDEncode transport construction was not coordinator-authorized",
        )
    if expected_class is not None and actual != expected_class:
        raise HDEncodeTrafficDenied(
            "wrong_transport_class",
            f"Expected {expected_class!r} authorization, got {actual!r}",
        )
    return actual


def transport_authorized() -> bool:
    return _AUTHORIZED_CLASS.get() is not None


@dataclass(frozen=True)
class HDEncodeDecision:
    blocked: bool
    state: str
    reason_code: Optional[str] = None
    cooldown_until: Optional[str] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cancelled(observer: Optional[Callable[[], bool]]) -> bool:
    if observer is None:
        return False
    try:
        return bool(observer())
    except Exception:
        # A broken observer must fail closed rather than issue source traffic.
        return True


class HDEncodeTrafficCoordinator:
    """One process-wide policy boundary for every HDEncode operation."""

    _BLOCK_STATUSES = frozenset({403, 429, 503})
    _BLOCK_THRESHOLD = 3
    _MIN_START_INTERVAL = 2.0
    _HEALTH_CACHE_SECONDS = 5.0
    _CLASS_LIMITS = {
        "listing": 1,
        "detail": 3,
        "selenium": 1,
        "rss": 1,
    }

    def __init__(self):
        self._config = {}
        self._db = None
        self._state_lock = threading.RLock()
        self._pacing_lock = threading.Lock()
        self._last_start: Optional[float] = None
        self._semaphores = {
            name: threading.BoundedSemaphore(limit)
            for name, limit in self._CLASS_LIMITS.items()
        }
        self._block_streak = 0
        self._local_cooldown_until: Optional[datetime] = None
        self._health_cache = {}
        self._health_cache_at = 0.0
        self._metrics = {
            "started": {name: 0 for name in self._CLASS_LIMITS},
            "denied": {name: 0 for name in self._CLASS_LIMITS},
            "cancelled": {name: 0 for name in self._CLASS_LIMITS},
            "successes": 0,
            "block_responses": 0,
            "challenges": 0,
            "network_failures": 0,
        }

    def configure(self, config, db=None) -> None:
        """Attach the current application context without requiring bootstrap.

        ScanHound historically enables HDEncode by default.  Small parsing
        callers and legacy tests often provide a partial config that omits the
        switch entirely; that is an unconfigured/default context, not an
        explicit request to disable traffic.  A literal ``False`` remains the
        only off-switch value.

        A different config/database identity represents a new application or
        test context.  Clear volatile cooldown state in that case so a prior
        context cannot poison an otherwise independent parser.  Repeated
        configuration with the same production objects preserves the shared
        process-wide streak and cooldown.
        """
        normalized = config if isinstance(config, dict) else {}
        with self._state_lock:
            context_changed = (
                normalized is not self._config or db is not self._db
            )
            self._config = normalized
            # Assign None deliberately.  Retaining a previous context's DB is
            # unsafe and was the source of cross-test/global-state leakage.
            self._db = db
            self._health_cache = {}
            self._health_cache_at = 0.0
            if context_changed:
                self._block_streak = 0
                self._local_cooldown_until = None

    def _enabled(self) -> bool:
        # The application default is enabled.  Missing/partial configuration
        # therefore preserves legacy parsing, while any present non-True value
        # (False, 0, strings, None) fails closed.
        return self._config.get("hdencode_enabled", True) is True

    def _load_health(self) -> dict:
        now = time.monotonic()
        with self._state_lock:
            if now - self._health_cache_at < self._HEALTH_CACHE_SECONDS:
                return dict(self._health_cache)
            db = self._db
        health = {}
        if db is not None:
            try:
                snapshot = db.get_source_health()
                health = (snapshot or {}).get("hdencode", {})
            except Exception:
                # Health persistence is advisory; traffic still follows the
                # in-memory state and strict off switch.
                health = {}
        with self._state_lock:
            self._health_cache = dict(health or {})
            self._health_cache_at = now
        return health

    def _active_decision(self) -> HDEncodeDecision:
        if not self._enabled():
            return HDEncodeDecision(True, "disabled", "source_disabled")

        now = _utcnow()
        with self._state_lock:
            local_until = self._local_cooldown_until
        if local_until and local_until > now:
            return HDEncodeDecision(
                True,
                "cooldown",
                "local_cooldown",
                local_until.isoformat(),
            )

        health = self._load_health()
        state = str(health.get("state") or "unknown")
        reason = health.get("reason_code")
        cooldown_until = _parse_datetime(health.get("cooldown_until"))

        if state == "cooldown" and cooldown_until and cooldown_until > now:
            return HDEncodeDecision(
                True,
                state,
                reason,
                cooldown_until.isoformat(),
            )

        if state == "blocked":
            # Legacy blocked records did not always include an expiry. Hold them
            # for thirty minutes from the last update, then permit one probe.
            updated = _parse_datetime(health.get("updated_at"))
            until = cooldown_until or (
                updated + timedelta(minutes=30) if updated else None
            )
            if until and until > now:
                return HDEncodeDecision(True, state, reason, until.isoformat())

        return HDEncodeDecision(False, state, reason)

    def snapshot(self) -> dict:
        decision = self._active_decision()
        with self._state_lock:
            metrics = {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in self._metrics.items()
            }
            streak = self._block_streak
        return {
            "enabled": self._enabled(),
            "blocked": decision.blocked,
            "state": decision.state,
            "reason_code": decision.reason_code,
            "cooldown_until": decision.cooldown_until,
            "block_streak": streak,
            "metrics": metrics,
        }

    def _wait_for_start(
        self,
        request_class: str,
        stop_requested: Optional[Callable[[], bool]],
    ) -> None:
        while True:
            if _cancelled(stop_requested):
                with self._state_lock:
                    self._metrics["cancelled"][request_class] += 1
                raise HDEncodeRequestCancelled()
            with self._pacing_lock:
                now = time.monotonic()
                wait_seconds = 0.0
                if self._last_start is not None:
                    wait_seconds = max(
                        0.0,
                        self._MIN_START_INTERVAL - (now - self._last_start),
                    )
                if wait_seconds <= 0:
                    self._last_start = now
                    with self._state_lock:
                        self._metrics["started"][request_class] += 1
                    return
            time.sleep(min(wait_seconds, 0.1))

    @contextlib.contextmanager
    def request(
        self,
        request_class: str,
        *,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> Iterator[None]:
        """Authorize exactly one transport operation."""
        if request_class not in self._semaphores:
            raise ValueError(f"Unknown HDEncode request class: {request_class}")

        decision = self._active_decision()
        if decision.blocked:
            with self._state_lock:
                self._metrics["denied"][request_class] += 1
            raise HDEncodeTrafficDenied(
                decision.reason_code or decision.state,
                f"HDEncode traffic is {decision.state}",
            )

        semaphore = self._semaphores[request_class]
        acquired = False
        while not acquired:
            if _cancelled(stop_requested):
                with self._state_lock:
                    self._metrics["cancelled"][request_class] += 1
                raise HDEncodeRequestCancelled()
            acquired = semaphore.acquire(timeout=0.1)

        token = None
        try:
            # A request may have been blocked while waiting for capacity.
            decision = self._active_decision()
            if decision.blocked:
                with self._state_lock:
                    self._metrics["denied"][request_class] += 1
                raise HDEncodeTrafficDenied(
                    decision.reason_code or decision.state,
                    f"HDEncode traffic is {decision.state}",
                )
            self._wait_for_start(request_class, stop_requested)
            token = _AUTHORIZED_CLASS.set(request_class)
            yield
        finally:
            if token is not None:
                _AUTHORIZED_CLASS.reset(token)
            semaphore.release()

    def _persist_success(self) -> None:
        try:
            if self._db is not None:
                self._db.record_source_success("hdencode")
        except Exception:
            pass

    def _persist_failure(
        self,
        state: str,
        reason_code: str,
        cooldown_seconds: Optional[int],
    ) -> None:
        try:
            if self._db is not None:
                self._db.record_source_failure(
                    "hdencode",
                    state,
                    reason_code,
                    cooldown_seconds=cooldown_seconds,
                )
        except Exception:
            pass

    def observe_http_status(self, status_code: int) -> HDEncodeDecision:
        status = int(status_code)
        if 200 <= status < 400:
            with self._state_lock:
                self._block_streak = 0
                self._local_cooldown_until = None
                self._health_cache_at = 0.0
                self._metrics["successes"] += 1
            self._persist_success()
            return HDEncodeDecision(False, "healthy")

        if status not in self._BLOCK_STATUSES:
            return HDEncodeDecision(False, "degraded", f"http_{status}")

        with self._state_lock:
            self._block_streak += 1
            self._metrics["block_responses"] += 1
            streak = self._block_streak

        if streak < self._BLOCK_THRESHOLD:
            return HDEncodeDecision(False, "degraded", f"http_{status}")

        seconds = 15 * 60 if status == 429 else 30 * 60
        until = _utcnow() + timedelta(seconds=seconds)
        with self._state_lock:
            self._local_cooldown_until = until
            self._health_cache_at = 0.0
        self._persist_failure("cooldown", f"http_{status}", seconds)
        return HDEncodeDecision(
            True,
            "cooldown",
            f"http_{status}",
            until.isoformat(),
        )

    def observe_challenge(self, reason_code: str = "interactive_challenge") -> None:
        seconds = 60 * 60
        until = _utcnow() + timedelta(seconds=seconds)
        with self._state_lock:
            self._metrics["challenges"] += 1
            self._local_cooldown_until = until
            self._health_cache_at = 0.0
        self._persist_failure("cooldown", reason_code, seconds)

    def observe_network_failure(self, reason_code: str) -> None:
        with self._state_lock:
            self._metrics["network_failures"] += 1
        self._persist_failure("degraded", reason_code, None)


_COORDINATOR = HDEncodeTrafficCoordinator()


def configure_hdencode_coordinator(config, db=None) -> HDEncodeTrafficCoordinator:
    _COORDINATOR.configure(config, db)
    return _COORDINATOR


def get_hdencode_coordinator() -> HDEncodeTrafficCoordinator:
    return _COORDINATOR
