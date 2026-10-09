"""Rate limiter construction tests (slowapi)."""

from __future__ import annotations

from contract_agent.security.rate_limit import build_limiter


def test_limiter_is_constructed(settings) -> None:
    limiter = build_limiter(settings)
    assert limiter is not None


def test_limiter_has_headers_enabled(settings) -> None:
    limiter = build_limiter(settings)
    assert limiter._headers_enabled is True


def test_limiter_default_limits_empty(settings) -> None:
    limiter = build_limiter(settings)
    # No global default — routes opt-in individually
    assert limiter._default_limits == []
