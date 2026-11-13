"""D10-91's alert rule, produced by a refusal that really happened.

Why this file exists
--------------------
``D10-91`` added ``escalation_email_failed_total`` and the rule that reads it,
``increase(escalation_email_failed_total[1h]) > 0``. ``D10-107`` then gave the
escalation scanner its own ``/metrics`` listener, so the family could be served
by the process that writes it, and ``D10-108`` scraped a *different* worker in a
*different* process — and recorded, in its Limits paragraph, that D10-91's own
series "never fired — that needs the scanner plus a refusing relay".

This script is that sentence, executed. The unit tests prove the increment with a
``RefusingAlerter`` **stub** that raises on request. Between that stub and a real
refusal sit the real SMTP sender, a real socket, a real subprocess and the
registry that process serves, and none of those had been measured together — so
"the alert rule works" rested on a double standing in for the relay.

Running it found a defect instead of confirming a claim. The refusal *was* counted
and the listener *did* serve the count — but only once the counter had been
incremented, because a counter with no samples renders no series at all. Prometheus
therefore saw the family go absent → 1, and ``increase()`` over an absent series has
nothing to subtract, so the rule stayed quiet through exactly the event it exists to
catch: an escalation raised, an operator never told, and a dashboard reporting
health. The fix is in ``app.core.metrics`` (a counter with no labels reports zero
from the moment it is declared), and this journey now measures both halves of it in
two processes: one with nothing to escalate, where the series must be present at
zero, and one whose relay refuses, where the same series must read one.

It starts ``python -m app.workers.escalation_scanner`` against a temporary SQLite
database holding one contract that went quiet longer ago than the response
window, with ``NOTIFICATION_TRANSPORT=smtp`` aimed at a loopback port nothing is
listening on. The connection is refused by the kernel, the sender converts that
into its own error, the scanner counts it — and the count is read back over the
wire, from the scanner's own listener, as the family name and value the rule
queries. It also asserts the escalation itself survived: the row is in the queue
and the contract is ``escalated``, which is the post-commit ordering the module
promises (a dead relay may cost an alert, never a row).

What it does not prove
----------------------
It is not a Prometheus evaluation: parsing the text here is not Prometheus parsing
it, so whether the *rule* fires is measured elsewhere — D10-109 drove a real
Prometheus container over this same family from a scratch harness that is
deliberately not committed, which means the firing is recorded but not reproducible
from this repository. It does not run a container; the six worker ports Compose
publishes are still verified by parsing ``docker-compose.yml``. And the relay is
never reached at all — that is the point of the journey, not a limitation of it:
it says nothing about a relay that accepts a message and then bounces it, which no
counter in this repository observes.

How to run it
-------------
From the repository root::

    venv\\Scripts\\python.exe backend/tests/smoke_escalation_alert.py

Exit code 0 means every step passed. Non-zero means a step failed, and the
failing step is printed with the worker's own log tail so the reason is visible
rather than inferred. Nothing is left behind: the database and the log live in a
temporary directory removed at exit, and the worker is killed and reaped first.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

# The bootstrap mirrors smoke_worker_metrics.py: the backend is not an installed
# distribution, and APP_ENV must be pinned before app.core.config is imported so
# production validation cannot fire.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_DIR))
os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import Engine, create_engine, event, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.base import Base, utc_now  # noqa: E402
from app.db.enums import Channel, ContractStatus, NotificationStatus  # noqa: E402
from app.db.models import Contract, Escalation, Notification, Tenant  # noqa: E402

# The socket, port and process helpers are imported rather than copied. The
# fourth copy of ``scrape``/``samples`` would be a fourth place for them to
# drift, and ``assert_port_released`` in particular encodes a lesson this repo
# paid for: binding a bare socket measures the kernel's ``TIME_WAIT`` timer on
# POSIX, not the listener's lifetime, so the probe binds the way the module binds
# (see commit ``f1f6f53``). Reusing it means this journey cannot re-learn that.
from tests.smoke_worker_metrics import (  # noqa: E402
    RETRY_SECONDS,
    SmokeError,
    assert_port_released,
    free_port,
    log_tail,
    need,
    samples,
    say,
    scrape,
    stop_worker,
)

#: How long the contract has been silent. Deliberately larger than the scanner's
#: default window (``ESCALATION_NO_RESPONSE_DAYS``, 14) so the claim query leases
#: it on the first tick without the journey having to tune the window itself.
SILENCE_DAYS = 30

#: How recently the *other* contract was contacted, in the phase that must count
#: nothing. One day, well inside the response window, so the scanner has a real
#: contract to look at and no reason to escalate it: the counter stays at its
#: baseline because nothing failed, not because nothing ran.
QUIET_SILENCE_DAYS = 1

#: The family ``D10-91``'s rule queries. It carries no labels, so the family name
#: is the whole series and ``increase(...[1h])`` needs nothing else.
EXPECTED_FAMILY = "escalation_email_failed_total"

#: The scanner's own series, split by outcome. ``opened`` is the one this journey
#: must see: it is the evidence that the refusal cost a counter and a log line
#: rather than the escalation.
ESCALATIONS_FAMILY = "escalations_total"
EXPECTED_OPENED = f'{ESCALATIONS_FAMILY}{{status="opened"}}'

#: How long the worker gets to bind, tick, refuse and be scraped. Generous: a cold
#: interpreter, two imports and a refused TCP connect are all inside it.
SCRAPE_DEADLINE_SECONDS = 30.0

#: A fast scan, so the first scrape attempt does not race a five-minute default.
#: ``run_forever`` ticks before it waits, so this only bounds later ticks.
POLL_INTERVAL_SECONDS = "0.5"

#: Bounded so a port that silently swallows the SYN fails this journey in seconds
#: instead of hanging it. A refused port answers immediately on both platforms.
RELAY_TIMEOUT_SECONDS = "5"


def build_database(path: Path, *, silence_days: int) -> tuple[Engine, sessionmaker[Session], int]:
    """Create the schema and one contract last contacted ``silence_days`` ago.

    The shape has to satisfy all four of ``claim_silent_contracts``'s conditions,
    which is why the notification matters as much as the contract: a contract with
    no accepted contact is not "silent", it was never contacted, and the scanner
    refuses to escalate that — correctly, since nothing has gone unanswered.

    ``silence_days`` is the only thing that differs between the two phases, and it
    is a keyword so that a call site cannot silently inherit the wrong one: 30 days
    is claimable, 1 day is not, and the whole point of running both is that the
    first produces a refusal while the second produces nothing.
    """
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        # SQLite ships with foreign keys off, so a row could point at a tenant that
        # does not exist and this journey would "pass" on a database MySQL refuses.
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        tenant = Tenant(name="Smoke Operator Ltd", slug="smoke-escalation-alert")
        session.add(tenant)
        session.flush()
        contract = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            status=ContractStatus.ACTIVE,
        )
        session.add(contract)
        session.flush()
        sent_at = utc_now() - timedelta(days=silence_days)
        session.add(
            Notification(
                tenant_id=tenant.id,
                contract_id=contract.id,
                channel=Channel.EMAIL,
                template="T-60",
                status=NotificationStatus.SENT,
                scheduled_date=sent_at.date(),
                scheduled_at=sent_at,
                sent_at=sent_at,
                idempotency_key="smoke-escalation-alert-T-60",
            )
        )
        session.commit()
        contract_id = contract.id
    return engine, factory, contract_id


def start_worker(
    database: Path,
    port: int,
    log_path: Path,
    *,
    relay_port: int,
    alert_to: str,
) -> subprocess.Popen[bytes]:
    """Start the escalation scanner as a real process, aimed at a dead relay.

    ``relay_port`` is a loopback port with nothing behind it, so the kernel refuses
    the connection. That is a *real* failure on the real code path — the alternative
    would be a stubbed alerter, which is exactly what the unit tests already do and
    what this journey exists to look past.

    Logs go to a file rather than a pipe on purpose: a pipe nobody drains fills at
    ~64 KB and then blocks the child mid-tick, which looks exactly like a hang.
    """
    env = dict(os.environ)
    env.update(
        {
            "APP_ENV": "test",
            "PYTHONPATH": str(_BACKEND_DIR),
            "DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "WORKER_METRICS_HOST": "127.0.0.1",
            "WORKER_METRICS_PORT": str(port),
            "ESCALATION_SCAN_POLL_INTERVAL_SECONDS": POLL_INTERVAL_SECONDS,
            # Settings validation refuses an alert address with no SMTP transport
            # behind it, which is why this block cannot be omitted: the refusal is
            # the feature.
            "NOTIFICATION_TRANSPORT": "smtp",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(relay_port),
            "SMTP_FROM": "agent@smoke.test",
            "SMTP_TIMEOUT_SECONDS": RELAY_TIMEOUT_SECONDS,
            "ESCALATION_ALERT_TO": alert_to,
            # Info, not warning: the alert's *failure* logs at error, but the
            # scanner's "started" line is what says whether an alerter was built at
            # all, and a journey failing on "no alerter" wants that line present.
            "LOG_LEVEL": "INFO",
            "LOG_JSON": "false",
            "EVENT_TRANSPORT": "none",
        }
    )
    log_file = log_path.open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", "app.workers.escalation_scanner"],
        cwd=str(_BACKEND_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def scrape_until(
    port: int,
    deadline_seconds: float,
    *,
    condition: Callable[[dict[str, float]], bool],
    waiting_for: str,
) -> tuple[str, dict[str, float]]:
    """Poll the listener until ``condition`` holds over the parsed samples.

    Both phases share this loop and differ only in what they are waiting for. One
    waits for the socket to answer at all, because a process with nothing to
    escalate has no value to wait for and the assertion is about a series that is
    already there at zero. The other waits for a particular value.

    Waiting for the family to *appear* is what this used to do, and that stopped
    being a signal when D10-109 gave the family a zero to appear at: the loop would
    have returned on the first scrape with the baseline and the assertion after it
    would have compared zero against one. So the condition is the value, and the
    deadline message repeats the value last seen — which is what tells a worker
    that is slow to start apart from one that is stuck at zero.
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
            if condition(found):
                return body, found
            value = found.get(EXPECTED_FAMILY)
            last_error = (
                f"{EXPECTED_FAMILY} reads {value:g}"
                if value is not None
                else f"{EXPECTED_FAMILY} is absent altogether"
            )
        time.sleep(RETRY_SECONDS)
    raise SmokeError(f"the scanner never {waiting_for} (last scrape: {last_error})")


