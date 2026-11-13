"""Prometheus metrics, without a Prometheus client dependency.

The exposition format is small and stable enough to emit directly, which keeps the
runtime free of a client library that would otherwise have to be pinned, audited
and mirrored for the offline dev path. What is *not* small is the rule that makes
the metrics safe to publish: **a label value is never free text.**

Spec §9 requires that no PII reaches metric labels, and §10 that PII is not used as
a lookup key. Both are enforced structurally here rather than by review: every
label value must match :data:`LABEL_VALUE_RE`, a bounded lowercase token, so a
caller cannot pass an e-mail address, a phone number or a contract id even by
accident. The rejected value is never echoed in the exception message, because a
message carrying the offending value would reintroduce exactly the leak the check
exists to prevent — an error string ends up in logs.

Metric families are process-local and cumulative for the life of the process, which
is what a Prometheus scrape expects (the server computes rates). :func:`reset` is
for tests only.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Final

#: Media type from the Prometheus text exposition format 0.0.4.
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: The only shape a label value may take. Deliberately excludes ``@``, ``+``,
#: whitespace and any character that a phone number, e-mail or address needs, and
#: demands a leading letter so a bare number (a date, an amount, a phone) cannot
#: slip through as a "token".
LABEL_VALUE_RE: Final = re.compile(r"\A[a-z][a-z0-9_.-]{0,31}\Z")

#: Buckets for confidence observations: a field's confidence is a fraction in
#: ``[0, 1]``, so the interesting resolution is entirely in the top decades.
CONFIDENCE_BUCKETS: Final = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.99, 1.0)


class LabelValueError(ValueError):
    """A label value is not a bounded lowercase token, so it may carry PII."""


def _check_label(name: str, value: object) -> str:
    """Return ``value`` if it is a safe label token, else raise.

    The message names the label and the violated rule but *never* the value: see
    the module docstring.
    """
    if not isinstance(value, str) or LABEL_VALUE_RE.match(value) is None:
        raise LabelValueError(
            f"metric label {name!r} must be a lowercase token of at most 32 "
            "characters that starts with a letter (e.g. 'email', 'low_confidence'); "
            "refusing to publish this value"
        )
    return value


def _format_value(value: float) -> str:
    """Render a sample value the way Prometheus expects to parse it."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(round(value, 9))


class _Family:
    """Shared label bookkeeping for one metric name."""

    kind: str = "untyped"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.labelnames):
            missing = sorted(set(self.labelnames) - set(labels))
            extra = sorted(set(labels) - set(self.labelnames))
            raise LabelValueError(
                f"metric {self.name!r} expects labels {list(self.labelnames)}, "
                f"got missing={missing} extra={extra}"
            )
        return tuple(_check_label(name, labels[name]) for name in self.labelnames)

    def _render_sample(self, out: list[str], labels: tuple[str, ...], value: float) -> None:
        if not self.labelnames:
            out.append(f"{self.name} {_format_value(value)}")
            return
        pairs = ",".join(
            f'{name}="{label}"' for name, label in zip(self.labelnames, labels, strict=True)
        )
        out.append(f"{self.name}{{{pairs}}} {_format_value(value)}")

    def _header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.kind}"]

    def render(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(_Family):
    """A monotonically increasing sample."""

    kind = "counter"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"counter {self.name!r} cannot decrease")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        lines = self._header()
        with self._lock:
            for key, value in sorted(self._values.items()):
                self._render_sample(lines, key, value)
        return lines


class Gauge(_Family):
    """A sample that may go up or down."""

    kind = "gauge"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        lines = self._header()
        with self._lock:
            for key, value in sorted(self._values.items()):
                self._render_sample(lines, key, value)
        return lines


