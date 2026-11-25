"""Tests for the malware scanner behind ``VIRUS_SCAN_TRANSPORT`` (P2-A's third part).

Exercised against a *real* loopback socket rather than a monkeypatched
``socket.create_connection``. That is a deliberate departure from
``test_voice.py``, which fakes the HTTP transport: there the thing worth pinning
was the request the sender built, while here the thing worth pinning is the bytes
on the wire, and ``INSTREAM`` is a framing protocol — a length-prefixed stream
whose correctness is invisible to a caller that never sees it framed. A fake
daemon on a real socket is the only double that can disagree with the module about
how a document is delimited.

What gets the attention:

* **the framing is the one clamd speaks.** ``zINSTREAM`` followed by length-prefixed
  chunks followed by a zero-length chunk, with no single frame larger than the
  module's buffer. A change that breaks this passes every unit test of the verdict
  logic and fails against a real clamd.
* **only ``stream: OK`` permits a document.** An empty reply, a version banner, an
  ``ERROR`` line — all refused, because "I could not tell" must never read as
  "clean".
* **the far end is untrusted input.** A signature name arrives over a plaintext
  socket and would be reflected into a 422 body and a log line if the module put it
  in an exception. It does not; neither does it carry the document, the endpoint,
  or the payload.
* **the scan happens between the duplicate check and the store.** An infected
  document never reaches the disk, and a document this deployment already stored
  is answered with its 409 without a second scan.
* **every refusal is a mapped refusal.** The three specific subclasses map to their
  own status codes; the base class is mapped too, so a scanner this build did not
  write cannot turn "I will not accept this" into a 500 that says nothing.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import document_scanner
from app.core.antivirus import (
    ClamdScanner,
    DocumentInfectedError,
    DocumentScanError,
    DocumentScanUnavailableError,
    DocumentScanUnreadableError,
    build_scanner,
)
from app.core.config import Settings
from app.core.documents import document_digest
from app.db.enums import UserRole
from app.db.models import Contract, OutboxEvent, Tenant, User
from app.db.session import session_scope
from app.main import create_app

DOCUMENT_PATH = "/api/v1/contracts/document"
LOGIN_PATH = "/api/v1/auth/login"

#: The media type the route declares, written out rather than imported for the same
#: reason ``test_documents`` writes it out: a change to the constant should stop the
#: upload below matching the client's request, which is what this suite notices.
DOCUMENT_MEDIA_TYPE = "application/octet-stream"

#: The protocol constants, written out rather than imported from the module under
#: test. If somebody changes the command or the terminator, the fake daemon here
#: should fail to understand it — that is the drift this suite exists to catch.
INSTREAM_COMMAND = b"zINSTREAM\x00"
STREAM_END = b"\x00\x00\x00\x00"

#: The clean verdict, verbatim, as clamd writes it.
CLEAN_REPLY = b"stream: OK\x00"

#: A positive verdict, shaped like the real thing: the signature name sits between
#: the ``stream:`` prefix and the ``FOUND`` suffix, and is far-end text.
INFECTED_REPLY = b"stream: Eicar-Test-Signature FOUND\x00"

#: An answer this build has no meaning for. Naming no verdict at all is the point.
UNREADABLE_REPLY = b"INSTREAM size limit exceeded. ERROR\x00"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"

#: Byte-distinct content, so a truncated or reordered stream shows up as a mismatch
#: rather than passing by luck. Recognisably a PDF because the route reads the bytes
#: to decide the format.
PAYLOAD = b"%PDF-1.7\n" + bytes(range(256)) * 4

#: A payload larger than one frame, so the chunking loop is actually entered. Three
#: frames at the module's 8 KiB buffer: 8192 + 8192 + 4096.
LARGE_PAYLOAD = bytes(range(256)) * 80

#: One mebibyte: big enough that no test payload is accidentally over it.
ONE_MIB = 1024 * 1024

#: The frame size the module documents. Asserted from the far side rather than
#: imported, so the two have to agree on the wire.
FRAME_BYTES = 8192


# --- a daemon on a real socket --------------------------------------------
def _read_exactly(connection: socket.socket, size: int) -> bytes:
    """Best-effort read of ``size`` bytes; short reads are returned short."""
    received = bytearray()
    while len(received) < size:
        piece = connection.recv(size - len(received))
        if not piece:
            break
        received.extend(piece)
    return bytes(received)


def closed_port() -> int:
    """A loopback port with nothing listening on it.

    Bound and released rather than hardcoded, so the test cannot collide with
    whatever else is running on the machine.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class FakeClamd:
    """A loopback daemon that speaks ``INSTREAM`` and answers with one canned reply.

    Records what it was actually sent, because the framing is the property under
    test. Failures are collected rather than raised: an assertion inside a thread is
    swallowed, so the test asserts on :attr:`problems` instead, which turns a
    malformed stream into a readable failure instead of a hang or a silent pass.
    """

    def __init__(self, reply: bytes = CLEAN_REPLY) -> None:
        self.reply = reply
        self.payloads: list[bytes] = []
        self.chunk_sizes: list[int] = []
        self.connections = 0
        self.problems: list[str] = []
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.05)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeClamd:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            try:
                self._converse(connection)
            finally:
                connection.close()

    def _converse(self, connection: socket.socket) -> None:
        connection.settimeout(5)
        command = _read_exactly(connection, len(INSTREAM_COMMAND))
        if command != INSTREAM_COMMAND:
            self.problems.append(f"command was {command!r}, not the INSTREAM one")
            return
        payload = bytearray()
        while True:
            prefix = _read_exactly(connection, 4)
            if len(prefix) != 4:
                self.problems.append("the length prefix was truncated")
                return
            size = int.from_bytes(prefix, "big")
            if size == 0:
                break
            chunk = _read_exactly(connection, size)
            if len(chunk) != size:
                self.problems.append(f"a chunk said {size} bytes and delivered {len(chunk)}")
                return
            self.chunk_sizes.append(size)
            payload.extend(chunk)
        self.payloads.append(bytes(payload))
        connection.sendall(self.reply)


