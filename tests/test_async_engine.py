import pytest
pytest.importorskip("backend.async_engine")

"""Tests for backend/async_engine.py (non-IO classes).

Covers:
- CircuitState enum values
- CircuitBreaker dataclass defaults and methods:
  - record_success: resets failure_count, HALF_OPEN -> CLOSED transition
  - record_failure: increments failure_count, CLOSED -> OPEN transition
  - can_execute: behaviour in CLOSED, OPEN, and HALF_OPEN states
  - reset via re-init / field defaults
- RateLimiter dataclass defaults and __post_init__
- EndpointConfig dataclass defaults
- ConnectionPool basic attribute initialization and register_endpoint
"""

import time
import pytest
from unittest.mock import patch

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytest.importorskip("backend.async_engine")

from backend.async_engine import (
    CircuitState,
    CircuitBreaker,
    RateLimiter,
    EndpointConfig,
    ConnectionPool,
)


# ===================================================================
# CircuitState enum
# ===================================================================

class TestCircuitState:

    def test_closed_value(self):
        assert CircuitState.CLOSED.value == "closed"

    def test_open_value(self):
        assert CircuitState.OPEN.value == "open"

    def test_half_open_value(self):
        assert CircuitState.HALF_OPEN.value == "half_open"

    def test_enum_has_three_members(self):
        assert len(CircuitState) == 3

    def test_members_are_distinct(self):
        states = [CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN]
        assert len(set(states)) == 3

    def test_access_by_value(self):
        assert CircuitState("closed") is CircuitState.CLOSED
        assert CircuitState("open") is CircuitState.OPEN
        assert CircuitState("half_open") is CircuitState.HALF_OPEN

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            CircuitState("invalid")


# ===================================================================
# CircuitBreaker dataclass defaults
# ===================================================================

class TestCircuitBreakerDefaults:

    def test_default_failure_threshold(self):
        cb = CircuitBreaker()
        assert cb.failure_threshold == 5

    def test_default_recovery_timeout(self):
        cb = CircuitBreaker()
        assert cb.recovery_timeout == 30.0

    def test_default_half_open_max_calls(self):
        cb = CircuitBreaker()
        assert cb.half_open_max_calls == 3

    def test_default_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_default_failure_count_zero(self):
        cb = CircuitBreaker()
        assert cb.failure_count == 0

    def test_default_success_count_zero(self):
        cb = CircuitBreaker()
        assert cb.success_count == 0

    def test_default_last_failure_time_zero(self):
        cb = CircuitBreaker()
        assert cb.last_failure_time == 0.0

    def test_default_half_open_calls_zero(self):
        cb = CircuitBreaker()
        assert cb.half_open_calls == 0

    def test_custom_values(self):
        cb = CircuitBreaker(failure_threshold=10, recovery_timeout=60.0, half_open_max_calls=5)
        assert cb.failure_threshold == 10
        assert cb.recovery_timeout == 60.0
        assert cb.half_open_max_calls == 5


# ===================================================================
# CircuitBreaker.record_success
# ===================================================================

class TestCircuitBreakerRecordSuccess:

    def test_resets_failure_count_in_closed_state(self):
        cb = CircuitBreaker()
        cb.failure_count = 3
        cb.record_success()
        assert cb.failure_count == 0

    def test_does_not_change_state_when_closed(self):
        cb = CircuitBreaker()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_increments_success_count(self):
        cb = CircuitBreaker(half_open_max_calls=3)
        cb.state = CircuitState.HALF_OPEN
        cb.success_count = 0

        cb.record_success()
        assert cb.success_count == 1
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_transitions_to_closed_after_threshold(self):
        cb = CircuitBreaker(half_open_max_calls=3)
        cb.state = CircuitState.HALF_OPEN
        cb.success_count = 0

        cb.record_success()  # 1
        cb.record_success()  # 2
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()  # 3 -> transition
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0

    def test_half_open_to_closed_with_max_calls_1(self):
        cb = CircuitBreaker(half_open_max_calls=1)
        cb.state = CircuitState.HALF_OPEN
        cb.success_count = 0

        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_success_in_open_state_resets_failure_count(self):
        """Even in OPEN state, record_success resets failure_count (OPEN is not HALF_OPEN)."""
        cb = CircuitBreaker()
        cb.state = CircuitState.OPEN
        cb.failure_count = 5

        cb.record_success()
        assert cb.failure_count == 0
        # State remains OPEN (only HALF_OPEN transitions to CLOSED)
        assert cb.state == CircuitState.OPEN


# ===================================================================
# CircuitBreaker.record_failure
# ===================================================================

