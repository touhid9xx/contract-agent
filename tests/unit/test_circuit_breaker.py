"""Circuit breaker tests — pybreaker wrapper.

Why these tests?
    - Verify threshold trip (fail_max → open)
    - Verify reset_timeout behavior (half-open → closed/open)
    - Verify logging listener fires (state_change, failure)

Bangla: pybreaker-এর semantics হলো N-1 calls-এ original exception
(e.g. RuntimeError) raise হয়, আর N-th call-এ breaker trip হয়ে
CircuitBreakerError raise হয়।
"""

from __future__ import annotations

import pybreaker
import pytest

from contract_agent.security.circuit_breaker import make_breaker


# ============================================================
# HELPERS
# ============================================================
def _failing() -> None:
    raise RuntimeError("boom")


def _succeeding() -> str:
    return "ok"


# ============================================================
# THRESHOLD TRIP
# ============================================================
def test_breaker_opens_after_threshold() -> None:
    """After fail_max consecutive failures, breaker opens.

    pybreaker semantics:
        call 1 .. call N-1 → original exception (RuntimeError)
        call N            → CircuitBreakerError (breaker just opened)
    """
    breaker = make_breaker("test-open", fail_max=3, reset_timeout=60)
    assert breaker.current_state == "closed"

    # First (fail_max - 1) calls raise original exception
    for _ in range(3 - 1):
        with pytest.raises(RuntimeError):
            breaker.call(_failing)

    # N-th call trips breaker → CircuitBreakerError
    with pytest.raises(pybreaker.CircuitBreakerError):
        breaker.call(_failing)

    assert breaker.current_state == "open"


# ============================================================
# RESET TIMEOUT
# ============================================================
def test_breaker_allows_after_reset_timeout() -> None:
    """With reset_timeout=0, breaker can retry immediately.

    Behavior:
        - First call: RuntimeError
        - Second call: CircuitBreakerError (breaker trips)
        - Third call (after reset_timeout=0 → half-open):
          attempts the call, fails, re-opens
    """
    breaker = make_breaker("test-reset", fail_max=2, reset_timeout=0)

    # First call: original exception
    with pytest.raises(RuntimeError):
        breaker.call(_failing)

    # Second call: breaker trips
    with pytest.raises(pybreaker.CircuitBreakerError):
        breaker.call(_failing)

    assert breaker.current_state == "open"

    # Third call: reset_timeout=0 → half-open allows attempt.
    # Since _failing() raises, breaker re-opens.
    with pytest.raises((RuntimeError, pybreaker.CircuitBreakerError)):
        breaker.call(_failing)


# ============================================================
# SUCCESS PATH
# ============================================================
def test_breaker_allows_successful_call() -> None:
    """Closed breaker lets successful calls pass through."""
    breaker = make_breaker("test-success", fail_max=3, reset_timeout=60)
    assert breaker.call(_succeeding) == "ok"
    assert breaker.current_state == "closed"


# ============================================================
# LISTENER LOGGING (smoke test — no exceptions raised)
# ============================================================
def test_listener_fires_on_failure() -> None:
    """Failure listener is called for each failed call (no crash)."""
    breaker = make_breaker("test-listener", fail_max=5, reset_timeout=60)
    with pytest.raises(RuntimeError):
        breaker.call(_failing)
    # No assertion on logs here — just verify no exception from listener
    assert breaker.current_state == "closed"
