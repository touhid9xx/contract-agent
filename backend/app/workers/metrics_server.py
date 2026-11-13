"""A ``/metrics`` listener for each worker process.

``GET /metrics`` on the API serves the registry of *whichever process answers
the request*: :mod:`app.core.metrics` keeps its families in a module-level list
with no background exporter, so every process holds a private copy. That is
deliberate (``D10-90``) and it leaves a hole that nothing filled until now —
the API is the only process that ever served a scrape, so the counters the
workers write could not be read by anything.

That was not a cosmetic gap. Four families exist *only* in a worker process:

* ``escalations_total`` and ``escalation_email_failed_total`` — written by the
  scanner, and the second is named so that the spec's recorded rule
  ``increase(escalation_email_failed_total[1h]) > 0`` needs no rewrite
  (``D10-91``). A rule whose series no target ever exposes cannot fire.
* ``reminder_plans_total`` — named as the answer to the only question the
  planner raises, "is the daily scan actually planning anything?".
* ``contracts_expired_total`` — the expiry sweep's only standing signal, and
  the one that distinguishes a cleared backlog from contracts that will never
  close.

``notifications_sent_total`` was half-visible for the same reason: the API
writes it when a client dispatches, the scheduler writes it for every send the
clock decided, and only the API's copy was ever scraped. ``extractions_total``
and ``extraction_confidence_histogram`` are worse than invisible: their only
writer is ``cli extract``, a process that exits before a scrape could happen.

**Decision.** Each worker serves its own registry, on its own thread, over the
standard library's HTTP server. No new dependency (the reason ``D10-90`` wrote
the exposition format by hand rather than vendoring ``prometheus_client``), and
the body is :func:`app.core.metrics.render` — the same bytes the API serves,
which is the point: one format, two processes.

What this deliberately does **not** do:

* It does not aggregate. A scrape of the relay declares ``escalations_total``
  and emits no sample for it — an *absent* series, not a zero, because the
  exposition format only prints label sets that were recorded. Prometheus treats
  the two differently: ``increase()`` over an absent series returns no data, not
  ``0``. That is what a per-process registry means, and pretending otherwise by
  summing across processes would be an invented number. Point rules at the
  worker that owns the family — the *published* ports differ per worker for
  exactly this reason (the containers all listen on the same number).
* It does not become a liveness signal. A thread that answers ``/metrics`` says
  the process can still accept a connection, not that its tick loop is making
  progress: a worker blocked on a hung database would answer 200 for ever. That
  is why the Compose services keep ``healthcheck: disable`` and the log line per
  tick stays the honest liveness evidence.
* It is not a second API. One path, one verb, no auth (a scrape carries none on
  the API either), and 501 for anything else.

A bind failure is fatal on purpose. A worker that cannot publish and says
nothing recreates this slice's own bug in a new shape — a listener that is
configured, expected, and absent — so startup fails with a message naming the
setting instead.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core import metrics
from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: The one path the worker listener answers, identical to the API's route.
METRICS_PATH = "/metrics"

#: Deliberately terse: an unmatched path is not a route table.
_NOT_FOUND_BODY = b"not found\n"

_PLAIN_TEXT = "text/plain; charset=utf-8"

#: How long :meth:`MetricsServer.stop` waits for the serving thread to unwind.
_JOIN_SECONDS = 5.0


class MetricsServerError(RuntimeError):
    """The metrics listener could not be started on the configured address."""


class _MetricsHandler(BaseHTTPRequestHandler):
    """Answers ``GET /metrics``; every other method gets the base class's 501."""

    #: The base class advertises ``BaseHTTP/0.6 Python/3.x``. Whoever can reach
    #: this port does not need to be told the interpreter version, and the header
    #: carries nothing else an operator uses.
    server_version = "contract-agent-metrics"

    def version_string(self) -> str:
        """``server_version`` alone, with no trailing space.

        The base class composes the header as ``server_version + " " +
        sys_version``, so the obvious way to hide the interpreter — setting
        ``sys_version = ""`` — still leaves a stray space on every response.
        """
        return self.server_version

    def do_GET(self) -> None:  # the spelling http.server dispatches on
        if self.path.split("?", 1)[0] != METRICS_PATH:
            self._send(404, _NOT_FOUND_BODY, _PLAIN_TEXT)
            return
        self._send(200, metrics.render().encode("utf-8"), metrics.CONTENT_TYPE)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # http.server's spelling
        """Route the access line to the structured logger instead of ``stderr``.

        The base class prints the raw request line to ``sys.stderr``. In a
        JSON-logging deployment that is one line per scrape arriving in a shape
        the log pipeline cannot parse, and one that no redaction rule ever sees.
        ``debug`` because a scrape every fifteen seconds is not news.
        """
        log.debug("worker_metrics_request", detail=format % args)


