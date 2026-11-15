"""Seven real worker processes, seven real sockets, one scrape each.

Why this file exists
--------------------
``D10-107`` gave every worker its own ``/metrics`` listener so the counters that
only a worker process writes could finally be read by something. The tests in
``tests/test_metrics_server.py`` prove the listener's behaviour, but every one of
them serves a registry the *test process* filled: the families arrive because the
test called ``.inc()`` a line earlier, not because a worker ticked and recorded
one. Nothing in this repository had ever read a worker's own series over a
socket, and the ``docker-compose.yml`` port mapping was therefore verified by
parsing the file rather than by a metric arriving anywhere.

This script closes exactly that. It starts ``python -m app.workers.expiry`` as a
*subprocess* against a temporary SQLite database holding one contract whose time
zone cannot be resolved, waits for the listener on the port the environment
variable names, and asserts that the sweep's own counter appears in the body —
``contracts_expired_total{outcome="unknown_timezone"}``, a series the API has
never written and cannot produce. It then asserts the opposite for a family the
sweep does not touch: ``reminder_plans_total`` is *declared* (HELP/TYPE are
there, because the registry is shared) and carries no sample, which is the
absent-series-not-zero distinction the module documents. Finally it stops the
process and proves the port was released.

That is the first phase. A second phase then repeats it for every worker module
— the expiry sweep twice, so that one check applies to all of them rather than to
all but one — each on its own port, and asserts of every one that it answers a
scrape, declares the families the shared registry holds, emits **no sample** for a
family another worker owns, and gives its port back when it dies. Until ``D10-110``
only the expiry sweep had ever been scraped and the port claim rested on reading
``docker-compose.yml``.

What it does not prove
----------------------
It is not a unit test of the listener: path, verb, header and error-body details
belong to ``tests/test_metrics_server.py``. It does not scrape a *container*:
the published ports are exercised here as local *processes*, so nothing below
proves the image, the volume or the network namespace. ~~It runs one worker, not
six.~~ **Amended by ``D10-110``:** it runs every one of them — six when that
paragraph was written, seven since ``D10-120`` — but a process is not a container
and this file is still about the process, the socket and the sample. And it says
nothing about fidelity to Prometheus: parsing the text here is not the same as
Prometheus parsing it, which is why the rule that reads this series is evaluated
in the ledger's measurement rather than here.

How to run it
-------------
From the repository root::

    venv\\Scripts\\python.exe backend/tests/smoke_worker_metrics.py

Exit code 0 means every step passed. Non-zero means a step failed, and the
failing step is printed with the worker's own log tail so the reason is visible
rather than inferred. Nothing is left behind: the database and the two log files
live in a temporary directory that is removed at exit, and the worker is killed
and reaped before the process ends.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# The bootstrap mirrors backend/tests/conftest.py and smoke_journey.py: the
# backend is not an installed distribution (see the pyproject header), and
# APP_ENV must be pinned before app.core.config is imported so production
# validation cannot fire.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_DIR))
os.environ.setdefault("APP_ENV", "test")

from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import Engine, create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.models import Contract, Tenant  # noqa: E402
from app.workers.metrics_server import MetricsServer, MetricsServerError  # noqa: E402

#: The contract the sweep will lease. Its zone is deliberately unresolvable: that
#: is the one branch of ``is_expired`` that increments a counter and changes no
#: row, so the series can be asserted without depending on today's date.
UNKNOWN_ZONE = "not/a_zone"

#: Long past on every clock the sweep could consult.
PAST_END_DATE = date(2020, 1, 1)

#: How long the worker gets to bind, tick and be scraped. Generous because a cold
#: interpreter plus two imports is most of it; the loop exits on the first match.
SCRAPE_DEADLINE_SECONDS = 60.0

#: Interval between scrape attempts. Short: the sweep ticks far more often than
#: this, so the wait is dominated by process start, not by the poller.
RETRY_SECONDS = 0.2

#: A fast tick, so the first scrape attempt does not race a one-hour default.
#: Every worker setting it drives has ``gt=0`` in ``app.core.config``, so a
#: half-second is legal for all seven.
POLL_INTERVAL_SECONDS = "0.5"

#: Key material for the one worker that refuses to start without it (``D10-120``).
#: ``ExtractionWorker.__init__`` builds its cipher and its contact blind index up
#: front, so a missing key fails at start-up rather than on the first contract —
#: which is the right shape for a deployment, and an inconvenient one for a smoke
#: that wants the listener to bind on an empty database. The 32-character floor is
#: ``app.core.config.MIN_BLIND_INDEX_KEY_LENGTH``; the value itself is fixed rather
#: than generated because nothing here reads it back.
SMOKE_BLIND_INDEX_KEY = "smoke-blind-index-key-0123456789abcdef"

#: The only family this script expects to *see* a sample for.
EXPECTED_FAMILY = "contracts_expired_total"

#: The outcome branch, and therefore the exact series, that only an unresolvable
#: zone produces. Named separately from the family on purpose: a first draft
#: accepted any ``contracts_expired_total`` sample, and a mutation that made the
#: seeded zone resolvable passed the journey with ``outcome="expired"`` — the
#: assertion proved the sweep ticks, not that this contract reached that branch.
EXPECTED_OUTCOME = "unknown_timezone"
EXPECTED_SAMPLE = f'{EXPECTED_FAMILY}{{outcome="{EXPECTED_OUTCOME}"}}'

#: Families the sweep never writes, asserted present-but-empty.
UNWRITTEN_FAMILIES = ("reminder_plans_total", "extractions_total")

#: The one family ``D10-109`` seeded. It carries no labels, so its declaration
#: *is* its series and ``Counter.render`` prints ``0`` for it in every process
#: that imports ``app.core.metrics``. Named apart from :data:`EXPECTED_FAMILY`,
#: which is the sweep's labelled family and is absent until the sweep writes it —
#: conflating the two is the mistake this constant exists to prevent.
SEEDED_ZERO_FAMILY = "escalation_email_failed_total"

#: Every series ``outbox_parked_events`` must carry once the watcher has ticked
#: (``D10-117``). Both, not "some": the explained population is what a writer that
#: published only findings would drop, and the unexplained one is what the alert
#: reads — a body missing either is a half-published gauge, and the half it is
#: missing is invisible from Prometheus' side.
PARKED_EVENT_SERIES: tuple[str, ...] = (
    'outbox_parked_events{outcome="relay_failed"}',
    'outbox_parked_events{outcome="transport_disabled"}',
)

#: Every series ``notifications_undelivered`` must carry once the watcher has ticked
#: (``D10-118``). Both, and for the sharper of the two reasons above: the alert reads
#: ``status="failed"`` while ``status="suppressed"`` is the number an operator is
#: meant to *find* when they go looking — so a body carrying only the paged one is a
#: half-published gauge whose missing half is invisible from Prometheus' side.
UNDELIVERED_STATUS_SERIES: tuple[str, ...] = (
    'notifications_undelivered{status="failed"}',
    'notifications_undelivered{status="suppressed"}',
)


@dataclass(frozen=True)
class Worker:
    """One worker process and the dial that makes it tick.

    The rows are the same ones ``tests/test_metrics_server.py`` maps to
    ``publish_metrics`` call sites, and the same ones ``docker-compose.yml``
    publishes a host port for. The module path and the worker name are kept
    together on purpose: a scrape body cannot tell you which process answered, so
    the only identity a journey has is the port it chose to ask.

    It carries no list of the families the worker writes: :data:`SINGLE_WRITER_FAMILIES`
    is the single statement of that, and a second copy here would be a second
    thing to drift.
    """

    name: str
    module: str
    poll_env: str


#: Every worker module that serves its own registry. The expiry sweep is first
#: because it is the one this file asserts a *sample* for; the rest are in no
#: particular order. (This block used to claim the order was Compose's, which it
#: is not — Compose runs relay first and the sweep fourth. Nothing compares the
#: two orders, so nothing here is worth keeping in step with the stack.)
#:
#: Membership is likewise not this file's to assert: ``test_worker_wiring``
#: derives the set from the worker modules and fails on a worker missing here, a
#: row starting a module that serves no registry, or a ``name`` that disagrees
#: with the label the module publishes.
WORKERS: tuple[Worker, ...] = (
    Worker("expiry", "app.workers.expiry", "EXPIRY_SWEEP_POLL_INTERVAL_SECONDS"),
    Worker(
        "reminder_planner", "app.workers.reminder_planner", "REMINDER_SCAN_POLL_INTERVAL_SECONDS"
    ),
    Worker(
        "notification_scheduler",
        "app.workers.notification_scheduler",
        "NOTIFICATION_POLL_INTERVAL_SECONDS",
    ),
    Worker(
        "escalation_scanner",
        "app.workers.escalation_scanner",
        "ESCALATION_SCAN_POLL_INTERVAL_SECONDS",
    ),
    # The relay writes no family of its own: its registry holds the API's zeros,
    # and its listener answers "did this process start?" rather than "is it
    # working?". It is here because a port nothing ever asks is a port nobody
    # would notice going missing. ``attention_watch`` shared that description until
    # ``D10-117`` gave it ``outbox_parked_events``; it stays in the sweep because a
    # listener that binds before the first tick is exactly where "the port is
    # missing" and "the worker is still starting" have to be told apart by asking.
    Worker("outbox_relay", "app.workers.outbox_relay", "OUTBOX_POLL_INTERVAL_SECONDS"),
    Worker("attention_watch", "app.workers.attention_watch", "ATTENTION_POLL_INTERVAL_SECONDS"),
    # ``D10-120``. Last, which is where ``docker-compose.yml`` lists it. It shares
    # the relay's description rather than the watcher's: on an empty database it
    # writes no *labelled* family at all, so this sweep can prove only that its
    # listener binds and answers — which is exactly the thing that would otherwise
    # go missing unnoticed.
    Worker(
        "extraction",
        "app.workers.extraction_worker",
        "EXTRACTION_SCAN_POLL_INTERVAL_SECONDS",
    ),
)

#: Family -> the one worker allowed to emit a sample for it. Every entry here
#: carries labels, so a process that never wrote one renders **no sample at
#: all** — absence is the evidence that the registry is process-local, which is
#: what ``app/workers/metrics_server.py`` claims and what nothing had measured
#: outside the expiry sweep. ``notifications_sent_total`` is deliberately absent:
#: the API writes it too, so its presence in a worker proves nothing either way.
#: ``escalation_email_failed_total`` is absent for the opposite reason: since
#: ``D10-109`` it is seeded at declaration, so it renders ``0`` in *every*
#: process and its absence is no longer available as evidence — which is itself
#: pinned below, because it changes which target a rule may safely name.
SINGLE_WRITER_FAMILIES: dict[str, str] = {
    "contracts_expired_total": "expiry",
    "reminder_plans_total": "reminder_planner",
    "escalations_total": "escalation_scanner",
    # ``D10-117``. Labelled like the three above and written by one worker like them:
    # only the attention watcher reports the dead-letter backlog, so finding a sample
    # in another worker's body would mean the registry had stopped being process-local.
    "outbox_parked_events": "attention_watch",
    # ``D10-118``. Same worker, same argument, and worth an entry of its own: the two
    # families are two statements about two different tables, so one of them drifting
    # into another process would not be visible in the other's body.
    "notifications_undelivered": "attention_watch",
    # ``D10-120``. The extraction worker's other family is ``extraction_backlog``,
    # which carries no labels and so renders in *every* process — absence is not
    # available as evidence for it, which is why it gets no entry here. This one is
    # labelled, so a sample in any other worker's body would mean the registry had
    # stopped being process-local.
    "contracts_extracted_total": "extraction",
}

#: Worker -> the series that must already be on the wire before its listener counts
#: as ready (``D10-117``).
#:
#: Only the attention watcher belongs here. The other five write no family this
#: journey can provoke without inventing a due outbox row, a due notification and a
#: silent contract apiece, so a declaration is the strongest honest signal for them.
#: This one publishes both park series on *every* tick, zeros included — the same
#: property that makes its alert fireable makes its series waitable-for.
#:
#: Why the wait is not optional, stated as the defect it fixes: declarations are
#: rendered by the registry at import, while samples are written by a tick, and the
#: listener binds *before* the first tick. So "200 plus declarations" is a state the
#: process really sits in, and asserting a sample after returning on it is a race.
#: CI run ``43711886352`` lost that race on a commit identical to one that had won it
#: minutes earlier — the pass-then-fail-on-identical-code shape that
#: ``assert_port_is_free`` records for the port. Anything asserted after the return
#: has to be waited for here, or it is read from a body that predates it.
EVERY_TICK_SERIES: dict[str, tuple[str, ...]] = {
    "attention_watch": PARKED_EVENT_SERIES + UNDELIVERED_STATUS_SERIES
}


def required_series(worker: Worker) -> tuple[str, ...]:
    """The series :func:`wait_for_listener` must see before this worker is ready.

    A function rather than a bare ``.get`` at the call site so the mapping has one
    tested seam: ``tests/test_smoke_worker_metrics.py`` asserts both halves of it, and
    emptying :data:`EVERY_TICK_SERIES` fails there instead of only in a race. Nothing
    else may call this directly — ``verify_worker`` is its only caller, and a second
    copy of the lookup is the drift this exists to prevent.
    """
    return EVERY_TICK_SERIES.get(worker.name, ())


class SmokeError(RuntimeError):
    """A step of the journey did not hold."""


def need(condition: bool, message: str) -> None:
    """Fail the journey with a readable message rather than an ``AssertionError``."""
    if not condition:
        raise SmokeError(message)


def say(step: str, detail: str = "") -> None:
    """One line per step, so a partial run says where it stopped."""
    print(f"  ok  {step}{f' — {detail}' if detail else ''}", flush=True)


def build_database(path: Path) -> tuple[Engine, sessionmaker[Session]]:
    """Create the schema and the one contract the sweep will find.

    ``create_all`` rather than Alembic: this is a throwaway SQLite file and the
    migration chain is already proven against MySQL, so re-running it here would
    test Alembic, not the worker.
    """
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        # SQLite ships with foreign keys off, so a row could point at a tenant
        # that does not exist and this journey would "pass" on a database that
        # MySQL would have refused.
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        tenant = Tenant(name="Smoke Test Ltd", slug="smoke-metrics")
        session.add(tenant)
        session.flush()
        session.add(
            Contract(
                tenant_id=tenant.id,
                customer_ref="A. Rahman",
                end_date=PAST_END_DATE,
                timezone=UNKNOWN_ZONE,
            )
        )
        session.commit()
    return engine, factory


def free_port() -> int:
    """A loopback port nothing is listening on *right now*.

    Inherently racy, and accepted: the window is microseconds and the alternative
    is a hardcoded port that collides with a developer's other stack.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_worker(
    worker: Worker, database: Path, port: int, log_path: Path
) -> subprocess.Popen[bytes]:
    """Start one worker module as a real process, logging to a file.

    Logs go to a file rather than a pipe on purpose: a pipe nobody drains fills
    at ~64 KB and then blocks the child mid-tick, which looks exactly like a
    hang. The file is also what gets printed when a step fails.
    """
    env = dict(os.environ)
    env.update(
        {
            "APP_ENV": "test",
            "PYTHONPATH": str(_BACKEND_DIR),
            "DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "WORKER_METRICS_HOST": "127.0.0.1",
            "WORKER_METRICS_PORT": str(port),
            worker.poll_env: POLL_INTERVAL_SECONDS,
            # Quiet, but not silent: a crash before the bind is the case the tail
            # is printed for, and it logs at warning or above.
            "LOG_LEVEL": "WARNING",
            "LOG_JSON": "false",
            # Both stand-ins, so a journey never needs a broker, a relay or a
            # phone. A worker whose transport is missing refuses at the point of
            # use, not at import, so the listener still binds and is still asked.
            "EVENT_TRANSPORT": "none",
            "NOTIFICATION_TRANSPORT": "none",
            # ``D10-120``: the extraction worker builds its cipher and blind index in
            # ``__init__``, so it refuses to start without both — a different shape
            # from the workers above, which refuse at the point of use and are
            # therefore scrapable with nothing configured. Seeded here rather than in
            # the worker so the refusal stays where it is: a deployment that cannot
            # encrypt extracted values must not silently run extraction.
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "CONTACT_BLIND_INDEX_KEY": SMOKE_BLIND_INDEX_KEY,
        }
    )
    log_file = log_path.open("wb")
    return subprocess.Popen(  # noqa: S603 - argv is this module's own table, not input
        [sys.executable, "-m", worker.module],
        cwd=str(_BACKEND_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def scrape(port: int, path: str = "/metrics") -> tuple[int, bytes]:
    """One GET against the worker's listener. Returns status and body.

    ``urlopen`` raises for any non-2xx, so the error is unwrapped into a normal
    return here: this journey asserts on the status code the listener chose, and
    an exception is a different observation from a 404 that arrived on the wire.
    """
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def samples(body: str, name: str) -> dict[str, float]:
    """Parse the samples of one family out of a text-exposition body.

    Deliberately not a general parser: it reads the shape this registry emits
    (one name, an optional label set, a float) and ignores HELP/TYPE lines, which
    is all this journey needs to distinguish "a zero" from "no series".
    """
    found: dict[str, float] = {}
    for line in body.splitlines():
        if not line.startswith(name):
            continue
        if line.startswith("#"):
            continue
        head, _, value = line.partition(" ")
        # ``head`` is the name on its own or ``name{...}``; a longer family name
        # that merely starts with this one would be a different metric.
        if head != name and not head.startswith(f"{name}{{"):
            continue
        found[head] = float(value)
    return found


def wait_for_sample(port: int, deadline_seconds: float) -> tuple[str, dict[str, float]]:
    """Poll the listener until the sweep's counter appears, and return the body.

    The listener binds *before* the first tick runs, so a single scrape is a race
    the script would lose on a slow interpreter. The loop also notices a worker
    that died, which is the difference between "slow" and "broken".
    """
    deadline = time.monotonic() + deadline_seconds
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            status, raw = scrape(port)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            need(status == 200, f"GET /metrics answered {status}, expected 200")
            body = raw.decode("utf-8")
            found = samples(body, EXPECTED_FAMILY)
            if EXPECTED_SAMPLE in found:
                return body, found
            last_error = f"200 but no {EXPECTED_SAMPLE} sample yet"
        time.sleep(RETRY_SECONDS)
    raise SmokeError(f"worker never served a {EXPECTED_SAMPLE} sample ({last_error})")


def log_tail(path: Path, limit: int = 25) -> str:
    """The last lines the worker wrote, for a failure message worth reading."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - only on a vanished temp dir
        return f"<log unreadable: {exc}>"
    lines = text.splitlines()[-limit:]
    return "\n".join(lines) if lines else "<the worker logged nothing>"


def stop_worker(process: subprocess.Popen[bytes]) -> None:
    """Kill and reap.

    ``terminate`` on Windows is ``TerminateProcess``, which no handler can catch,
    so the worker's own SIGTERM shutdown is not exercised here — and that is the
    point of stopping this way: the assertion below is that the *kernel* releases
    the port when the process goes, not that the worker was polite. Reaping is
    not optional: a child left un-waited holds the parent's handles, and in this
    project an unreaped child has already wedged a shell.
    """
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)


def assert_port_released(port: int, deadline_seconds: float = 10.0) -> None:
    """Bind the port the way the module binds it, once the worker is gone.

    ``MetricsServer.start`` rather than a bare socket, for the lesson ``f1f6f53``
    paid for: on POSIX a bare bind can be refused for minutes while a closed
    connection lingers in ``TIME_WAIT``, which measures the kernel's timer rather
    than the listener's lifetime. Binding through the module applies the same
    ``SO_REUSEADDR`` rule the worker applied, so a refusal here means a listener,
    not a timer. A listener still running would be refused on both platforms.
    """
    deadline = time.monotonic() + deadline_seconds
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            server = MetricsServer.start("127.0.0.1", port, worker="release-probe")
        except MetricsServerError as exc:
            last_error = str(exc)
            time.sleep(RETRY_SECONDS)
        else:
            server.stop()
            return
    raise SmokeError(f"port {port} was never released after the worker exited ({last_error})")


def wait_for_listener(
    worker: Worker,
    port: int,
    deadline_seconds: float,
    require_series: tuple[str, ...] = (),
) -> str:
    """Poll until the worker answers a scrape, and return the body.

    Weaker than :func:`wait_for_sample` on purpose. Most of the workers write no
    family this journey can provoke without inventing a due outbox row, a due
    notification and a silent contract apiece, so for them the honest readiness
    signal is a ``200`` whose body carries the declarations every process holds.
    That is not vacuous: it fails for a worker that never binds, and for one whose
    body is not this registry.

    The declaration asserted is ``escalation_email_failed_total`` — the family
    ``D10-109`` made readable — because it is declared by ``app.core.metrics``
    itself, so it must appear in *every* process regardless of what that process
    does.

    ``require_series`` is for the worker that publishes a family every tick
    (:data:`EVERY_TICK_SERIES`), and it is not a convenience: a declaration arrives
    before the first tick while a sample arrives with it, so a body can be complete
    by the first measure and empty by the second. Waiting on both here is what makes
    the assertions *after* the return statements about this body.
    """
    deadline = time.monotonic() + deadline_seconds
    last_error = "no attempt made"
    marker = f"# HELP {SEEDED_ZERO_FAMILY}"
    while time.monotonic() < deadline:
        try:
            status, raw = scrape(port)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            need(
                status == 200,
                f"{worker.name}: GET /metrics answered {status}, expected 200",
            )
            body = raw.decode("utf-8")
            if marker not in body:
                last_error = f"200 without a '{marker}' declaration"
            else:
                # A series reads as its family for parsing: ``samples`` keys on the
                # head, so ``name{outcome="x"}`` is looked up as ``name``.
                missing = [
                    series
                    for series in require_series
                    if series not in samples(body, series.partition("{")[0])
                ]
                if not missing:
                    return body
                last_error = f"200 without {', '.join(missing)} yet"
        time.sleep(RETRY_SECONDS)
    raise SmokeError(f"{worker.name} never answered a scrape ({last_error})")


def verify_worker(worker: Worker, database: Path, workdir: Path) -> str:
    """Start one worker, prove it answers and is process-local, stop, prove release.

    Returns the body so a caller can assert on one specific family. The absence
    checks are the interesting half: a family another worker owns must have **no
    sample**, which is a different observation from a sample of zero and is what
    "each worker serves its own registry" actually means.
    """
    port = free_port()
    log_path = workdir / f"{worker.name}.log"
    process = start_worker(worker, database, port, log_path)
    try:
        body = wait_for_listener(worker, port, SCRAPE_DEADLINE_SECONDS, required_series(worker))
        for family, owner in SINGLE_WRITER_FAMILIES.items():
            if owner == worker.name:
                continue
            present = samples(body, family)
            need(
                not present,
                f"{worker.name} emits {family} samples ({present}), but only "
                f"{owner} writes it, so the registry is not process-local after "
                f"all and this assertion proves nothing",
            )
        say(
            f"{worker.name} answers a scrape and holds no other worker's series",
            f"127.0.0.1:{port}",
        )
    except SmokeError:
        print(f"\n--- {worker.name} log ---\n" + log_tail(log_path), flush=True)
        raise
    finally:
        stop_worker(process)
    assert_port_released(port)
    say(f"{worker.name} gave the port back when the process died", f"127.0.0.1:{port}")
    return body


def run() -> None:
    """The journey itself: seed, start, scrape, assert, stop, release — once per worker."""
    with tempfile.TemporaryDirectory(prefix="smoke-worker-metrics-") as raw_dir:
        workdir = Path(raw_dir)
        database = workdir / "smoke.sqlite"
        log_path = workdir / "worker.log"

        engine, _factory = build_database(database)
        try:
            say("seeded one contract with an unresolvable time zone", UNKNOWN_ZONE)

            port = free_port()
            process = start_worker(WORKERS[0], database, port, log_path)
            try:
                body, found = wait_for_sample(port, SCRAPE_DEADLINE_SECONDS)
                value = found[EXPECTED_SAMPLE]
                need(
                    value >= 1.0,
                    f"{EXPECTED_SAMPLE} reads {value}, expected at least one tick to count it",
                )
                say(
                    "the sweep's own counter arrived over the socket",
                    f"{EXPECTED_SAMPLE} {value:g}",
                )

                for name in UNWRITTEN_FAMILIES:
                    present = samples(body, name)
                    need(
                        not present,
                        f"{name} has samples here ({present}); the registry is not "
                        f"process-local, so this journey is proving nothing",
                    )
                say(
                    "families this worker never writes are declared and absent",
                    ", ".join(UNWRITTEN_FAMILIES),
                )

                status, _ = scrape(port, "/nope")
                need(status == 404, f"GET /nope answered {status}, expected 404")
                say("another path is a 404 on the wire, not just in a handler unit test")
            except SmokeError:
                print("\n--- worker log ---\n" + log_tail(log_path), flush=True)
                raise
            finally:
                stop_worker(process)

            assert_port_released(port)
            say("the port was released when the process died", f"127.0.0.1:{port}")

            # Phase two. The expiry sweep above is the one worker this journey can
            # provoke into writing a counter without inventing a fixture per
            # worker. Every worker is verified again here, expiry included, because
            # the locality check is the same check for every worker and because the
            # expiry body is the only one *guaranteed* to carry a worker-only
            # sample — which is what makes that check falsifiable instead of
            # merely quiet.
            say("--- every worker, each on its own port ---")
            bodies: dict[str, str] = {}
            for worker in WORKERS:
                bodies[worker.name] = verify_worker(worker, database, workdir)

            # The seeded zero is a *declaration*, not a recording, so it renders in
            # a process that can never write it. Pinned because it is the reason
            # `EscalationEmailFailed` has to be aimed at the scanner's port: pointed
            # at the relay, the rule reads a constant `0` for ever and looks
            # healthy. Before `D10-109` that mistake was at least visible as an
            # absent series; now only the target's identity tells the two apart.
            relay_baseline = samples(bodies["outbox_relay"], SEEDED_ZERO_FAMILY)
            need(
                relay_baseline == {SEEDED_ZERO_FAMILY: 0.0},
                f"the relay renders {relay_baseline} for {SEEDED_ZERO_FAMILY}, expected "
                f"the seeded zero: an absent series here would mean the counter is "
                f"unreadable again in the process that owns it",
            )
            say(
                "the seeded zero renders in a process that cannot write it",
                f"{SEEDED_ZERO_FAMILY} 0",
            )

            # ``D10-117``. Both outcome series must be on the wire from a watcher that
            # has ticked, and *presence* is the whole assertion: the value is a reading
            # of this throwaway database, where the relay may legitimately have parked
            # something under ``EVENT_TRANSPORT=none`` by the time this scrape happens.
            # What must never be true is "neither series is here", because an absent
            # gauge and a dead watcher are the same observation to `OutboxEventsParked`.
            # "Has ticked" is not assumed here: ``EVERY_TICK_SERIES`` made the scrape
            # wait for these series, so this asserts on a body that already carries them
            # and can only fail on an *extra* label value — which is the other half.
            parks = samples(bodies["attention_watch"], "outbox_parked_events")
            need(
                set(parks) == set(PARKED_EVENT_SERIES),
                f"the attention watcher renders {sorted(parks)} for outbox_parked_events, "
                f"expected {sorted(PARKED_EVENT_SERIES)}: a missing outcome would make the "
                f"alert unfireable for it, and an extra one would mean a label value "
                f"nobody declared",
            )
            say("the park gauge publishes every outcome it declares", str(parks))

            # ``D10-118``. Same shape of assertion, different table: presence only,
            # because the value is a reading of this throwaway database. A body short
            # of ``status="suppressed"`` would leave the backlog an operator is meant to
            # find invisible while the alert still looked healthy.
            undelivered = samples(bodies["attention_watch"], "notifications_undelivered")
            need(
                set(undelivered) == set(UNDELIVERED_STATUS_SERIES),
                f"the attention watcher renders {sorted(undelivered)} for "
                f"notifications_undelivered, expected {sorted(UNDELIVERED_STATUS_SERIES)}: "
                f"a missing status would hide a backlog, and an extra one would mean a "
                f"label value nobody declared",
            )
            say("the undelivered gauge publishes every status it declares", str(undelivered))
        finally:
            engine.dispose()


def main() -> int:
    """Run the journey, returning a shell exit code."""
    print("smoke: worker metrics over a real socket", flush=True)
    try:
        run()
    except SmokeError as exc:
        print(f"\nFAILED: {exc}", flush=True)
        return 1
    print("\nall steps passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