def logged(path: Path, needle: str) -> bool:
    """Whether the worker wrote ``needle`` anywhere in its log.

    The whole file rather than a tail: the refusal is logged once, on the first
    tick, and a later tick on a quiet board writes nothing — so a tail can be
    nothing but idle ticks by the time the scrape succeeds.
    """
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - only on a vanished temp dir
        return False


def count_escalations(factory: sessionmaker[Session], contract_id: int) -> list[Escalation]:
    """The escalation rows the scanner committed, read after the process is gone."""
    with factory() as session:
        return list(
            session.scalars(select(Escalation).where(Escalation.contract_id == contract_id))
        )


def contract_status(factory: sessionmaker[Session], contract_id: int) -> ContractStatus:
    """The contract's status after the pass. Read from the row, not from a log."""
    with factory() as session:
        return session.get_one(Contract, contract_id).status


def check_the_family_reports_zero() -> None:
    """Phase one: the family is served at zero before any alert has failed.

    A contract contacted a day ago is inside the response window, so the scanner
    has something real to consider and no reason to escalate it: the alerter is
    configured, valid, and never reached. That matters, because the zero has to be
    the counter's baseline rather than the side effect of a missing alerter.

    This is the assertion that was missing before D10-109. The refusal itself was
    always counted; what nobody had measured is what the family looks like in the
    hour *before* one — and it looked like nothing at all, which is why
    ``increase()`` had no pair to subtract on the day it mattered.
    """
    with tempfile.TemporaryDirectory(prefix="smoke-escalation-alert-quiet-") as raw_dir:
        workdir = Path(raw_dir)
        database = workdir / "quiet.sqlite"
        log_path = workdir / "quiet.log"

        engine, _factory, _contract_id = build_database(database, silence_days=QUIET_SILENCE_DAYS)
        try:
            port = free_port()
            process = start_worker(
                database,
                port,
                log_path,
                relay_port=free_port(),
                alert_to="operator@smoke.test",
            )
            try:
                body, found = scrape_until(
                    port,
                    SCRAPE_DEADLINE_SECONDS,
                    condition=lambda _found: True,
                    waiting_for="served its metrics listener",
                )
                need(
                    EXPECTED_FAMILY in found,
                    f"{EXPECTED_FAMILY} is declared and carries no sample: a counter "
                    "reports zero until it rises, or the first failure is absent -> 1 "
                    "and no rule reading increase() can ever see it",
                )
                baseline = found[EXPECTED_FAMILY]
                need(
                    baseline == 0.0,
                    f"{EXPECTED_FAMILY} reads {baseline:g} before any refusal, expected 0",
                )
                say(
                    "a process that never failed an alert reports the family, at zero",
                    f"{EXPECTED_FAMILY} {baseline:g}",
                )
                need(
                    not samples(body, ESCALATIONS_FAMILY),
                    f"{ESCALATIONS_FAMILY} has samples on a board with nothing to "
                    "escalate: a labelled family must stay absent until an outcome is "
                    "written, so this process has to show both answers at once",
                )
                say(
                    "the labelled family with no outcome written is absent, not zero",
                    ESCALATIONS_FAMILY,
                )
            except SmokeError:
                print("\n--- quiet worker log ---\n" + log_tail(log_path), flush=True)
                raise
            finally:
                stop_worker(process)
        finally:
            engine.dispose()