# --- helpers --------------------------------------------------------------
def scan_settings(**overrides: Any) -> Settings:
    """Settings with a scanner configured against the loopback default.

    Constructed through ``Settings`` rather than ``model_copy`` on purpose: the
    start-up refusals below are validators, and a ``model_copy`` would skip them.
    """
    values: dict[str, Any] = {
        "app_env": "test",
        "log_json": False,
        "virus_scan_transport": "clamd",
        "clamd_host": "127.0.0.1",
        "clamd_port": 3310,
        "clamd_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return Settings(**values)


def scanner(port: int, *, timeout_seconds: float = 5.0) -> ClamdScanner:
    return ClamdScanner(host="127.0.0.1", port=port, timeout_seconds=timeout_seconds)


def make_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    encryption_key: str,
    store_root: Path,
    scanning: FakeClamd | None = None,
    port: int | None = None,
) -> TestClient:
    """Build the app with a document store in the temp directory and a chosen scanner.

    ``scanning`` names a running fake daemon; ``port`` names a bare port with
    nothing behind it. Neither means ``VIRUS_SCAN_TRANSPORT=none``, which is the
    third configuration and the one every other upload test runs under.
    """
    update: dict[str, Any] = {
        "encryption_key": encryption_key,
        "document_store_path": str(store_root),
        "document_max_bytes": ONE_MIB,
        "virus_scan_transport": "none",
    }
    if scanning is not None or port is not None:
        update |= {
            "virus_scan_transport": "clamd",
            "clamd_host": "127.0.0.1",
            "clamd_port": scanning.port if scanning is not None else port,
            "clamd_timeout_seconds": 5.0,
        }
    return TestClient(
        create_app(settings.model_copy(update=update), session_factory=session_factory)
    )


def login(client: TestClient, tenant: Tenant, email: str = EMAIL) -> dict[str, str]:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def auth(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    *,
    role: UserRole = UserRole.OPERATOR,
    email: str = EMAIL,
) -> dict[str, str]:
    make_user(tenant, email=email, role=role)
    return login(client, tenant, email)


def upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    payload: bytes = PAYLOAD,
    customer_ref: str = "A. Rahman",
) -> Response:
    """POST a document, with the one media type the route expects."""
    return client.post(
        DOCUMENT_PATH,
        params={"customer_ref": customer_ref},
        content=payload,
        headers={**headers, "Content-Type": DOCUMENT_MEDIA_TYPE},
    )


