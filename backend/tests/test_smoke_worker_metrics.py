"""What the worker-metrics journey waits for before it asserts (``D10-117``).

Why this file exists
--------------------
``smoke_worker_metrics.py`` starts real processes and scrapes real sockets, so it is
not collected by pytest and nothing here re-runs it. What is worth pinning is not its
journey but one *decision inside* it, because that decision is invisible in a passing
run and looks like a flake in a failing one.

The listener binds before the worker takes its first tick, and a family's
``HELP``/``TYPE`` declarations are rendered by the registry at import time while its
samples are written by a tick. So a scrape can return a body that declares every
family and carries no sample — a real state of a healthy process, roughly one tick
wide. ``wait_for_listener`` used to treat that body as readiness and return it; the
journey then asserted on ``outbox_parked_events`` samples in it and lost the race
about one run in ten. CI run ``43711886352`` lost it on a commit whose neighbour had
won it minutes earlier, which is exactly how a race reads from the outside: identical
code, different answer.

The fix is the ``require_series`` argument of ``wait_for_listener``. These tests drive
it against a stub listener that serves the pre-tick body first and the ticked body
second, which makes the window deterministic instead of hoping for it.

What it does not prove
----------------------
It does not run a worker, a port from the journey's table, or the alert rule: those
belong to the smoke script and to ``ops/prometheus``. It pins one function's
readiness contract, and a stub is the right instrument for that — the failure being
guarded against is precisely a body that a stub can produce on demand.
"""

from __future__ import annotations

import contextlib
import http.server
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from tests import smoke_worker_metrics as journey

#: Long enough for the two loopback round trips below on a loaded CI box, short
#: enough that a wait which never ends fails the test instead of hanging it.
DEADLINE_SECONDS = 10.0

#: The body of a live listener that has bound but not yet ticked: every declaration
#: the shared registry holds, and no sample for the gauge the journey asserts on.
PRETICK_BODY = (
    "# HELP escalation_email_failed_total Escalation alerts that could not be sent.\n"
    "# TYPE escalation_email_failed_total counter\n"
    "escalation_email_failed_total 0\n"
    "# HELP outbox_parked_events Dead-lettered outbox events, by whether it is explained.\n"
    "# TYPE outbox_parked_events gauge\n"
)

#: The same body one tick later. Both outcomes, because the watcher writes both every
#: tick — the property that makes the alert fireable and the series waitable-for.
TICKED_BODY = PRETICK_BODY + (
    'outbox_parked_events{outcome="relay_failed"} 0\n'
    'outbox_parked_events{outcome="transport_disabled"} 0\n'
)

#: The worker the park assertions are made against. Only its *name* reaches anything
#: under test: the stub answers for it, so no module is imported and nothing is started.
WORKER = journey.Worker(
    "attention_watch", "app.workers.attention_watch", "ATTENTION_POLL_INTERVAL_SECONDS"
)


@contextlib.contextmanager
def serving(bodies: list[str]) -> Iterator[tuple[int, list[str]]]:
    """Serve ``bodies`` in order, one per request, and yield the port plus the log.

    The last body repeats, so a test can serve a state twice or hold one for ever
    without the handler needing to know which case it is in. The request log is
    returned rather than counted here: "how many times did it ask?" is the observation
    that distinguishes waiting from assuming.
    """
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the spelling http.server dispatches on
            seen.append(self.path)
            payload = bodies[min(len(seen), len(bodies)) - 1].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            # The stub is not the subject; a line per scrape would bury the failure.
            return

    port = journey.free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_body_declared_but_not_yet_ticked_is_a_state_a_listener_really_has() -> None:
    """The precondition, pinned so the fix has a reason on the record.

    The weak signal still returns this body, and it should: for the five workers that
    write nothing this journey can provoke, "the right process answered" is the whole
    question, and these declarations are the strongest honest answer to it. This test
    exists so that nobody later reads the missing sample as an impossible case.
    """
    with serving([PRETICK_BODY, TICKED_BODY]) as (port, seen):
        body = journey.wait_for_listener(WORKER, port, DEADLINE_SECONDS)

    assert body == PRETICK_BODY
    assert seen == ["/metrics"], "the weak signal must not poll a body it accepted"


def test_the_park_series_are_waited_for_rather_than_assumed() -> None:
    """The fix: a body without both series is not readiness when series are required.

    Two requests and not one is the whole assertion. With the pre-tick body accepted,
    the journey's later ``set(parks) == set(PARKED_EVENT_SERIES)`` reads an empty set
    and fails on code that is correct — the failure mode ``43711886352`` recorded.
    """
    with serving([PRETICK_BODY, TICKED_BODY]) as (port, seen):
        body = journey.wait_for_listener(
            WORKER, port, DEADLINE_SECONDS, journey.PARKED_EVENT_SERIES
        )

    assert len(seen) == 2
    assert set(journey.samples(body, "outbox_parked_events")) == set(journey.PARKED_EVENT_SERIES)


def test_the_journey_asks_for_the_park_series_and_nothing_else() -> None:
    """The wiring, not just the function: what ``verify_worker`` actually passes.

    The two tests above drive ``wait_for_listener`` with the tuple spelled out, so on
    their own they stay green if ``verify_worker`` stops passing it — which is the one
    line that decides whether CI waits at all. Emptying ``EVERY_TICK_SERIES`` left them
    all passing, which is how this test came to exist. Both halves are asserted: the
    watcher requires the park series, and a worker with nothing to publish requires
    nothing, so the map cannot silently grow into a way to make a scrape hang.
    """
    assert journey.required_series(WORKER) == journey.PARKED_EVENT_SERIES
    assert journey.required_series(journey.WORKERS[0]) == ()


def test_a_watcher_that_never_ticks_fails_by_name_rather_than_by_assertion() -> None:
    """A worker that binds and never publishes is broken, and must not read as ready.

    The message matters as much as the exception: the journey prints it and returns
    an exit code, so "never answered a scrape" plus the series it wanted is what a
    reader gets instead of a downstream assertion about an empty set.
    """
    with (
        serving([PRETICK_BODY]) as (port, _seen),
        pytest.raises(journey.SmokeError) as raised,
    ):
        journey.wait_for_listener(WORKER, port, 1.0, journey.PARKED_EVENT_SERIES)

    message = str(raised.value)
    assert "attention_watch never answered a scrape" in message
    assert 'outbox_parked_events{outcome="relay_failed"}' in message
