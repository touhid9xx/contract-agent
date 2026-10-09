"""Rate limiting via slowapi.

Why slowapi?
    - FastAPI-native, decorator-based, Redis or in-memory backend
    - Per-IP / per-user limits configurable from Settings

"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from contract_agent.config import Settings


def build_limiter(settings: Settings) -> Limiter:
    """Create the global Limiter. Attach to app.state.limiter in main.py."""
    return Limiter(
        key_func=get_remote_address,
        default_limits=[],
        storage_uri="memory://",  # M4: switch to REDIS_URL for multi-worker
        headers_enabled=True,
    )