def payload_markers() -> bytes:
    """A run of bytes that appears verbatim in :data:`PAYLOAD`.

    Used to assert that a document's content did not leak into an error: a
    ciphertext or a hash would not contain it, so a hit is a real leak.
    """
    return PAYLOAD[16:48]


def contract_rows(session_factory: sessionmaker[Session]) -> list[Contract]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(Contract)).scalars())


def outbox_rows(session_factory: sessionmaker[Session]) -> list[OutboxEvent]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(OutboxEvent)).scalars())


def blobs(root: Path) -> list[Path]:
    """Every file under the store root, so "nothing was written" can be asserted."""
    return [path for path in root.rglob("*") if path.is_file()]


# --- the wire -------------------------------------------------------------
def test_a_clean_reply_is_the_one_answer_that_permits_a_document() -> None:
    with FakeClamd(CLEAN_REPLY) as daemon:
        assert scanner(daemon.port).scan(PAYLOAD) is None

    assert daemon.problems == []
    assert daemon.payloads == [PAYLOAD]


def test_the_document_crosses_the_socket_as_length_prefixed_frames() -> None:
    """The framing, which is the part no verdict-level test can see."""
    with FakeClamd(CLEAN_REPLY) as daemon:
        scanner(daemon.port).scan(LARGE_PAYLOAD)

    assert daemon.problems == []
    # Delivered whole and in order: a reassembly bug shows as a mismatch here.
    assert daemon.payloads == [LARGE_PAYLOAD]
    # More than one frame, none over the documented buffer, summing to the payload.
    assert len(daemon.chunk_sizes) == 3
    assert max(daemon.chunk_sizes) == FRAME_BYTES
    assert sum(daemon.chunk_sizes) == len(LARGE_PAYLOAD)


def test_an_empty_document_still_ends_the_stream_properly() -> None:
    """A zero-byte payload is an immediate terminator, not a frame of nothing."""
    with FakeClamd(CLEAN_REPLY) as daemon:
        assert scanner(daemon.port).scan(b"") is None

    assert daemon.problems == []
    assert daemon.payloads == [b""]
    assert daemon.chunk_sizes == []


def test_a_signature_match_is_refused() -> None:
    with (
        FakeClamd(INFECTED_REPLY) as daemon,
        pytest.raises(DocumentInfectedError, match="matched a known malware signature"),
    ):
        scanner(daemon.port).scan(PAYLOAD)


def test_the_signature_name_never_reaches_the_error_or_the_repr() -> None:
    """The name is far-end text and would otherwise land in a 422 body."""
    with FakeClamd(INFECTED_REPLY) as daemon, pytest.raises(DocumentInfectedError) as raised:
        scanner(daemon.port).scan(PAYLOAD)

    for surface in (str(raised.value), repr(raised.value), repr(scanner(daemon.port))):
        assert "Eicar" not in surface
        assert "FOUND" not in surface


def test_an_answer_this_build_does_not_recognise_is_refused_not_read_as_clean() -> None:
    with (
        FakeClamd(UNREADABLE_REPLY) as daemon,
        pytest.raises(DocumentScanUnreadableError, match="does not recognise") as raised,
    ):
        scanner(daemon.port).scan(PAYLOAD)

    # The reply text is not echoed: it is far-end text and this reaches a response.
    assert "size limit" not in str(raised.value)


def test_a_daemon_that_answers_nothing_is_refused_not_read_as_clean() -> None:
    """EOF without a verdict is the fourth answer, and it is not approval."""
    with (
        FakeClamd(b"") as daemon,
        pytest.raises(DocumentScanUnreadableError, match="does not recognise"),
    ):
        scanner(daemon.port).scan(PAYLOAD)


def test_a_verdict_line_without_its_terminator_is_still_understood() -> None:
    """A daemon that closes right after answering is not a different answer."""
    with FakeClamd(b"stream: OK") as daemon:
        assert scanner(daemon.port).scan(PAYLOAD) is None