class _MetricsHTTPServer(ThreadingHTTPServer):
    """``SO_REUSEADDR`` on POSIX, and never on Windows.

    :class:`http.server.HTTPServer` sets ``allow_reuse_address``, which
    ``socketserver`` turns into ``SO_REUSEADDR``. On POSIX that is the flag that
    lets a restarted worker rebind a port whose connections are still in
    ``TIME_WAIT``, so it is wanted. On Windows the same flag means the opposite of
    what its name suggests: a second process may bind a port another socket is
    still *listening* on, and the stack decides which of them receives a
    connection. Two workers given the same ``WORKER_METRICS_PORT`` would both
    start and one would silently starve the other — precisely the failure this
    module exists to refuse — so the flag is cleared there and a taken port raises
    ``WSAEADDRINUSE`` instead. Measured, not assumed: the inherited flag binds
    twice, clearing it raises. (``SO_EXCLUSIVEADDRUSE`` is the documented Windows
    answer and cannot be combined with ``SO_REUSEADDR``; the plain bind already
    refuses.)
    """

    allow_reuse_address = sys.platform != "win32"


class MetricsServer:
    """A bound listener serving :func:`app.core.metrics.render` on its own thread.

    Obtained from :meth:`start` or, in a worker, from :func:`publish_metrics`.
    """

    def __init__(self, httpd: _MetricsHTTPServer, thread: threading.Thread) -> None:
        self._httpd = httpd
        self._thread = thread

    @property
    def port(self) -> int:
        """The port actually bound — useful when the caller asked for one."""
        return int(self._httpd.server_address[1])

    @classmethod
    def start(cls, host: str, port: int, *, worker: str) -> MetricsServer:
        """Bind ``host:port`` and start serving. Binds synchronously, so a taken
        port raises here rather than inside the thread that no one watches."""
        if not 1 <= port <= 65535:
            raise MetricsServerError(
                f"{worker}: WORKER_METRICS_PORT={port} is not a bindable port; "
                "use 1-65535, or 0 to switch the worker metrics listener off."
            )
        try:
            httpd = _MetricsHTTPServer((host, port), _MetricsHandler)
        except OSError as exc:
            raise MetricsServerError(
                f"{worker}: cannot serve metrics on {host}:{port} ({exc}). Another "
                "process usually holds that port on this host; give each worker its "
                "own WORKER_METRICS_PORT, or set it to 0 to switch the listener off."
            ) from exc

        # Daemon threads so a straggling scrape cannot keep the process alive
        # after the main loop has stopped: shutdown must not wait on a client.
        httpd.daemon_threads = True
        thread = threading.Thread(
            target=httpd.serve_forever,
            name=f"metrics-{worker}",
            daemon=True,
        )
        thread.start()
        log.info("worker_metrics_listening", worker=worker, host=host, port=port)
        return cls(httpd, thread)

    def stop(self) -> None:
        """Stop serving and release the port. Safe to call twice."""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=_JOIN_SECONDS)
        log.info("worker_metrics_stopped", port=self.port)


@contextmanager
def publish_metrics(settings: Settings, *, worker: str) -> Iterator[MetricsServer | None]:
    """Serve this process's metrics for the duration of the block.

    ``WORKER_METRICS_PORT=0`` switches the listener off and yields ``None``: a
    deployment that does not scrape should not open a port. Everything else
    binds, and a refused bind propagates so the worker exits instead of running
    without the listener it was configured to have.
    """
    if settings.worker_metrics_port == 0:
        log.info("worker_metrics_disabled", worker=worker)
        yield None
        return

    server = MetricsServer.start(
        settings.worker_metrics_host,
        settings.worker_metrics_port,
        worker=worker,
    )
    try:
        yield server
    finally:
        server.stop()
