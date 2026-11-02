"""Readiness probing (F5).

Beyond "does it return 200", two properties are asserted that the obvious test
would miss:

* readiness fails **closed** — an unusable dependency must produce 503, because a
  probe that cannot fail is not a probe;
* the response discloses nothing about **why**. ``/health/ready`` is
  unauthenticated, so driver text (which can echo a DSN, a host or a user) must
  not travel from a failed connection into a public body.

The Kafka check is always driven through an injected probe. There is no broker in
``docker-compose.yml`` and none in CI, so a test that needed one would be a test
that only ever skips.
"""

from __future__ import annotations

import time
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.readiness import (
    REASON_NOT_CONFIGURED,
    REASON_TIMEOUT,
    REASON_UNAVAILABLE,
    CheckResult,
    check_event_transport,
    readiness_report,
)
from app.main import create_app


class UnreachableDatabase:
    """A session factory whose every use fails, standing in for a downed server."""

    def __init__(self, detail: str = "can't connect to 127.0.0.1") -> None:
        self.detail = detail

    def __call__(self) -> Session:
        raise RuntimeError(self.detail)


def broken_factory(detail: str = "can't connect to 127.0.0.1") -> sessionmaker[Session]:
    """A factory the app cannot use, typed as one it could.

    The cast is the point of the test: the app is handed something that satisfies
    the factory contract but fails when called, which is exactly what a downed
    database looks like from the outside.
    """
    return cast("sessionmaker[Session]", UnreachableDatabase(detail))


def kafka_settings() -> Settings:
    """Test settings with a transport selected, so the bus check applies.

    ``broker.invalid`` is reserved and never resolves (RFC 6761), so a probe that
    escapes its injected fake fails fast instead of reaching a real host.
    """
    return Settings(
        app_env="test",
        log_json=False,
        event_transport="kafka",
        kafka_bootstrap_servers="broker.invalid:9092",
    )


# --- the endpoint ---------------------------------------------------------


def test_ready_when_every_dependency_answers(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": {"ok": True}}


def test_no_transport_is_omitted_rather_than_reported_green(client: TestClient) -> None:
    """A green tick for a transport the process does not use is a lie by omission."""
    checks = client.get("/health/ready").json()["checks"]

    assert "event_transport" not in checks


def test_readiness_reports_the_service_it_is(client: TestClient) -> None:
    body = client.get("/health/ready").json()

    assert body["service"]
    assert body["version"]
    assert body["environment"] == "test"


def test_not_ready_when_the_database_is_unreachable(settings: Settings) -> None:
    app = create_app(settings, session_factory=broken_factory())
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == {"ok": False, "reason": REASON_UNAVAILABLE}


def test_a_failing_check_does_not_leak_the_driver_error(settings: Settings) -> None:
    detail = "Access denied for user 'app'@'10.0.0.5' (using password: YES)"
    app = create_app(settings, session_factory=broken_factory(detail))
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert detail not in response.text
    assert "10.0.0.5" not in response.text
    assert "Access denied" not in response.text


def test_liveness_stays_independent_of_dependencies(settings: Settings) -> None:
    """The separation that stops a database blip from becoming a restart loop."""
    app = create_app(settings, session_factory=broken_factory())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# --- the checks -----------------------------------------------------------


async def test_only_the_database_is_checked_without_a_transport(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    report = await readiness_report(session_factory, settings)

    assert tuple(report.as_dict()) == ("database",)
    assert report.ready


async def test_a_selected_transport_is_probed(
    session_factory: sessionmaker[Session],
) -> None:
    report = await readiness_report(session_factory, kafka_settings(), probe=lambda: None)

    assert report.as_dict()["database"] == {"ok": True}
    assert report.as_dict()["event_transport"] == {"ok": True}
    assert report.ready


async def test_an_unreachable_broker_makes_the_service_not_ready(
    session_factory: sessionmaker[Session],
) -> None:
    def failing_probe() -> None:
        raise RuntimeError("NoBrokersAvailable")

    report = await readiness_report(session_factory, kafka_settings(), probe=failing_probe)

    # The database is fine; the verdict is still "not ready", because a request
    # that needs to publish would be accepted and then fail.
    assert report.as_dict()["database"] == {"ok": True}
    assert report.as_dict()["event_transport"] == {"ok": False, "reason": REASON_UNAVAILABLE}
    assert not report.ready


def test_no_transport_reports_not_configured() -> None:
    """Called directly, the bus check is total rather than raising."""
    result = check_event_transport(Settings(app_env="test", log_json=False))

    assert result == CheckResult("event_transport", True, REASON_NOT_CONFIGURED)


async def test_a_hanging_check_times_out_instead_of_hanging_the_probe(
    session_factory: sessionmaker[Session],
) -> None:
    class HangingDatabase:
        def __call__(self) -> Session:
            time.sleep(5)
            raise RuntimeError("never reached inside the timeout")

    settings = Settings(app_env="test", log_json=False, readiness_timeout_seconds=0.05)

    started = time.monotonic()
    report = await readiness_report(cast("sessionmaker[Session]", HangingDatabase()), settings)
    elapsed = time.monotonic() - started

    # Bounded by the timeout, not by the dependency: a probe that waits for a
    # wedged connection hands the orchestrator no verdict at all.
    assert elapsed < 1.0
    assert report.as_dict()["database"] == {"ok": False, "reason": REASON_TIMEOUT}
    assert not report.ready


@pytest.mark.parametrize(
    ("ok", "expected"),
    [
        (True, {"ok": True}),
        (False, {"ok": False, "reason": "unavailable"}),
    ],
)
def test_a_check_result_omits_an_absent_reason(ok: bool, expected: dict[str, object]) -> None:
    reason = None if ok else "unavailable"

    assert CheckResult("database", ok, reason).as_dict() == expected