def test_no_scanner_answering_is_unavailable_rather_than_unreadable() -> None:
    """The distinction is the operator's: a dead service, not a wrong one."""
    with pytest.raises(DocumentScanUnavailableError, match="no scanner answered") as raised:
        scanner(closed_port()).scan(PAYLOAD)

    # The exception names the failure type and nothing else — no endpoint, no bytes.
    assert "127.0.0.1" not in str(raised.value)
    assert payload_markers() not in str(raised.value).encode("utf-8", "replace")


def test_the_endpoint_is_named_in_the_repr_and_no_document_is() -> None:
    printed = repr(scanner(3310))

    assert printed == "ClamdScanner(endpoint=127.0.0.1:3310)"
    assert payload_markers() not in printed.encode("utf-8", "replace")


# --- the switch -----------------------------------------------------------
def test_no_scanner_is_configured_by_default() -> None:
    """``none`` is the default, and it means unexamined rather than clean."""
    default = Settings(app_env="test", log_json=False)

    assert default.virus_scan_transport == "none"
    assert build_scanner(default) is None


def test_the_scanner_the_settings_name_is_the_one_that_is_built() -> None:
    with FakeClamd(CLEAN_REPLY) as daemon:
        built = build_scanner(scan_settings(clamd_port=daemon.port))
        assert isinstance(built, ClamdScanner)
        built.scan(PAYLOAD)

    # If this is empty the built scanner never spoke to the configured port.
    assert daemon.payloads == [PAYLOAD]


def test_a_scanner_switch_with_no_address_is_refused_at_start_up() -> None:
    with pytest.raises(ValueError, match="CLAMD_HOST must be set"):
        scan_settings(clamd_host="   ")


def test_a_public_scanner_address_is_refused_at_start_up() -> None:
    """The protocol has no TLS, so a public daemon is a document leak."""
    with pytest.raises(ValueError, match="must not be a public address"):
        scan_settings(clamd_host="8.8.8.8")


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "192.168.0.5", "clamd", "localhost"])
def test_a_loopback_private_or_named_address_is_permitted(host: str) -> None:
    """Only a literal public address is refused; a name is not resolved at boot."""
    assert scan_settings(clamd_host=host).clamd_host == host


def test_a_public_address_is_permitted_when_no_scanner_is_configured() -> None:
    """The refusal follows the switch: an unused setting cannot leak anything."""
    configured = Settings(app_env="test", log_json=False, clamd_host="8.8.8.8")

    assert configured.clamd_host == "8.8.8.8"


