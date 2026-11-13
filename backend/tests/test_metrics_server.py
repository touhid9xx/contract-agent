"""The per-worker ``/metrics`` listener (``D10-107``).

``test_metrics`` pins the registry and the *API's* scrape route. This file pins
the other half: that each worker process can be scraped at all. Before the
listener existed every worker wrote counters — ``escalations_total``,
``reminder_plans_total``, ``contracts_expired_total``, half of
``notifications_sent_total`` — into a registry that no target ever exposed, so a
recorded rule such as ``increase(escalation_email_failed_total[1h]) > 0`` could
not fire however often it fired in the code.

What is worth pinning is not "a socket opened". It is the five decisions in
:mod:`app.workers.metrics_server`, each of which a later change could quietly
undo:

* **The body is the same bytes the API serves.** Two processes, one format —
  the entire reason this module does not vendor ``prometheus_client``.
* **A taken port is fatal.** A listener that is configured, expected and absent
  is this slice's own bug in a new shape, and it is invisible: the worker looks
  healthy and its series simply never arrive.
* **A second bind cannot succeed.** ``SO_REUSEADDR`` means the opposite of its
  name on Windows, so the guarantee is asserted here rather than inherited —
  two workers sharing a port would silently starve one of them.
* **Off means nothing bound.** ``WORKER_METRICS_PORT=0`` is a deployment that
  scrapes nothing; it must not leave a port open, and it must not be reachable
  by accident through the ``start`` path.
* **Every worker is wired, under its own name.** Six ``main()`` functions is six
  chances to add a seventh worker and forget the listener.
"""

from __future__ import annotations

import ast
import contextlib
import http.client
import importlib
import inspect
import logging
import socket
import textwrap
import threading
from collections.abc import Iterator
from types import ModuleType

import pytest
import structlog
from pydantic import ValidationError
from structlog.testing import capture_logs

from app.core import metrics
from app.core.config import Settings
from app.workers.metrics_server import (
    METRICS_PATH,
    MetricsServer,
    MetricsServerError,
    publish_metrics,
)

#: The workers that must serve their own registry, and the name each must
#: publish under — the label an operator uses to tell which process answered.
WORKERS = {
    "app.workers.outbox_relay": "outbox_relay",
    "app.workers.notification_scheduler": "notification_scheduler",
    "app.workers.escalation_scanner": "escalation_scanner",
    "app.workers.attention_watch": "attention_watch",
    "app.workers.reminder_planner": "reminder_planner",
    "app.workers.expiry": "expiry",
}

#: Long enough for a loopback round trip on a loaded CI box, short enough that a
#: listener which never answers fails the test instead of hanging it.
TIMEOUT_SECONDS = 5.0


# --- fixtures and helpers --------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Every test starts and ends with empty samples, so a body is predictable."""
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(autouse=True)
def _capture_at_debug() -> None:
    """Lower the capture threshold so the access line is observable.

    ``conftest`` pins structlog at ``INFO`` so no test can write to pytest's dying
    capture stream, and the access line is ``debug`` on purpose — it is one line
    per scrape, which is noise at ``info``. Without this the event asserted below
    would be filtered before ``capture_logs`` saw it, and routing the line to the
    logger would be untestable. Same fixture as ``test_attention_watch``, and no
    teardown for the same reason: ``quiet_logging`` is itself autouse, so the
    pinned configuration comes back after every test.
    """
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


def free_port() -> int:
    """A port the OS has just called free — the ``smoke_journey`` shape.

    Binding and closing rather than hard-coding a number: a fixed port collides
    with whatever else the machine runs and turns a real failure into a flake.
    """
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def serving(port: int | None = None) -> Iterator[MetricsServer]:
    """A listener on ``port`` (or a free one), stopped even if the test fails."""
    server = MetricsServer.start("127.0.0.1", free_port() if port is None else port, worker="test")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def live_server() -> Iterator[MetricsServer]:
    """One listener on a free port, for the tests that only need to scrape one."""
    with serving() as server:
        yield server


