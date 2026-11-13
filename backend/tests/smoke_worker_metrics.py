"""One real worker process, one real socket, one real scrape.

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

What it does not prove
----------------------
It is not a unit test of the listener: path, verb, header and error-body details
belong to ``tests/test_metrics_server.py``. It does not scrape a *container* —
the six ports Compose publishes are still verified by parsing the compose file,
and this script is about the process, the socket and the sample. It runs one
worker, not six. And it says nothing about fidelity to Prometheus: parsing the
text here is not the same as Prometheus parsing it, which is why the rule that
reads this series is evaluated in the ledger's measurement rather than here.

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
from datetime import date
from pathlib import Path

# The bootstrap mirrors backend/tests/conftest.py and smoke_journey.py: the
# backend is not an installed distribution (see the pyproject header), and
# APP_ENV must be pinned before app.core.config is imported so production
# validation cannot fire.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_DIR))
os.environ.setdefault("APP_ENV", "test")

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

#: A fast sweep, so the first scrape attempt does not race a one-hour default.
POLL_INTERVAL_SECONDS = "0.5"

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


def start_worker(database: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    """Start the expiry worker as a real process, logging to a file.

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
            "EXPIRY_SWEEP_POLL_INTERVAL_SECONDS": POLL_INTERVAL_SECONDS,
            # Quiet, but not silent: a crash before the bind is the case the tail
            # is printed for, and it logs at warning or above.
            "LOG_LEVEL": "WARNING",
            "LOG_JSON": "false",
            "EVENT_TRANSPORT": "none",
            "NOTIFICATION_TRANSPORT": "none",
        }
    )
    log_file = log_path.open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", "app.workers.expiry"],
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


def run() -> None:
    """The journey itself: seed, start, scrape, assert, stop, release."""
    with tempfile.TemporaryDirectory(prefix="smoke-worker-metrics-") as raw_dir:
        workdir = Path(raw_dir)
        database = workdir / "smoke.sqlite"
        log_path = workdir / "worker.log"

        engine, _factory = build_database(database)
        try:
            say("seeded one contract with an unresolvable time zone", UNKNOWN_ZONE)

            port = free_port()
            process = start_worker(database, port, log_path)
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
