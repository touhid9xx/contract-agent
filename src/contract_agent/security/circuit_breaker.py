"""Circuit breaker wrapper — pybreaker.

Why?
    - Prevent cascading failure when external deps (Ollama, textbee, SMTP) go down
    - States: CLOSED → OPEN → HALF_OPEN → CLOSED
"""

from __future__ import annotations

import pybreaker

from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


class LoggingListener(pybreaker.CircuitBreakerListener):
    """Logs state transitions — critical for ops."""

    def state_change(
        self,
        cb: pybreaker.CircuitBreaker,
        old_state: pybreaker.CircuitBreakerState | None,
        new_state: pybreaker.CircuitBreakerState | None,
    ) -> None:
        # pybreaker's base signature allows None for old/new state
        # (e.g., on first transition). Match it exactly to satisfy LSP.
        logger.warning(
            "circuit_breaker_state_change",
            breaker=cb.name,
            old=type(old_state).__name__ if old_state else "None",
            new=type(new_state).__name__ if new_state else "None",
        )

    def failure(self, cb: pybreaker.CircuitBreaker, exc: BaseException) -> None:
        logger.error("circuit_breaker_failure", breaker=cb.name, error=str(exc))

    def success(self, cb: pybreaker.CircuitBreaker) -> None:
        logger.debug("circuit_breaker_success", breaker=cb.name)


def make_breaker(
    name: str,
    *,
    fail_max: int = 5,
    reset_timeout: int = 60,
) -> pybreaker.CircuitBreaker:
    """Factory for a named circuit breaker with logging."""
    return pybreaker.CircuitBreaker(
        fail_max=fail_max,
        reset_timeout=reset_timeout,
        name=name,
        listeners=[LoggingListener()],
    )


# Pre-built breakers for known external deps
ollama_breaker = make_breaker("ollama", fail_max=3, reset_timeout=60)
textbee_breaker = make_breaker("textbee", fail_max=5, reset_timeout=60)
smtp_breaker = make_breaker("smtp", fail_max=5, reset_timeout=60)
voice_breaker = make_breaker("voice", fail_max=3, reset_timeout=120)
