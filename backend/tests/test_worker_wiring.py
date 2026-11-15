"""The five places one worker process is named, compared instead of trusted.

A single worker is declared five times in this tree, and every one of those copies is
load-bearing:

* the module — ``app/workers/<stem>.py`` with a ``main()`` and a ``__main__`` guard, which
  is the only copy that actually serves a registry;
* ``docker-compose.yml`` — the service block whose ``command`` starts that module, which
  is the only copy that makes the process exist;
* ``ops/prometheus/prometheus.yml`` — the scrape target that reads the registry, which is
  the only copy that makes the numbers visible;
* ``backend/tests/test_metrics_server.py``'s ``WORKERS`` table, which parametrizes the
  listener proof;
* ``backend/tests/smoke_worker_metrics.py``'s ``WORKERS`` tuple, which is the journey that
  scrapes the socket.

``D10-120`` added the seventh worker by hand to all five and needed no test to pass:
``promtool`` validates the Prometheus config on its own, ``docker compose config`` accepts
the stack on its own, and the two tables only assert about the modules they already list.
So the failure this file exists for is a worker that is started and never scraped — it
runs, it serves, and its families are invisible — or one that is scraped and never
started, which is a target that is permanently ``up == 0``. Both are silent, and both are
one forgotten line.

The two smoke/metrics docstrings already *claim* the lists are the same rows. A claim
repeated in prose is not a check, which is precisely what ``D10-121`` concluded about the
README's job count, so the same treatment applies here: nothing is restated, everything is
derived, and the number seven appears nowhere in this file.

**What this does not cover, stated rather than implied.** It compares *which* workers are
declared where, not what each one does. A worker renamed in its module and its label, in
all five places, passes. It also does not check that the scrape interval, the
``WORKER_METRICS_HOST`` value, or the alert rules mention the right things — an eighth
worker inherits the shared anchor and is scraped, but no rule has to name it.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from types import ModuleType

from tests.smoke_worker_metrics import WORKERS as SMOKE_WORKERS
from tests.test_metrics_server import WORKERS as METRICS_WORKERS
from tests.test_metrics_server import published_labels

ROOT = Path(__file__).resolve().parents[2]
WORKERS_DIR = ROOT / "backend" / "app" / "workers"
COMPOSE = ROOT / "docker-compose.yml"
PROMETHEUS = ROOT / "ops" / "prometheus" / "prometheus.yml"

#: ``services:`` as a section heading, and one service per key indented two spaces
#: beneath it. The same split ``test_readme`` uses on ``ci.yml``'s ``jobs:`` block: a
#: heading is cheaper to read than a YAML parser the test job would then have to install.
SERVICES_HEADING = re.compile(r"(?m)^services:\s*$")
SERVICE_BLOCK = re.compile(r"(?m)^  (?=[a-zA-Z0-9_-]+:\s*$)")
NEXT_TOP_LEVEL = re.compile(r"(?m)^[a-z][a-zA-Z0-9_-]*:\s*$")

#: The one line that makes a service a worker: the module its ``command`` runs.
WORKER_COMMAND = re.compile(r"(?m)^\s+- (app\.workers\.[a-z0-9_]+)\s*$")

#: ``- "127.0.0.1:${RELAY_METRICS_PORT:-9101}:9101"`` — host side, then container side.
METRICS_MAPPING = re.compile(r'127\.0\.0\.1:\$\{[A-Z0-9_]+:-(\d+)\}:(\d+)"')

#: The container-side port every worker listens on, declared once on the shared anchor.
DECLARED_PORT = re.compile(r"(?m)^\s+WORKER_METRICS_PORT:\s*(\d+)\s*$")

#: The ``workers`` scrape job, and the ``- "name:port"`` entries inside it.
WORKERS_JOB = re.compile(r"(?m)^  - job_name: workers\s*$")
NEXT_JOB = re.compile(r"(?m)^  - job_name: ")
TARGET = re.compile(r'- "([a-zA-Z0-9_-]+):(\d+)"')


def _is_main_guard(node: ast.stmt) -> bool:
    """Whether ``node`` is ``if __name__ == "__main__":`` in either operand order."""

    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, *test.comparators]
    named = any(isinstance(side, ast.Name) and side.id == "__name__" for side in sides)
    literal = any(isinstance(side, ast.Constant) and side.value == "__main__" for side in sides)
    return named and literal


def _runnable_workers() -> dict[str, ModuleType]:
    """Every module under ``app.workers`` that is a process, by file stem.

    "Is a process" is decided from the module's own AST — a ``main`` function plus a
    ``__main__`` guard — and not from a list, because a list is the thing under test.
    ``metrics_server``, ``senders`` and the rest are libraries: they are imported by the
    processes above rather than started as one, so they have no guard and drop out here.
    """

    found: dict[str, ModuleType] = {}
    for path in sorted(WORKERS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defines_main = any(
            isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body
        )
        if not defines_main or not any(_is_main_guard(node) for node in tree.body):
            continue
        found[path.stem] = importlib.import_module(f"app.workers.{path.stem}")
    return found


def _served() -> dict[str, str]:
    """Worker stem -> the ``worker=`` label its ``main`` publishes, in the AST.

    ``published_labels`` is ``test_metrics_server``'s reader, reused rather than copied:
    one of the five lists is *that* list, and two extractors would be a sixth thing that
    can disagree with the other five.
    """

    served: dict[str, str] = {}
    for stem, module in _runnable_workers().items():
        labels = published_labels(module)
        assert len(labels) == 1, (
            f"app.workers.{stem} has a main() and a __main__ guard but publishes "
            f"{labels!r}: a process must enter publish_metrics(worker=...) exactly once, or "
            "it serves a registry nothing can tell apart from the next worker's"
        )
        served[stem] = labels[0]
    return served


def _compose_sections() -> dict[str, str]:
    """Service name -> the text of its block in ``docker-compose.yml``."""

    text = COMPOSE.read_text(encoding="utf-8")
    heading = SERVICES_HEADING.search(text)
    assert heading is not None, f"{COMPOSE.name} has no `services:` block to read"

    body = text[heading.end() :]
    end = NEXT_TOP_LEVEL.search(body)
    if end is not None:
        body = body[: end.start()]

    sections: dict[str, str] = {}
    for block in SERVICE_BLOCK.split(body)[1:]:
        sections[block.split(":", 1)[0].strip()] = block
    assert sections, f"parsed no services out of {COMPOSE.name}: that is the parser, not the stack"
    return sections


def _started() -> dict[str, tuple[str, int, int]]:
    """Worker service -> (module stem, published host port, container port)."""

    started: dict[str, tuple[str, int, int]] = {}
    for service, block in _compose_sections().items():
        command = WORKER_COMMAND.search(block)
        if command is None:
            continue
        mapping = METRICS_MAPPING.search(block)
        assert mapping is not None, (
            f"docker-compose.yml starts {command.group(1)} for `{service}` but publishes no "
            "metrics port for it: the worker would serve a registry on the Compose network "
            "that nothing can reach, which reads as a worker with no metrics at all"
        )
        stem = command.group(1).rsplit(".", 1)[-1]
        started[service] = (stem, int(mapping.group(1)), int(mapping.group(2)))
    return started


def _scraped() -> dict[str, int]:
    """Service name -> port, from the ``workers`` job in ``prometheus.yml``."""

    text = PROMETHEUS.read_text(encoding="utf-8")
    heading = WORKERS_JOB.search(text)
    assert heading is not None, (
        f"{PROMETHEUS.name} has no `job_name: workers` scrape job: the per-worker listeners "
        "exist and nothing reads them"
    )

    body = text[heading.end() :]
    end = NEXT_JOB.search(body)
    if end is not None:
        body = body[: end.start()]

    scraped = {host: int(port) for host, port in TARGET.findall(body)}
    assert scraped, f"the `workers` job in {PROMETHEUS.name} names no targets at all"
    return scraped


def _declared_port() -> int:
    """The container-side port the shared ``x-worker`` anchor tells every worker to bind."""

    ports = DECLARED_PORT.findall(COMPOSE.read_text(encoding="utf-8"))
    assert len(ports) == 1, (
        f"docker-compose.yml declares WORKER_METRICS_PORT {len(ports)} times ({ports!r}); the "
        "anchor is meant to declare it once so no two workers can disagree"
    )
    return int(ports[0])


def test_the_stack_starts_exactly_the_workers_that_serve_a_registry() -> None:
    """A new module with no service is code nothing runs; a service with no module, a typo."""

    served = set(_served())
    started = {stem for stem, _, _ in _started().values()}

    unstarted = sorted(served - started)
    unserved = sorted(started - served)
    assert not (unstarted + unserved), (
        f"docker-compose.yml starts {sorted(started)} while app/workers declares {sorted(served)}. "
        f"Not started: {unstarted} — a worker the stack never runs. "
        f"Not serving: {unserved} — a service whose module publishes no registry. "
        "Both halves belong in the same commit"
    )


def test_every_worker_in_the_stack_is_scraped() -> None:
    """The failure with no symptom: a worker runs, serves, and no target ever asks."""

    started = set(_started())
    scraped = set(_scraped())

    unscraped = sorted(started - scraped)
    unknown = sorted(scraped - started)
    assert not (unscraped + unknown), (
        f"the `workers` job scrapes {sorted(scraped)} and the stack runs {sorted(started)}. "
        f"Unscraped: {unscraped} — those processes are alive and their families are invisible, "
        "which is the one failure an operator cannot see even by looking. "
        f"Not in the stack: {unknown} — a target that is permanently `up == 0`"
    )


def test_every_scrape_asks_the_port_the_anchor_declares() -> None:
    """Two ports to get wrong per worker: what it binds, and what other end dials."""

    declared = _declared_port()
    scraped = _scraped()

    for service, (_, _, container) in _started().items():
        assert container == declared, (
            f"docker-compose.yml maps `{service}` to container port {container}, but the "
            f"x-worker anchor sets WORKER_METRICS_PORT={declared}. The process binds one and "
            "the mapping names the other, so the published port is a closed door"
        )
        assert scraped[service] == container, (
            f"prometheus.yml scrapes `{service}` on {scraped[service]}, but compose maps it to "
            f"container port {container}. The target answers on a port the worker never bound"
        )


def test_every_worker_publishes_a_distinct_host_port() -> None:
    """All seven listen on the same port inside their own namespaces; only the host differs."""

    host_ports: dict[int, str] = {}
    for service, (_, host, _) in _started().items():
        assert host not in host_ports, (
            f"`{service}` and `{host_ports[host]}` both publish host port {host}: on one machine "
            "the second container to start cannot bind, and `docker compose up` fails on a port "
            "collision rather than on anything to do with this change"
        )
        host_ports[host] = service


def test_every_worker_publishes_a_distinct_label() -> None:
    """The label is the only identity in a scrape body: two workers sharing one is a merge."""

    labels: dict[str, str] = {}
    for stem, label in _served().items():
        assert label not in labels, (
            f"app.workers.{stem} and app.workers.{labels[label]} both publish worker={label!r}. "
            "A scrape body cannot say which process answered if two of them answer the same"
        )
        labels[label] = stem


def test_the_metrics_listener_table_names_every_worker() -> None:
    """A worker missing from this table is a listener no test ever asks about."""

    expected = {f"app.workers.{stem}": label for stem, label in _served().items()}

    missing = sorted(set(expected) - set(METRICS_WORKERS))
    extra = sorted(set(METRICS_WORKERS) - set(expected))
    assert not (missing + extra), (
        f"test_metrics_server.WORKERS lists {sorted(METRICS_WORKERS)} while app/workers serves "
        f"{sorted(expected)}. Missing: {missing} — that worker's listener is unproven. "
        f"Extra: {extra} — a parametrized case that imports nothing. Add the row in the same "
        "commit as the worker"
    )
    assert expected == METRICS_WORKERS, (
        f"test_metrics_server.WORKERS maps {METRICS_WORKERS}, but each module publishes "
        f"{expected}. The label is read out of the module's own AST, so this is a table that "
        "disagrees with the code it names"
    )


def test_the_smoke_journey_names_every_worker() -> None:
    """A worker missing here is never dialled over a real socket; the rest of the sweep lies."""

    expected = {f"app.workers.{stem}": label for stem, label in _served().items()}

    modules = {worker.module for worker in SMOKE_WORKERS}
    names = {worker.name for worker in SMOKE_WORKERS}
    missing = sorted(set(expected) - modules)
    extra = sorted(modules - set(expected))
    assert not (missing + extra), (
        f"smoke_worker_metrics.WORKERS starts {sorted(modules)} while app/workers serves "
        f"{sorted(expected)}. Missing: {missing} — that listener is never scraped over a real "
        f"socket. Extra: {extra} — a subprocess this journey starts for no module"
    )
    assert names == set(expected.values()), (
        f"the smoke journey names its processes {sorted(names)}, but they publish "
        f"{sorted(expected.values())}: the name a row is started by must be the label its "
        "body reports, or a failover between two workers looks like one worker restarting"
    )
