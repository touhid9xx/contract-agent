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
        self._seed_its_one_series()

    def _seed_its_one_series(self) -> None:
        """Report an unlabelled counter at zero from the moment it is declared.

        A counter with no labels has exactly one series, so ``0`` here is a fact
        about counting rather than a claim about the world: nothing has been
        counted yet. Rendering no sample instead makes the *first* failure
        invisible to the only query anyone writes over a failure counter.
        Prometheus then sees the series go absent → 1, and ``increase()`` over an
        absent series returns no data rather than 1, so ``increase(x[1h]) > 0``
        stays false through exactly the event it exists to catch.

        That was not a hypothetical. Measured in D10-109: an escalation was
        raised, its alert was refused, and ``EscalationEmailFailed`` never even
        reached ``pending`` — the deployment reported health while an operator was
        never told.

        Only an *unlabelled* counter gets this. A labelled family's series names
        its outcomes, so there is no single series to invent, and inventing every
        outcome would publish zeros for paths no call site can produce. A
        histogram is a distribution, not a count: "no observations" is not a
        measurement of zero, and a zero count divides into nothing.
        """
        if not self.labelnames:
            self._values[()] = 0.0

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
# Only families that some code path increments are declared. Declaring a family
# with no writer is a lie that shows up as a permanent zero on a dashboard: "no
# failures" and "not instrumented" look identical, and no query separates them.
#
# That note used to read "a declared-but-never written counter is a lie ... which is
# worse than an absent series", which was too broad. Amended, not deleted, in
# D10-109: it rules out a family nothing can *ever* write, not the zero itself. A
# counter that can rise reports zero until it does -- that is the baseline
# ``increase()`` needs, and suppressing the zero until the first increment is what
# left D10-91's rule unable to fire at all.

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
#
# The name was necessary and not sufficient. Until D10-109 the family carried no
# sample until its first failure, so the rule compared "absent" against 1 and stayed
# quiet; carrying no labels is what made the zero fixable, since there is only one
# series to report (see ``Counter._seed_its_one_series``).
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