class Histogram(_Family):
    """A bucketed distribution with cumulative buckets and a running sum."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] = CONFIDENCE_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labelnames)
        # Sorted so the rendered buckets are ascending, which the format requires.
        self.buckets = tuple(sorted(float(edge) for edge in buckets))
        self._counts: dict[tuple[str, ...], list[float]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        """Record one observation. Non-finite values are ignored, not counted.

        A ``nan`` confidence would poison the sum and every rate computed from it,
        and refusing to record it is safer than publishing a metric no query can
        trust.
        """
        value = float(value)
        if not math.isfinite(value):
            return
        key = self._key(labels)
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = [0.0] * len(self.buckets)
                self._counts[key] = counts
            for index, edge in enumerate(self.buckets):
                if value <= edge:
                    counts[index] += 1.0
            self._sums[key] = self._sums.get(key, 0.0) + value

    def count(self, **labels: str) -> int:
        counts = self._counts.get(self._key(labels), ())
        return int(sum(counts[-1:]) if counts else 0)

    def total(self, **labels: str) -> float:
        return self._sums.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        lines = self._header()
        with self._lock:
            for key, counts in sorted(self._counts.items()):
                for edge, count in zip(self.buckets, counts, strict=True):
                    pairs = self._pairs(key, le=_format_value(edge))
                    lines.append(f"{self.name}_bucket{pairs} {_format_value(count)}")
                total = counts[-1] if counts else 0.0
                lines.append(
                    f"{self.name}_bucket{self._pairs(key, le='+Inf')} {_format_value(total)}"
                )
                lines.append(
                    f"{self.name}_sum{self._pairs(key)} {_format_value(self._sums.get(key, 0.0))}"
                )
                lines.append(f"{self.name}_count{self._pairs(key)} {_format_value(total)}")
        return lines

    def _pairs(self, labels: tuple[str, ...], **extra: str) -> str:
        """Render ``{...}`` including any label the caller adds (e.g. ``le``).

        ``le`` is appended last, which matches the convention every Prometheus
        client library follows for histogram buckets.
        """
        items = [f'{name}="{label}"' for name, label in zip(self.labelnames, labels, strict=True)]
        items.extend(f'{name}="{value}"' for name, value in extra.items())
        return "{" + ",".join(items) + "}" if items else ""


#: Every family declared in this module, in scrape order. Appended by ``_declare``.
REGISTRY: list[_Family] = []


def _declare[T: _Family](family: T) -> T:
    REGISTRY.append(family)
    return family


# --- the families this build actually writes ----------------------------------
#
# Only families that some code path increments are declared. A declared-but-never
# written counter is a lie that shows up as a permanent zero on a dashboard, which
# is worse than an absent series: the spec's alert rules cannot distinguish "no
# failures" from "not instrumented".

EXTRACTIONS_TOTAL: Final = _declare(
    Counter(
        "extractions_total",
        "Contract extraction runs, by outcome.",
        ("status",),
    )
)

EXTRACTION_CONFIDENCE: Final = _declare(
    Histogram(
        "extraction_confidence",
        "Per-field extraction confidence, as stored.",
    )
)

NOTIFICATIONS_SENT_TOTAL: Final = _declare(
    Counter(
        "notifications_sent_total",
        "Notification dispatch attempts, by channel and outcome.",
        ("channel", "status"),
    )
)

ESCALATIONS_TOTAL: Final = _declare(
    Counter(
        "escalations_total",
        "Escalations the response window produced, by outcome.",
        ("status",),
    )
)

#: One tick of the reminder planner, counted per contract it considered.
#:
#: ``outcome`` is one of ``planned`` (a notification row was written),
#: ``already_planned`` (the slot for this milestone already existed, so the tick was
#: a no-op — the healthy steady state, and the number that should dominate) or
#: ``refused`` (policy declined; the reason is in the log line and never in the
#: label, because refusal codes are bounded but ``details`` are not).
#:
#: This is the series an operator needs to answer the only question the planner
#: raises: "is the daily scan actually planning anything?" A planner that is running
#: and finding nothing looks identical to a planner that is not running, unless
#: something counts the ticks.
REMINDER_PLANS_TOTAL: Final = _declare(
    Counter(
        "reminder_plans_total",
        "Contracts the reminder planner considered, by outcome.",
        ("outcome",),
    )
)

# Named ``..._total`` rather than ``escalation_alerts_total{status="failed"}`` so the
# alert rule the spec records -- ``increase(escalation_email_failed_total[1h]) > 0``
# -- works verbatim, with no query to rewrite on the day someone writes the rule.
# There is deliberately no matching "sent" counter: nothing would read it, and an
# unwatched series is what the note above warns about.
ESCALATION_EMAIL_FAILED_TOTAL: Final = _declare(
    Counter(
        "escalation_email_failed_total",
        "Escalation alerts that could not be delivered to the operator.",
    )
)

# Split by outcome rather than only counting opt-outs, because the *interesting*
# number is not how many customers said stop -- it is ``unmatched``. A reply we cannot
# tie to a contract means somebody asked us to stop and we could not find them to
# honour it, and a series stuck above zero is the only signal that will exist for it.
SMS_INBOUND_TOTAL: Final = _declare(
    Counter(
        "sms_inbound_total",
        "Inbound SMS webhook calls, by outcome.",
        ("outcome",),
    )
)

#: One tick of the expiry sweep, counted per contract it leased.
#:
#: ``outcome`` is ``expired`` (the term had ended on the customer's own calendar, so
#: the status moved and an audit row was written), ``still_current`` (leased because
#: the end date is at or before today *in UTC*, but not yet past on the customer's
#: clock — the one-day over-selection the query accepts) or ``unknown_timezone`` (the
#: zone could not be resolved, so the day the term ends cannot be computed and the
#: row was left alone).
#:
#: Split this way because the three look identical from outside and mean opposite
#: things: ``expired`` dominating is a backlog being cleared and then going quiet,
#: ``still_current`` dominating is normal, and a standing ``unknown_timezone`` is a
#: set of contracts that will never close — the only one of the three that needs
#: someone to do something.
CONTRACTS_EXPIRED_TOTAL: Final = _declare(
    Counter(
        "contracts_expired_total",
        "Contracts the expiry sweep leased, by outcome.",
        ("outcome",),
    )
)


def render() -> str:
    """Return the whole registry in Prometheus text exposition format."""
    lines: list[str] = []
    for family in REGISTRY:
        lines.extend(family.render())
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Clear every sample. Tests only — production metrics are cumulative."""
    for family in REGISTRY:
        if isinstance(family, Histogram):
            family._counts.clear()
            family._sums.clear()
        else:
            family._values.clear()  # type: ignore[attr-defined]


def families() -> Iterator[_Family]:
    """Iterate the declared families (used by tests and /metrics introspection)."""
    return iter(REGISTRY)