class TestCircuitBreakerRecordFailure:

    def test_increments_failure_count(self):
        cb = CircuitBreaker()
        assert cb.failure_count == 0
        cb.record_failure()
        assert cb.failure_count == 1

    def test_sets_last_failure_time(self):
        cb = CircuitBreaker()
        before = time.time()
        cb.record_failure()
        after = time.time()
        assert before <= cb.last_failure_time <= after

    def test_transitions_to_open_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()  # 1
        cb.record_failure()  # 2
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()  # 3 -> threshold
        assert cb.state == CircuitState.OPEN

    def test_stays_open_after_threshold_exceeded(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.record_failure()  # Additional failure
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    def test_does_not_open_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_failure_in_half_open_increments_and_may_reopen(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.state = CircuitState.HALF_OPEN
        cb.failure_count = 1

        cb.record_failure()
        assert cb.failure_count == 2
        assert cb.state == CircuitState.OPEN


# ===================================================================
# CircuitBreaker.can_execute
# ===================================================================

class TestCircuitBreakerCanExecute:

    def test_closed_always_returns_true(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    def test_open_returns_false_before_recovery_timeout(self):
        cb = CircuitBreaker(recovery_timeout=30.0)
        cb.state = CircuitState.OPEN
        cb.last_failure_time = time.time()

        assert cb.can_execute() is False

    def test_open_transitions_to_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(recovery_timeout=10.0)
        cb.state = CircuitState.OPEN
        cb.last_failure_time = time.time() - 15  # 15 seconds ago, > 10s recovery

        result = cb.can_execute()
        assert result is True
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0
        assert cb.success_count == 0

    def test_half_open_allows_up_to_max_calls(self):
        cb = CircuitBreaker(half_open_max_calls=3)
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 0

        assert cb.can_execute() is True  # call 1
        assert cb.half_open_calls == 1

        assert cb.can_execute() is True  # call 2
        assert cb.half_open_calls == 2

        assert cb.can_execute() is True  # call 3
        assert cb.half_open_calls == 3

        assert cb.can_execute() is False  # exceeded

    def test_half_open_rejects_after_max_calls(self):
        cb = CircuitBreaker(half_open_max_calls=1)
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 0

        assert cb.can_execute() is True
        assert cb.half_open_calls == 1
        assert cb.can_execute() is False

    def test_open_to_half_open_uses_mocked_time(self):
        """Verify the recovery timeout comparison uses time.time()."""
        cb = CircuitBreaker(recovery_timeout=60.0)
        cb.state = CircuitState.OPEN
        cb.last_failure_time = 1000.0

        # Mock time.time() to return a value just past recovery
        with patch("backend.async_engine.time.time", return_value=1061.0):
            result = cb.can_execute()

        assert result is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_open_stays_open_with_mocked_time_before_recovery(self):
        cb = CircuitBreaker(recovery_timeout=60.0)
        cb.state = CircuitState.OPEN
        cb.last_failure_time = 1000.0

        with patch("backend.async_engine.time.time", return_value=1050.0):
            result = cb.can_execute()

        assert result is False
        assert cb.state == CircuitState.OPEN


# ===================================================================
# CircuitBreaker reset (via re-creating or manually resetting fields)
# ===================================================================

class TestCircuitBreakerReset:

    def test_new_instance_has_default_values(self):
        """Creating a fresh instance is effectively a reset."""
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0
        assert cb.last_failure_time == 0.0
        assert cb.half_open_calls == 0

    def test_manually_reset_after_failures(self):
        """Simulating a manual reset by resetting all mutable fields."""
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Reset to defaults
        cb.state = CircuitState.CLOSED
        cb.failure_count = 0
        cb.success_count = 0
        cb.last_failure_time = 0.0
        cb.half_open_calls = 0

        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.can_execute() is True

    def test_full_lifecycle_closed_open_halfopen_closed(self):
        """Full lifecycle: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0, half_open_max_calls=2)

        # CLOSED -> OPEN
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # OPEN -> HALF_OPEN (simulate recovery timeout elapsed)
        cb.last_failure_time = time.time() - 10
        assert cb.can_execute() is True
        assert cb.state == CircuitState.HALF_OPEN

        # HALF_OPEN -> CLOSED (enough successes)
        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0


# ===================================================================
# RateLimiter dataclass
# ===================================================================

class TestRateLimiter:

    def test_default_requests_per_second(self):
        rl = RateLimiter()
        assert rl.requests_per_second == 10.0

    def test_default_burst_size(self):
        rl = RateLimiter()
        assert rl.burst_size == 20

    def test_post_init_sets_tokens_to_burst_size(self):
        rl = RateLimiter(burst_size=15)
        assert rl.tokens == 15.0

    def test_custom_values(self):
        rl = RateLimiter(requests_per_second=5.0, burst_size=10)
        assert rl.requests_per_second == 5.0
        assert rl.burst_size == 10
        assert rl.tokens == 10.0

    def test_last_update_set_to_current_time(self):
        before = time.time()
        rl = RateLimiter()
        after = time.time()
        assert before <= rl.last_update <= after


# ===================================================================
# EndpointConfig dataclass
# ===================================================================

class TestEndpointConfig:

    def test_required_base_url(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.base_url == "https://api.example.com"

    def test_default_rate_limit(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.rate_limit == 10.0

    def test_default_burst_size(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.burst_size == 20

    def test_default_timeout(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.timeout == 30.0

    def test_default_max_retries(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.max_retries == 3

    def test_default_circuit_breaker_enabled(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.circuit_breaker is True

    def test_default_headers_empty_dict(self):
        ec = EndpointConfig(base_url="https://api.example.com")
        assert ec.headers == {}

    def test_custom_values(self):
        ec = EndpointConfig(
            base_url="https://api.example.com",
            rate_limit=5.0,
            burst_size=10,
            timeout=60.0,
            max_retries=5,
            circuit_breaker=False,
            headers={"Authorization": "Bearer token"},
        )
        assert ec.rate_limit == 5.0
        assert ec.burst_size == 10
        assert ec.timeout == 60.0
        assert ec.max_retries == 5
        assert ec.circuit_breaker is False
        assert ec.headers == {"Authorization": "Bearer token"}

    def test_headers_default_not_shared(self):
        """Each instance gets its own headers dict."""
        ec1 = EndpointConfig(base_url="https://a.com")
        ec2 = EndpointConfig(base_url="https://b.com")
        ec1.headers["X-Key"] = "val"
        assert "X-Key" not in ec2.headers


# ===================================================================
# ConnectionPool (non-IO parts)
# ===================================================================

class TestConnectionPool:

    def test_default_init_values(self):
        pool = ConnectionPool()
        assert pool.max_per_host == 10
        assert pool.max_total == 100
        assert pool.keepalive_timeout == 30.0

    def test_custom_init_values(self):
        pool = ConnectionPool(
            max_connections_per_host=20,
            max_total_connections=200,
            keepalive_timeout=60.0,
        )
        assert pool.max_per_host == 20
        assert pool.max_total == 200
        assert pool.keepalive_timeout == 60.0

    def test_session_initially_none(self):
        pool = ConnectionPool()
        assert pool._session is None

    def test_empty_dicts_on_init(self):
        pool = ConnectionPool()
        assert pool._rate_limiters == {}
        assert pool._circuit_breakers == {}
        assert pool._endpoints == {}

    def test_register_endpoint_stores_config(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com", rate_limit=5.0)
        pool.register_endpoint("test_api", config)

        assert "test_api" in pool._endpoints
        assert pool._endpoints["test_api"] is config

    def test_register_endpoint_creates_rate_limiter(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com", rate_limit=5.0, burst_size=10)
        pool.register_endpoint("test_api", config)

        rl = pool._rate_limiters.get("test_api")
        assert rl is not None
        assert rl.requests_per_second == 5.0
        assert rl.burst_size == 10

    def test_register_endpoint_creates_circuit_breaker_when_enabled(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com", circuit_breaker=True)
        pool.register_endpoint("test_api", config)

        cb = pool._circuit_breakers.get("test_api")
        assert cb is not None
        assert isinstance(cb, CircuitBreaker)

    def test_register_endpoint_no_circuit_breaker_when_disabled(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com", circuit_breaker=False)
        pool.register_endpoint("test_api", config)

        assert "test_api" not in pool._circuit_breakers

    def test_get_rate_limiter_returns_registered(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com")
        pool.register_endpoint("test_api", config)

        rl = pool.get_rate_limiter("test_api")
        assert rl is not None
        assert isinstance(rl, RateLimiter)

    def test_get_rate_limiter_returns_none_for_unknown(self):
        pool = ConnectionPool()
        assert pool.get_rate_limiter("unknown") is None

    def test_get_circuit_breaker_returns_registered(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com", circuit_breaker=True)
        pool.register_endpoint("test_api", config)

        cb = pool.get_circuit_breaker("test_api")
        assert cb is not None

    def test_get_circuit_breaker_returns_none_for_unknown(self):
        pool = ConnectionPool()
        assert pool.get_circuit_breaker("unknown") is None

    def test_get_endpoint_config_returns_registered(self):
        pool = ConnectionPool()
        config = EndpointConfig(base_url="https://api.test.com")
        pool.register_endpoint("test_api", config)

        retrieved = pool.get_endpoint_config("test_api")
        assert retrieved is config

    def test_get_endpoint_config_returns_none_for_unknown(self):
        pool = ConnectionPool()
        assert pool.get_endpoint_config("unknown") is None

    def test_register_multiple_endpoints(self):
        pool = ConnectionPool()
        config_a = EndpointConfig(base_url="https://a.com", rate_limit=1.0)
        config_b = EndpointConfig(base_url="https://b.com", rate_limit=2.0)

        pool.register_endpoint("a", config_a)
        pool.register_endpoint("b", config_b)

        assert pool.get_endpoint_config("a") is config_a
        assert pool.get_endpoint_config("b") is config_b
        assert pool.get_rate_limiter("a").requests_per_second == 1.0
        assert pool.get_rate_limiter("b").requests_per_second == 2.0
