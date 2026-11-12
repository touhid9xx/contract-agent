"""Tests for :mod:`app.core.metrics` and the ``/metrics`` scrape route.

The interesting property here is not that a counter counts. It is that the
*shape* of the API makes a PII leak into a metric label impossible: a label value
must be a bounded lowercase token, so an email address or a phone number cannot
be recorded even by accident. That guard, and the promise that it never echoes
the offending value into an exception message, are what these tests pin.
"""

from __future__ import annotations

import pytest

from app.api.v1 import metrics as metrics_route  # noqa: F401  (import-time contract)
from app.core import metrics
from app.db.enums import Channel, NotificationStatus

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts and ends with empty samples."""
    metrics.reset()
    yield
    metrics.reset()


# --------------------------------------------------------------------------- #
# exposition format
# --------------------------------------------------------------------------- #


def test_a_counter_renders_help_type_and_sample() -> None:
    metrics.EXTRACTIONS_TOTAL.inc(status="ok")
    metrics.EXTRACTIONS_TOTAL.inc(status="ok")

    text = metrics.render()

    assert "# HELP extractions_total Contract extraction runs, by outcome." in text
    assert "# TYPE extractions_total counter" in text
    assert 'extractions_total{status="ok"} 2' in text


def test_a_counter_starts_at_zero_and_only_counts_what_was_recorded() -> None:
    assert metrics.EXTRACTIONS_TOTAL.value(status="ok") == 0.0

    metrics.EXTRACTIONS_TOTAL.inc(status="ok", amount=3)

    assert metrics.EXTRACTIONS_TOTAL.value(status="ok") == 3.0
    assert metrics.EXTRACTIONS_TOTAL.value(status="failed") == 0.0


def test_a_counter_refuses_to_decrease() -> None:
    with pytest.raises(ValueError, match="cannot decrease"):
        metrics.EXTRACTIONS_TOTAL.inc(status="ok", amount=-1)


def test_a_histogram_renders_cumulative_buckets_sum_and_count() -> None:
    metrics.EXTRACTION_CONFIDENCE.observe(0.55)
    metrics.EXTRACTION_CONFIDENCE.observe(0.93)

    text = metrics.render()

    assert "# TYPE extraction_confidence histogram" in text
    # 0.5 bucket holds nothing; 0.6 and every larger bucket hold the 0.55 sample;
    # the 1.0 bucket holds both.  Cumulative, as the format requires.
    assert 'extraction_confidence_bucket{le="0.5"} 0' in text
    assert 'extraction_confidence_bucket{le="0.6"} 1' in text
    assert 'extraction_confidence_bucket{le="1"} 2' in text
    assert 'extraction_confidence_bucket{le="+Inf"} 2' in text
    assert "extraction_confidence_count 2" in text
    assert "extraction_confidence_sum 1" in text


def test_a_scrape_with_nothing_recorded_still_names_every_family() -> None:
    text = metrics.render()

    for family in metrics.families():
        assert f"# HELP {family.name} " in text
        assert f"# TYPE {family.name} {family.kind}" in text


# --------------------------------------------------------------------------- #
# the PII guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "a.r@mail.com",
        "+8801700000000",
        "House 12, Road 5, Ban",
        "1985-03-12",
        "A. Rahman",
        "EMAIL",
        "",
        "x" * 33,
    ],
)
def test_a_label_value_that_could_be_pii_is_refused(value: str) -> None:
    with pytest.raises(metrics.LabelValueError):
        metrics.EXTRACTIONS_TOTAL.inc(status=value)


def test_the_refusal_names_the_label_but_never_repeats_the_value() -> None:
    secret = "a.r@mail.com"

    with pytest.raises(metrics.LabelValueError) as caught:
        metrics.EXTRACTIONS_TOTAL.inc(status=secret)

    assert secret not in str(caught.value)
    assert "status" in str(caught.value)


def test_a_refused_label_records_nothing() -> None:
    with pytest.raises(metrics.LabelValueError):
        metrics.EXTRACTIONS_TOTAL.inc(status="a.r@mail.com")

    assert "extractions_total{status=" not in metrics.render()


def test_a_non_string_label_value_is_refused_rather_than_stringified() -> None:
    with pytest.raises(metrics.LabelValueError):
        metrics.EXTRACTIONS_TOTAL.inc(status=1985)  # type: ignore[arg-type]


def test_a_wrong_label_set_is_refused() -> None:
    with pytest.raises(metrics.LabelValueError):
        metrics.EXTRACTIONS_TOTAL.inc(channel="email")
    with pytest.raises(metrics.LabelValueError):
        metrics.EXTRACTIONS_TOTAL.inc(status="ok", channel="email")


# --------------------------------------------------------------------------- #
# the values the wiring sites actually pass
# --------------------------------------------------------------------------- #


def test_every_channel_and_notification_status_is_a_legal_label_value() -> None:
    """Guard the call sites in ``dispatch``: an enum rename must fail here."""
    for channel in Channel:
        for status in NotificationStatus:
            metrics.NOTIFICATIONS_SENT_TOTAL.inc(channel=channel, status=status)

    text = metrics.render()
    assert 'notifications_sent_total{channel="email",status="sent"} 1' in text


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #


def test_the_scrape_route_serves_the_registry(client) -> None:
    metrics.EXTRACTIONS_TOTAL.inc(status="ok")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == metrics.CONTENT_TYPE
    assert 'extractions_total{status="ok"} 1' in response.text


def test_the_scrape_route_needs_no_credentials(client) -> None:
    """Prometheus cannot authenticate, so neither may this route."""
    assert client.get("/metrics").status_code == 200


def test_the_scrape_route_is_hidden_from_the_openapi_schema(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/metrics" not in paths
    assert "/health" in paths