def request(
    server: MetricsServer, method: str = "GET", path: str = METRICS_PATH
) -> tuple[int, dict[str, str], bytes]:
    """One raw round trip: status, headers, bytes. No client-side interpretation."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=TIMEOUT_SECONDS)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def assert_port_is_free(port: int) -> None:
    """Bind the port directly, without ``SO_REUSEADDR``, and give it straight back.

    Stricter than rebinding through the module: a listener that was shut down but
    whose socket was left open still fails this.
    """
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", port))


# --- the listener ----------------------------------------------------------


def test_the_body_is_the_registry_rendered_by_the_api(live_server: MetricsServer) -> None:
    """The one property the whole module exists for: one format, two processes."""
    metrics.EXTRACTIONS_TOTAL.inc(status="ok")

    status, headers, body = request(live_server)

    assert status == 200
    assert body == metrics.render().encode("utf-8")
    assert headers["Content-Type"] == metrics.CONTENT_TYPE
    assert b'extractions_total{status="ok"} 1' in body


def test_a_scrape_of_a_worker_that_counted_nothing_declares_the_families(
    live_server: MetricsServer,
) -> None:
    """An empty scrape is a valid scrape, and an absent series is not a zero.

    The relay never increments ``escalations_total`` — that series lives in the
    scanner, and this module deliberately does not aggregate across processes —
    so its scrape carries the family's ``HELP``/``TYPE`` lines and no sample. The
    distinction is real: ``increase()`` over an absent series returns no data,
    while over a recorded ``0`` it returns ``0``.
    """
    status, _, body = request(live_server)

    assert status == 200
    assert b"# TYPE escalations_total counter" in body
    assert b"escalations_total{" not in body


def test_a_query_string_does_not_change_the_route(live_server: MetricsServer) -> None:
    """Prometheus sends none, but a proxy or a probe in front of it might."""
    status, _, body = request(live_server, path=f"{METRICS_PATH}?debug=1")

    assert status == 200
    assert body == metrics.render().encode("utf-8")


def test_another_path_is_a_bare_404(live_server: MetricsServer) -> None:
    """One path, one verb: this listener is not a second API."""
    status, headers, body = request(live_server, path="/health")

    assert status == 404
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert body == b"not found\n"


def test_a_post_is_refused_by_the_base_class(live_server: MetricsServer) -> None:
    """Nothing here writes, so any method other than GET stays unimplemented."""
    status, _, _ = request(live_server, method="POST")

    assert status == 501


def test_the_response_does_not_advertise_the_interpreter_version(
    live_server: MetricsServer,
) -> None:
    """Reachability is not an invitation to publish the patch level."""
    _, headers, _ = request(live_server)

    assert headers["Server"] == "contract-agent-metrics"


def test_the_access_line_goes_to_the_logger_and_not_to_stderr(
    live_server: MetricsServer, capsys: pytest.CaptureFixture[str]
) -> None:
    """The base class prints the raw request line past every redaction rule.

    ``log_message`` is routed to structlog for exactly that reason, so the
    assertion has three halves: the event exists, it carries the detail, and the
    raw line never reaches the stream the base class would have written to.
    """
    capsys.readouterr()  # drain whatever the imports wrote

    with capture_logs() as captured:
        status, _, _ = request(live_server)

    written = capsys.readouterr()

    assert status == 200
    assert [entry["event"] for entry in captured] == ["worker_metrics_request"]
    assert "GET /metrics" in captured[0]["detail"]
    assert "GET /metrics" not in written.err
    assert "GET /metrics" not in written.out


# --- the settings that decide whether it runs ------------------------------


def test_port_zero_switches_the_listener_off_and_opens_nothing() -> None:
    """A deployment that scrapes nothing should not carry a listener anyway."""
    settings = Settings(app_env="test", worker_metrics_port=0)

    with publish_metrics(settings, worker="switched-off") as server:
        assert server is None

    assert not any(thread.name == "metrics-switched-off" for thread in threading.enumerate())


def test_the_listener_serves_inside_the_block_and_the_port_is_free_afterwards() -> None:
    """``publish_metrics`` owns the lifetime: bound on entry, released on exit."""
    port = free_port()
    settings = Settings(app_env="test", worker_metrics_port=port)

    with publish_metrics(settings, worker="block") as server:
        assert server is not None
        assert server.port == port
        status, _, body = request(server)
        assert status == 200
        assert body == metrics.render().encode("utf-8")

    assert_port_is_free(port)


def test_a_blank_bind_host_is_refused_when_settings_are_loaded() -> None:
    """Absence is expressed by the port, so an empty host means a missing value."""
    with pytest.raises(ValidationError, match="WORKER_METRICS_HOST"):
        Settings(app_env="test", worker_metrics_host="   ")


# --- the failures this module exists to make loud --------------------------


def test_a_taken_port_is_fatal_and_names_the_setting() -> None:
    """Silently sharing a port is what ``SO_REUSEADDR`` brings on Windows."""
    port = free_port()

    with serving(port), pytest.raises(MetricsServerError, match="WORKER_METRICS_PORT"):
        MetricsServer.start("127.0.0.1", port, worker="second")


def test_the_refusal_names_the_worker_it_came_from() -> None:
    """An operator reading the log needs to know which of six workers to fix."""
    port = free_port()

    with serving(port), pytest.raises(MetricsServerError, match="escalation_scanner"):
        MetricsServer.start("127.0.0.1", port, worker="escalation_scanner")


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_start_refuses_a_port_it_could_not_bind_meaningfully(port: int) -> None:
    """``0`` means "off" in settings, so it must not mean "any port" here."""
    with pytest.raises(MetricsServerError, match="WORKER_METRICS_PORT"):
        MetricsServer.start("127.0.0.1", port, worker="test")


def test_stopping_twice_is_safe() -> None:
    """``stop`` runs in a ``finally``, so it can be reached after a partial start."""
    server = MetricsServer.start("127.0.0.1", free_port(), worker="test")

    server.stop()
    server.stop()

    assert_port_is_free(server.port)


def test_a_stopped_listener_releases_the_port_for_the_next_worker() -> None:
    """A restart must be able to take the same port back."""
    port = free_port()

    first = MetricsServer.start("127.0.0.1", port, worker="first")
    first.stop()

    second = MetricsServer.start("127.0.0.1", port, worker="second")
    try:
        status, _, _ = request(second)
        assert status == 200
    finally:
        second.stop()


# --- the wiring: six workers, six listeners --------------------------------


def call_name(call: ast.Call) -> str:
    """The plain name of the function a call node invokes, attribute or not."""
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")


def published_labels(module: ModuleType) -> list[str]:
    """The ``worker=`` labels ``main()`` passes to ``publish_metrics``, in order.

    Read from the AST rather than by running ``main``: each of these functions
    blocks on a tick loop and installs signal handlers, so a test that started
    one would be testing the loop, not the wiring. What can go wrong here is a
    worker whose ``main`` never enters ``publish_metrics`` — and that is exactly
    what a missing call node means.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(module.main)))
    labels: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call) or call_name(call) != "publish_metrics":
                continue
            for keyword in call.keywords:
                if keyword.arg == "worker" and isinstance(keyword.value, ast.Constant):
                    labels.append(str(keyword.value.value))
    return labels


@pytest.mark.parametrize(("module_name", "label"), sorted(WORKERS.items()))
def test_every_worker_serves_its_own_registry(module_name: str, label: str) -> None:
    """Six workers, six listeners. A seventh that forgets is invisible otherwise."""
    module = importlib.import_module(module_name)

    assert published_labels(module) == [label]


def test_importing_a_worker_opens_no_listener() -> None:
    """Serving belongs to ``main``: importing a module must not bind a port.

    An import-time bind would fire in every test run and in every CLI that
    happens to import a worker, and it would do so before logging is configured.
    """
    for module_name in WORKERS:
        importlib.import_module(module_name)

    opened = [thread.name for thread in threading.enumerate() if thread.name.startswith("metrics-")]

    assert opened == []