# --- the route ------------------------------------------------------------
def test_an_infected_document_never_reaches_the_disk(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    root = tmp_path / "documents"
    with (
        FakeClamd(INFECTED_REPLY) as daemon,
        make_client(
            settings,
            session_factory,
            encryption_key=Fernet.generate_key().decode("ascii"),
            store_root=root,
            scanning=daemon,
        ) as client,
    ):
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "DOCUMENT_INFECTED"
    assert error["details"] == {"scanner": "clamd"}
    # Nothing stored, no row, no event: the refusal happened before the store.
    assert not blobs(root)
    assert contract_rows(session_factory) == []
    assert outbox_rows(session_factory) == []
    # And the document really was examined — the scan is not a skipped step.
    assert daemon.payloads == [PAYLOAD]


def test_a_clean_document_is_stored_after_the_scanner_approves_it(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    root = tmp_path / "documents"
    with (
        FakeClamd(CLEAN_REPLY) as daemon,
        make_client(
            settings,
            session_factory,
            encryption_key=Fernet.generate_key().decode("ascii"),
            store_root=root,
            scanning=daemon,
        ) as client,
    ):
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 201, response.text
    assert response.json()["sha256"] == document_digest(PAYLOAD)
    assert len(blobs(root)) == 1
    assert daemon.payloads == [PAYLOAD]


def test_an_unreachable_scanner_refuses_the_upload_and_stores_nothing(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """Fails closed: no verdict means no document, even though the daemon is configured."""
    root = tmp_path / "documents"
    with make_client(
        settings,
        session_factory,
        encryption_key=Fernet.generate_key().decode("ascii"),
        store_root=root,
        port=closed_port(),
    ) as client:
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DOCUMENT_SCAN_UNAVAILABLE"
    assert not blobs(root)
    assert contract_rows(session_factory) == []


def test_something_that_is_not_the_daemon_is_a_bad_gateway(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """Distinct from the 503 above, because the fix is a version, not a restart."""
    root = tmp_path / "documents"
    with (
        FakeClamd(UNREADABLE_REPLY) as daemon,
        make_client(
            settings,
            session_factory,
            encryption_key=Fernet.generate_key().decode("ascii"),
            store_root=root,
            scanning=daemon,
        ) as client,
    ):
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "DOCUMENT_SCAN_UNREADABLE"
    assert not blobs(root)


def test_no_scanner_configured_stores_the_document_unexamined(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """The ``none`` branch is a deliberate absence, not a silent approval."""
    root = tmp_path / "documents"
    with make_client(
        settings,
        session_factory,
        encryption_key=Fernet.generate_key().decode("ascii"),
        store_root=root,
    ) as client:
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 201, response.text
    assert len(blobs(root)) == 1


def test_a_document_already_stored_is_a_conflict_without_a_second_scan(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """The duplicate check runs first, so a resend pays for no scan."""
    root = tmp_path / "documents"
    with (
        FakeClamd(CLEAN_REPLY) as daemon,
        make_client(
            settings,
            session_factory,
            encryption_key=Fernet.generate_key().decode("ascii"),
            store_root=root,
            scanning=daemon,
        ) as client,
    ):
        headers = auth(client, tenant, make_user)
        first = upload(client, headers)
        second = upload(client, headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    # One conversation: the 409 was decided from the row, not by re-reading bytes.
    assert daemon.connections == 1
    assert len(blobs(root)) == 1


def test_the_route_asks_the_scanner_the_dependency_resolves(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """A substituted scanner is honoured, which is what makes the tests above possible."""

    class RefusingScanner:
        def __init__(self) -> None:
            self.seen: list[bytes] = []

        def scan(self, payload: bytes) -> None:
            self.seen.append(payload)
            raise DocumentScanUnavailableError("the operator's scanner said no")

    substitute = RefusingScanner()
    app = create_app(
        settings.model_copy(
            update={
                "encryption_key": Fernet.generate_key().decode("ascii"),
                "document_store_path": str(tmp_path / "documents"),
                "document_max_bytes": ONE_MIB,
            }
        ),
        session_factory=session_factory,
    )
    app.dependency_overrides[document_scanner] = lambda: substitute

    with TestClient(app) as client:
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DOCUMENT_SCAN_UNAVAILABLE"
    assert substitute.seen == [PAYLOAD]


def test_an_unforeseen_scanner_refusal_is_still_a_refusal(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """The base class keeps somebody else's scanner out of the 500 branch.

    ``ClamdScanner`` raises one of the three specific subclasses, so the route's
    ``except DocumentScanError`` is only reachable by a scanner this build did not
    write. It is worth pinning anyway: the alternative to a mapped refusal is a
    handler that stores nothing *and* tells the caller nothing, which reads as a
    server fault rather than as a document that was refused.
    """

    class OpaqueScanner:
        def __init__(self) -> None:
            self.seen: list[bytes] = []

        def scan(self, payload: bytes) -> None:
            self.seen.append(payload)
            raise DocumentScanError("the scanner refused without saying which way")

    substitute = OpaqueScanner()
    app = create_app(
        settings.model_copy(
            update={
                "encryption_key": Fernet.generate_key().decode("ascii"),
                "document_store_path": str(tmp_path / "documents"),
                "document_max_bytes": ONE_MIB,
            }
        ),
        session_factory=session_factory,
    )
    app.dependency_overrides[document_scanner] = lambda: substitute

    root = tmp_path / "documents"
    with TestClient(app) as client:
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DOCUMENT_SCAN_FAILED"
    # The scanner really was asked, so this is a refusal and not a skipped step.
    assert substitute.seen == [PAYLOAD]
    # A refusal is not an acceptance: nothing sealed, no row, no event.
    assert not blobs(root)
    assert contract_rows(session_factory) == []
    assert outbox_rows(session_factory) == []
    # The scanner's own sentence is the caller's only actionable detail, and the
    # document must not travel out with it.
    assert "refused without saying which way" in response.text
    assert PAYLOAD not in response.content
