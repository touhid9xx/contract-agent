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
# the baseline an alert rule needs
# --------------------------------------------------------------------------- #


def _sample_lines(family: str) -> list[str]:
    """The rendered sample lines for ``family``. ``HELP``/``TYPE`` are comments."""
    return [
        line
        for line in metrics.render().splitlines()
        if line.startswith(f"{family} ") or line.startswith(f"{family}{{")
    ]


def test_an_unlabelled_counter_reports_zero_before_it_is_incremented() -> None:
    """D10-109: the family D10-91's rule reads exists at zero, before it can fail.

    This is a defect pinned, not a preference. ``increase()`` subtracts two
    samples, so with none before the first failure Prometheus saw the series go
    absent → 1, and ``increase(...[1h]) > 0`` — the rule as the ledger records it —
    read "no data" through the exact event it exists to catch. A counter that has
    counted nothing is a zero; suppressing it until the first increment is what made
    the rule unable to fire at all.
    """
    assert _sample_lines("escalation_email_failed_total") == ["escalation_email_failed_total 0"]

    metrics.ESCALATION_EMAIL_FAILED_TOTAL.inc()

    assert _sample_lines("escalation_email_failed_total") == ["escalation_email_failed_total 1"]


def test_only_the_unlabelled_counter_reports_before_it_is_written() -> None:
    """The zero is one family wide, so absent stays absent everywhere else.

    A labelled family cannot be seeded: its series are named by outcome, and
    inventing them would publish zeros for outcomes no call site can produce. A
    histogram is a distribution, where zero observations are not a measurement of
    zero. So D10-108's "declared, with no sample" assertion keeps its meaning for
    the families it was written about, and this is the test that would notice if a
    future family started reporting a value nobody recorded.
    """
    reporting = {family.name for family in metrics.families() if _sample_lines(family.name)}

    assert reporting == {"escalation_email_failed_total"}
    # Spelled out as the two reasons rather than only as the set literal, so a
    # future family that breaks one of them fails on the rule it broke.
    assert _sample_lines("extractions_total") == []
    assert _sample_lines("extraction_confidence") == []


def test_the_baseline_is_a_zero_and_not_a_recorded_increment() -> None:
    """Reading the baseline is not recording it, and counting zero changes nothing."""
    assert metrics.ESCALATION_EMAIL_FAILED_TOTAL.value() == 0.0

    metrics.ESCALATION_EMAIL_FAILED_TOTAL.inc(amount=0)

    assert _sample_lines("escalation_email_failed_total") == ["escalation_email_failed_total 0"]


def test_reset_restores_the_baseline_rather_than_removing_it() -> None:
    """``reset`` empties samples; it must not remove the one series a counter has.

    Otherwise the regression above would depend on test ordering, and a run that
    followed any ``reset`` would pass for the wrong reason.
    """
    metrics.ESCALATION_EMAIL_FAILED_TOTAL.inc()

    metrics.reset()

    assert _sample_lines("escalation_email_failed_total") == ["escalation_email_failed_total 0"]


def test_a_refused_increment_leaves_the_baseline_in_place() -> None:
    with pytest.raises(ValueError, match="cannot decrease"):
        metrics.ESCALATION_EMAIL_FAILED_TOTAL.inc(amount=-1)

    assert _sample_lines("escalation_email_failed_total") == ["escalation_email_failed_total 0"]


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
