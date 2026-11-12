"""``GET /metrics`` — the Prometheus scrape endpoint.

Deliberately outside ``/api/v1``: the scrape path is an operational contract with
Prometheus, not a versioned product API that could gain a ``v2`` without breaking
scrapers. It sits beside ``/health`` for the same reason.

Unauthenticated, like every Prometheus target: a scrape carries no credentials, so
the endpoint carries no secrets either. That safety is structural rather than
promised — see :mod:`app.core.metrics`, where a label value that could hold PII is
rejected before it can be rendered.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core import metrics

router = APIRouter(tags=["ops"])


@router.get("/metrics", include_in_schema=False)
def scrape() -> PlainTextResponse:
    """Return every metric family in text exposition format."""
    return PlainTextResponse(metrics.render(), media_type=metrics.CONTENT_TYPE)