def run() -> None:
    """Both phases, in order: the baseline, then the refusal that moves it."""
    check_the_family_reports_zero()
    check_a_refused_alert_is_counted()


def check_a_refused_alert_is_counted() -> None:
    """Phase two: seed, start, refuse, scrape, assert, stop, release."""
    with tempfile.TemporaryDirectory(prefix="smoke-escalation-alert-") as raw_dir:
        workdir = Path(raw_dir)
        database = workdir / "smoke.sqlite"
        log_path = workdir / "worker.log"

        engine, factory, contract_id = build_database(database, silence_days=SILENCE_DAYS)
        try:
            say("seeded one contract silent for more than the response window", f"{SILENCE_DAYS}d")

            port = free_port()
            relay_port = free_port()
            process = start_worker(
                database,
                port,
                log_path,
                relay_port=relay_port,
                alert_to="operator@smoke.test",
            )
            try:
                body, failed = scrape_until(
                    port,
                    SCRAPE_DEADLINE_SECONDS,
                    condition=lambda found: found.get(EXPECTED_FAMILY) == 1.0,
                    waiting_for="counted a refused alert",
                )
                value = failed[EXPECTED_FAMILY]
                # Exactly one, not "at least one". The claim query excludes a
                # contract that already has an open escalation, so a second tick
                # must raise nothing and count nothing; a value above one would
                # mean the queue is about to hold duplicate rows for one customer.
                need(
                    value == 1.0,
                    f"{EXPECTED_FAMILY} reads {value:g}, expected exactly 1: the alert "
                    f"failed once, and a further tick must not re-raise the escalation",
                )
                say(
                    "the refused alert was counted, on the family D10-91's rule reads",
                    f"{EXPECTED_FAMILY} {value:g}",
                )

                opened = samples(body, ESCALATIONS_FAMILY)
                need(
                    EXPECTED_OPENED in opened,
                    f"no {EXPECTED_OPENED} sample: the refusal cost the escalation too, "
                    f"which is the ordering this scan must never have",
                )
                need(
                    opened[EXPECTED_OPENED] == 1.0,
                    f"{EXPECTED_OPENED} reads {opened[EXPECTED_OPENED]:g}, expected 1",
                )
                # A relay refusal is not a policy refusal. The scanner's ``refused``
                # outcome counts contracts its own policy declined, and mixing the
                # two would make a dead relay look like a data problem.
                refused_series = f'{ESCALATIONS_FAMILY}{{status="refused"}}'
                need(
                    refused_series not in opened,
                    f"{refused_series} appeared; a refused alert is not a refused contract",
                )
                say(
                    "the escalation was still opened and counted",
                    f"{EXPECTED_OPENED} {opened[EXPECTED_OPENED]:g}",
                )
            except SmokeError:
                print("\n--- worker log ---\n" + log_tail(log_path), flush=True)
                raise
            finally:
                stop_worker(process)

            # The log is evidence the failure was *noticed* rather than merely
            # counted: the module promises a counter and one log line, because a
            # series nobody reads and a log nobody writes are the same silence.
            need(
                logged(log_path, "escalation_alert_failed"),
                "the worker never logged escalation_alert_failed; the failure was "
                "counted but not reported, and the log tail is:\n" + log_tail(log_path),
            )
            say("the refusal was logged, not only counted", "escalation_alert_failed")

            # Across the process boundary, in the database the worker wrote: the
            # alert runs after the commit, so a refused relay cannot cost a row.
            rows = count_escalations(factory, contract_id)
            need(
                len(rows) == 1,
                f"{len(rows)} escalation rows for contract {contract_id}, expected 1",
            )
            status = contract_status(factory, contract_id)
            need(
                status is ContractStatus.ESCALATED,
                f"contract {contract_id} is {status.value}, expected escalated",
            )
            say(
                "the escalation survived the dead relay",
                f"contract {contract_id} -> {status.value}, escalation {rows[0].id}",
            )

            assert_port_released(port)
            say("the port was released when the process died", f"127.0.0.1:{port}")
        finally:
            engine.dispose()


def main() -> int:
    """Run the journey, returning a shell exit code."""
    print("smoke: a refused escalation alert, on a real socket", flush=True)
    try:
        run()
    except SmokeError as exc:
        print(f"\nFAILED: {exc}", flush=True)
        return 1
    print("\nall steps passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