#: Call-outcome callbacks from Asterisk, by outcome (``D10-154``).
#:
#: ``human`` and ``machine`` are the two verdicts that move a notification's status,
#: and they are the point of the family: a deployment whose calls are all ``machine``
#: is dialling voicemail boxes, which is exactly the fact ``VOICE_TRANSPORT=ari`` alone
#: could never report. ``unknown`` is AMD's own ``NOTSURE`` — counted and *not* written,
#: because inventing one of the two verdicts from a refusal to give one would be a
#: record of something that did not happen.
#:
#: ``unmatched`` is the series an operator watches: a callback naming a channel id no
#: row holds means a call was placed that this database has no record of, or the
#: callback is arriving before the dispatch committed it. Either way the answer is lost
#: and only the count says so — the route answers the same 202 regardless.
VOICE_OUTCOME_TOTAL: Final = _declare(
    Counter(
        "voice_outcome_total",
        "Call-outcome callbacks received from the dialler, by outcome.",
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


#: ``outcome`` tokens for :data:`OUTBOX_PARKED_EVENTS`, named once here because the
#: writer and every test that asserts a park was counted read the same spelling, and
#: a typo in one of them would look exactly like "no parks".
OUTBOX_PARK_RELAY_FAILED: Final = "relay_failed"
OUTBOX_PARK_TRANSPORT_DISABLED: Final = "transport_disabled"

#: The dead-letter backlog, as one reading of the table rather than a running count.
#:
#: A gauge, not a counter: replay un-parks rows, so the number has to be able to fall,
#: and a counter of "parks ever" would stay permanently above zero after the first one
#: was cleared — an alarm nobody can silence is the same thing as one nobody reads.
#: It reads :func:`app.db.repositories.outbox.dead_letter_count`, the identical query
#: `doctor` and the attention verdict already use, so the metric cannot drift from the
#: number an operator is told on the command line.
#:
#: The ``outcome`` label is `D10-39`'s distinction, given a series. With
#: ``EVENT_TRANSPORT=none`` — the default — every event the relay claims parks, so a
#: bare ``outbox_parked_events > 0`` would fire on the first ``docker compose up`` and
#: be muted by Thursday, which is how the module docstring of
#: :mod:`app.workers.attention` says a real alarm stops being read. ``transport_disabled``
#: is that explained population; ``relay_failed`` is a deployment that had a transport
#: and could not deliver, which nothing clears by itself. Only the second one is
#: alerted on (``ops/prometheus/rules.yml``), and both are published so the first stays
#: visible instead of merely excluded.
OUTBOX_PARKED_EVENTS: Final = _declare(
    Gauge(
        "outbox_parked_events",
        "Outbox events parked in the dead-letter state, by why delivery stopped.",
        ("outcome",),
    )
)


#: The outbox's *other* backlog: events nothing has claimed for longer than a lease,
#: which is what a relay that is not running looks like from the outside.
#:
#: `D10-90` listed ``outbox_pending_count`` among the families that were absent rather
#: than hollow, because no code path read the table and a family declared to read zero
#: is the placeholder the spec forbids in production paths. Something reads it now, and
#: this is the reading ``docker-compose.yml`` used to apologise for on the relay's port.
#:
#: Unlabelled, and that is a decision rather than an omission. The park gauge needed an
#: ``outcome`` split because ``transport_disabled`` is *every* event on the shipped
#: default, so a bare ``> 0`` would have fired on the first ``docker compose up``. Here
#: there is no second population: a row serving out a retry backoff is excluded by the
#: query rather than by a label, so a non-zero reading has exactly one explanation — the
#: relay stopped claiming — and no configured-and-expected version of that exists.
#:
#: No alert rule ships on it yet, following ``extraction_backlog``: the number is a day
#: old and nobody has stated a threshold, and a rule invented to look finished is the
#: "alert nobody specified" that ``ops/prometheus/rules.yml`` opens by refusing. It is
#: published on every tick including the zero, so adding the rule later is a rule and
#: nothing else (`D10-134`).
OUTBOX_PENDING_COUNT: Final = _declare(
    Gauge(
        "outbox_pending_count",
        "Outbox events nothing has claimed for longer than a lease.",
    )
)


#: ``status`` tokens for :data:`NOTIFICATIONS_UNDELIVERED`, spelled once for the same
#: reason the park tokens are: the writer, the rule and every test that asserts a row
#: was counted read one spelling, and a typo would look like "nothing is stuck".
#:
#: The names are the stored :class:`~app.db.enums.NotificationStatus` values, not
#: invented labels, because an operator reading the gauge next to a row in the database
#: should not have to translate between two vocabularies for the same state.
NOTIFICATION_UNDELIVERED_FAILED: Final = "failed"
NOTIFICATION_UNDELIVERED_SUPPRESSED: Final = "suppressed"

#: The undelivered-message backlog, by the state that stopped it (gap 22).
#:
#: A gauge for the same reason as the park backlog: a retry lifts a row out of both
#: states, so the number has to be able to fall, and a counter of "failures ever" would
#: stay above zero for ever after the first one was answered.
#:
#: The two labels are **not** two severities of the same thing, and the alert reads the
#: difference. ``failed`` means a transport was configured, was reached, and refused —
#: ``dispatch_notification`` only parks a row there once ``attempts >= retry_max``, and
#: the attempt counter is not incremented on the ``SENDER_UNAVAILABLE`` path, so a
#: deployment with ``NOTIFICATION_TRANSPORT=none`` (the shipped default) cannot produce
#: one however long it runs. ``suppressed`` is a policy verdict: the contract closed, the
#: customer withdrew consent, a reviewer flagged the field. That is usually the system
#: doing its job, and paging on it would page on correct behaviour — so both are
#: published and only ``failed`` is alerted on (``ops/prometheus/rules.yml``).
#:
#: Published every tick including the zeros, for the ``D10-109`` reason: a family written
#: only when something is wrong looks identical, from Prometheus' side, to a process that
#: is not running.
#:
#: It reads :func:`app.db.repositories.notifications.undelivered_counts`, the identical
#: query `doctor`, ``/attention`` and the attention verdict already use, so the metric
#: cannot drift from the number an operator is told on the command line.
NOTIFICATIONS_UNDELIVERED: Final = _declare(
    Gauge(
        "notifications_undelivered",
        "Notifications that were never delivered, by the state that stopped them.",
        ("status",),
    )
)


#: ``state`` tokens for :data:`NOTIFICATIONS_PENDING_OVERDUE`, spelled once for the
#: reason every other token set here is: the writer, the rule and every test that
#: asserts a row was counted read one spelling, and a typo would look like "nothing
#: is stuck".
#:
#: :data:`NOTIFICATION_UNDELIVERED_FAILED` and ``..._SUPPRESSED`` are deliberately
#: **not** reused as a third and fourth member of that family. Those two labels mean
#: *a fault* and *a policy verdict* — states a row stopped in — where these three mean
#: *why a row has not moved at all*, and the rule matcher on the old family
#: (``status="failed"``) would silently start covering a population it was not written
#: for. A separate family keeps each matcher meaning one thing.
NOTIFICATION_PENDING_UNCONFIGURED: Final = "unconfigured"
NOTIFICATION_PENDING_UNSUPPORTED: Final = "unsupported"
NOTIFICATION_PENDING_STUCK: Final = "stuck"

#: The queue that had no reading at all, by why a row is still waiting (``D10-129``).
#:
#: :data:`NOTIFICATIONS_UNDELIVERED` counts rows that *stopped*. A ``pending`` row was
#: accounted for by the scheduler's existence, so it needed no alarm — and with
#: ``NOTIFICATION_TRANSPORT=none``, the shipped default, that reasoning fails:
#: :func:`~app.workers.dispatch.dispatch_notification` raises ``SENDER_UNAVAILABLE``
#: **before** ``attempts`` is incremented, so a reminder can never reach the retry cap,
#: never turns ``failed``, and stays ``pending`` for ever. The default install therefore
#: published ``failed 0`` and a clean board while a queue of customer reminders sat
#: behind it, unsendable and uncounted.
#:
#: Only *overdue* rows are counted — due, still ``pending``, and due for longer than
#: :attr:`~app.core.config.Settings.notification_overdue_grace` — which is what keeps
#: this from firing on a deployment doing its job: a row deferred for quiet hours has a
#: ``scheduled_at`` in the future, is not due, and appears in no series here.
#:
#: The three states are separated by one question, *is there a setting whose change
#: would make this channel deliverable?*:
#:
#: * ``unconfigured`` — yes, and it is off. Counted and published but **not** alerted
#:   on, for the reason ``transport_disabled`` is not: on the default install it is
#:   non-zero from the first reminder onward, so an alarm on it would be muted by
#:   Thursday, which is how a real one stops being read.
#: * ``unsupported`` — no, because the channel sits outside
#:   :data:`~app.core.config.NOTIFIABLE_CHANNELS`: a ceiling rather than a switch, so
#:   no environment variable clears these and the human action is to change what may be
#:   consented or supply an address a channel can use. Empty on every configuration
#:   this build ships since ``D10-144`` moved ``voice`` inside the ceiling; the bucket
#:   is kept because the ceiling is a statement about the ``Channel`` enum, and a
#:   channel added to it before it can be sent would land here.
#: * ``stuck`` — the channel *is* deliverable and the send path is not running. A fault
#:   on every configuration, and the only one of the three that says "the scheduler is
#:   down" rather than "the scheduler has nothing it can send".
#:
#: Published every tick including the zeros, for the ``D10-109`` reason: a family
#: written only when something is wrong looks identical, from Prometheus' side, to a
#: process that is not running.
#:
#: It reads :func:`app.db.repositories.notifications.pending_overdue_counts`, the same
#: query the attention verdict uses, so the metric cannot drift from the number an
#: operator is told on the command line.
NOTIFICATIONS_PENDING_OVERDUE: Final = _declare(
    Gauge(
        "notifications_pending_overdue",
        "Notifications due and still pending past the grace, by why nothing sent them.",
        ("state",),
    )
)


#: ``outcome`` tokens for :data:`CONTRACTS_EXTRACTED_TOTAL`, spelled once for the reason
#: the park and undelivered tokens are: the writer, every test that asserts a contract
#: was counted, and whoever reads the series all use one spelling, and a typo in one of
#: them would look exactly like "nothing happened".
#:
#: The five are one per *thing an operator would do differently*, not one per branch in
#: the worker: ``extracted`` and ``empty`` are the two success paths (values stored, and
#: "read it, it says nothing we store") and both leave the queue; ``reviewed`` is a
#: contract whose stored values are all human-owned, so nothing was overwritten;
#: ``unavailable`` is a backend that was down and will be retried next tick; and
#: ``unreadable`` is bytes this deployment cannot get text out of at all. Only the last
#: keeps a row in the queue for what may be for ever — see
#: ``0008_extraction_attempted_at`` — so a standing ``unreadable`` is the one series
#: that names work for a person.
EXTRACTION_OUTCOME_EXTRACTED: Final = "extracted"
EXTRACTION_OUTCOME_EMPTY: Final = "empty"
EXTRACTION_OUTCOME_REVIEWED: Final = "reviewed"
EXTRACTION_OUTCOME_UNAVAILABLE: Final = "unavailable"
EXTRACTION_OUTCOME_UNREADABLE: Final = "unreadable"

#: One tick of the extraction worker, counted per contract it leased.
#:
#: Separate from ``extractions_total`` — which :func:`app.workers.extraction.extract_contract`
#: already increments with ``status="ok"`` — because the two answer different questions
#: and one of them cannot answer this: ``extractions_total`` counts runs of the *function*,
#: including runs that stored nothing and runs started by the CLI, so a rate over it says
#: how often somebody ran extraction rather than how much work there was. This counts
#: contracts the *worker* leased, by what became of each one.
#:
#: It is a counter and not a gauge because these are events, not a backlog. The backlog
#: is :data:`EXTRACTION_BACKLOG`, published alongside it on the same tick.
CONTRACTS_EXTRACTED_TOTAL: Final = _declare(
    Counter(
        "contracts_extracted_total",
        "Contracts the extraction worker leased, by outcome.",
        ("outcome",),
    )
)

#: How many stored documents are still waiting to be extracted.
#:
#: A gauge because the number falls, and it is the only series here that can answer "is
#: the worker keeping up". ``contracts_extracted_total`` only ever says how many
#: contracts have *left* the queue; an ingest far faster than
#: ``extraction_scan_batch_size`` over ``extraction_scan_poll_interval_seconds`` leaves
#: that counter rising monotonically while the queue behind it grows, which is a
#: deployment that looks healthy and is not.
#:
#: Published every tick including the zero, for the `D10-109` reason: a family written
#: only when there is a backlog is indistinguishable, from Prometheus' side, from a
#: worker that is not running. There is no alert rule on it — a standing backlog is a
#: tuning question, not a page — and none of the five outcome tokens above is in a
#: danger the way ``unreadable`` is, so the gauge is for the dashboard and the counter is
#: for the question.
EXTRACTION_BACKLOG: Final = _declare(
    Gauge(
        "extraction_backlog",
        "Contracts holding a stored document that no worker has extracted yet.",
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
        elif isinstance(family, Counter):
            family._values.clear()
            # Clearing a bare counter would undo the one-series invariant its
            # constructor establishes, so a test that guards that invariant would
            # pass or fail on test ordering rather than on the behaviour.
            family._seed_its_one_series()
        else:
            family._values.clear()  # type: ignore[attr-defined]


def families() -> Iterator[_Family]:
    """Iterate the declared families (used by tests and /metrics introspection)."""
    return iter(REGISTRY)
