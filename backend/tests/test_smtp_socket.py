"""The SMTP transport against a real socket (F10).

:mod:`tests.test_senders` pins the adapter's behaviour against a fake client, which
is enough to prove the *envelope* and the *call order*. It cannot prove that
``smtplib`` and the adapter together complete a conversation, because a fake client
never parses a greeting, never waits for a ``354`` and never sees a real ``550``
come back as an exception class of the relay's choosing. This module closes that
gap the way ``test_mysql_integration.py`` closes the equivalent one for the MySQL
lock: by talking to the real thing.

The relay here is a loopback SMTP server built from :mod:`socket` and
:mod:`threading` rather than a container or a new dependency — the point is that
the client speaks the protocol, not that a particular mail server is installed.
What it adds over the fake client:

* ``smtplib`` really parses the multiline ``EHLO`` reply and really waits for the
  ``354`` before the body goes out, so a message that is sent is a message a relay
  acknowledged rather than one a double agreed with.
* The ``550`` on ``RCPT TO`` arrives as :class:`smtplib.SMTPRecipientsRefused` —
  a *different* exception class from the ``SMTPResponseException`` the unit tests
  raise — and its text quotes the address it refused. That is the redaction path
  that matters, exercised through the exception a relay actually produces.
* ``SMTP_TIMEOUT_SECONDS`` is proven as a ceiling rather than as a keyword
  argument: a relay that accepts the connection and then says nothing fails the
  send in bounded time, which is what keeps a silent provider from holding a
  locked ``Notification`` row open (D10-09).
* STARTTLS is a real handshake over a real certificate, and the credentials are
  proven to cross only after it — not merely *called* in that order.

Two honest limits, stated rather than hidden. The relay is ours, so this proves
the client speaks SMTP to something that answers SMTP, not that it is compatible
with every provider's quirks. And the certificate is self-signed: ``smtplib``
negotiates TLS through an unverified context, so what is proven is the handshake
and the ordering, not authentication of the relay.
"""

from __future__ import annotations

import base64
import email
import re
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import IO, Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core.config import Settings
from app.core.logging import MASK
from app.db.enums import Channel
from app.workers.dispatch import OutgoingMessage, SenderError
from app.workers.senders import SUBJECT, create_smtp_sender

#: The approved contact value. A send must reach it, and no error text may repeat it.
CONTACT_EMAIL = "a.rahman@example.test"
SENDER_ADDRESS = "reminders@example.com"
IDEMPOTENCY_KEY = "17:T-60:2026-03-01"
PASSWORD = "hunter2"

_ADDRESS_RE = re.compile(r"<([^>]*)>")


# --- the relay -------------------------------------------------------------
class LoopbackRelay:
    """A one-socket-per-conversation SMTP server, in a thread.

    Implements exactly the commands ``smtplib`` issues for one message and records
    what it saw, so a test asserts on the wire rather than on the client's
    intentions. Not a mail server: there is no queue, no storage and no policy.
    """

    def __init__(
        self,
        *,
        tls_context: ssl.SSLContext | None = None,
        accepts_auth: bool = False,
        expected_password: str = PASSWORD,
    ) -> None:
        self.tls_context = tls_context
        self.accepts_auth = accepts_auth
        self.expected_password = expected_password
        # What the relay saw. ``exchanges`` pairs each command with whether the
        # socket was already encrypted when it arrived, which is how the
        # "credentials over TLS" claim is checked at the relay and not by the
        # client asserting its own good intentions.
        self.exchanges: list[tuple[bool, str]] = []
        self.mail_from: list[str] = []
        self.recipients: list[str] = []
        self.payloads: list[bytes] = []
        self.authenticated: list[str] = []
        self.password_matched = False
        self.rcpt_status = 250
        self.silent = False
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(5.0)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (OSError, TimeoutError):
                return
            with conn:
                try:
                    self._converse(conn)
                except (OSError, TimeoutError):
                    return

    # -- one conversation ---------------------------------------------------
    def _converse(self, conn: socket.socket) -> None:
        if self.silent:
            # Accept, then say nothing at all: the client must give up on its own
            # clock rather than on the relay's goodwill. Far longer than the
            # client's timeout, so a send that returns is returning because of the
            # setting and not because this server eventually hung up.
            self._stop.wait(30.0)
            return

        conn.settimeout(5.0)
        stream = conn.makefile("rb")
        self._reply(conn, "220 loopback.test ESMTP")
        encrypted = False
        while True:
            raw = stream.readline()
            if not raw:
                return
            command = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not command:
                continue
            self.exchanges.append((encrypted, command))
            verb = command.split(" ", 1)[0].upper()

            if verb in {"EHLO", "HELO"}:
                self._reply(conn, "250-loopback.test")
                if self.accepts_auth and encrypted:
                    self._reply(conn, "250-AUTH PLAIN LOGIN")
                if self.tls_context is not None and not encrypted:
                    self._reply(conn, "250-STARTTLS")
                self._reply(conn, "250 8BITMIME")
            elif verb == "STARTTLS":
                self._reply(conn, "220 2.0.0 ready to start TLS")
                assert self.tls_context is not None
                conn = self.tls_context.wrap_socket(conn, server_side=True)
                stream = conn.makefile("rb")
                encrypted = True
            elif verb == "AUTH":
                self._authenticate(conn, stream, command, encrypted=encrypted)
            elif verb == "MAIL":
                self.mail_from.append(_address(command))
                self._reply(conn, "250 2.1.0 ok")
            elif verb == "RCPT":
                address = _address(command)
                if self.rcpt_status == 250:
                    self.recipients.append(address)
                    self._reply(conn, "250 2.1.5 ok")
                else:
                    # Worded the way a real relay words it: the failing address is
                    # quoted back, which is the whole reason the adapter redacts.
                    self._reply(
                        conn,
                        f"{self.rcpt_status} 5.1.1 <{address}>: recipient address rejected",
                    )
            elif verb == "DATA":
                self._reply(conn, "354 end data with <CR><LF>.<CR><LF>")
                self.payloads.append(_read_body(stream))
                self._reply(conn, "250 2.0.0 queued as LOOPBACK1")
            elif verb == "QUIT":
                self._reply(conn, "221 2.0.0 bye")
                return
            else:
                self._reply(conn, "250 2.0.0 ok")

    def _authenticate(
        self,
        conn: socket.socket,
        stream: IO[bytes],
        command: str,
        *,
        encrypted: bool,
    ) -> None:
        if not encrypted:
            # A relay that accepted credentials in the clear would make the
            # "TLS before auth" claim untestable, so it refuses instead — and that
            # refusal is what the test asserts never arrives.
            self._reply(conn, "530 5.7.0 must issue a STARTTLS command first")
            return
        parts = command.split(" ")
        token = parts[2] if len(parts) > 2 else ""
        if not token:
            self._reply(conn, "334 ")
            token = stream.readline().decode("ascii", "replace").strip()
        user, password = _plain_credentials(token)
        # The user is kept; the password is only compared, never stored, so a
        # failing assertion cannot print a credential that also sits in Settings.
        self.authenticated.append(user)
        self.password_matched = password == self.expected_password
        self._reply(conn, "235 2.7.0 authentication successful")

    @staticmethod
    def _reply(conn: socket.socket, line: str) -> None:
        conn.sendall(f"{line}\r\n".encode())


def _address(command: str) -> str:
    """The address inside ``MAIL FROM:<...>`` or ``RCPT TO:<...>``."""
    match = _ADDRESS_RE.search(command)
    return match.group(1) if match else ""


def _read_body(stream: IO[bytes]) -> bytes:
    """Read a ``DATA`` payload up to the terminating bare dot."""
    chunks: list[bytes] = []
    while True:
        line = stream.readline()
        if not line or line in (b".\r\n", b".\n", b"."):
            return b"".join(chunks)
        chunks.append(line)


def _plain_credentials(token: str) -> tuple[str, str]:
    """Decode an ``AUTH PLAIN`` initial response: ``NUL user NUL password``."""
    parts = [part.decode("utf-8", "replace") for part in base64.b64decode(token).split(b"\0")]
    if len(parts) < 3:
        return (parts[0] if parts else "", "")
    return (parts[1], parts[2])


# --- the certificate for the TLS conversation ------------------------------
@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway certificate for the loopback relay.

    Generated rather than committed: a checked-in private key is exactly what the
    ``gitleaks`` hook exists to catch, and a test certificate gains nothing by
    being stable. Generated once per module, so the RSA key is paid for once.
    """
    directory = tmp_path_factory.mktemp("smtp-tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loopback.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("loopback.test")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "loopback.pem"
    key_path = directory / "loopback.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return (cert_path, key_path)


# --- fixtures --------------------------------------------------------------
def start_relay(**kwargs: Any) -> LoopbackRelay:
    """Start a relay, skipping rather than failing where loopback is unavailable."""
    try:
        return LoopbackRelay(**kwargs)
    except OSError as exc:  # pragma: no cover - only on hosts without a loopback stack
        pytest.skip(f"loopback sockets are unavailable here: {exc}")


@pytest.fixture
def plaintext_relay() -> Iterator[LoopbackRelay]:
    """A loopback relay with no TLS, reachable on 127.0.0.1."""
    relay = start_relay()
    try:
        yield relay
    finally:
        relay.close()


@pytest.fixture
def tls_relay(certificate: tuple[Path, Path]) -> Iterator[LoopbackRelay]:
    """A loopback relay that offers STARTTLS, and AUTH once the socket is encrypted."""
    cert_path, key_path = certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    relay = start_relay(tls_context=context, accepts_auth=True)
    try:
        yield relay
    finally:
        relay.close()


def settings_for(relay: LoopbackRelay, **overrides: Any) -> Settings:
    """Settings pointed at the relay, so the whole settings → socket path is real."""
    values: dict[str, Any] = {
        "app_env": "test",
        "log_json": False,
        "notification_transport": "smtp",
        "smtp_host": "127.0.0.1",
        "smtp_port": relay.port,
        "smtp_from": SENDER_ADDRESS,
        "smtp_starttls": False,
        "smtp_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return Settings(**values)


def message(**overrides: Any) -> OutgoingMessage:
    values: dict[str, Any] = {
        "channel": Channel.EMAIL,
        "template": "T-60",
        "contract_id": 17,
        "idempotency_key": IDEMPOTENCY_KEY,
        "recipient": CONTACT_EMAIL,
    }
    values.update(overrides)
    return OutgoingMessage(**values)


def parse(payload: bytes) -> Message:
    return email.message_from_bytes(payload)


# --- the conversation ------------------------------------------------------
def test_a_send_completes_a_real_conversation(plaintext_relay: LoopbackRelay) -> None:
    """The body goes out only after the relay said ``354``, not before.

    A fake client cannot show this: what matters is the client waiting for the
    relay's go-ahead, which only exists when the relay is a socket.
    """
    sender = create_smtp_sender(settings_for(plaintext_relay))

    sender.send(message())

    verbs = [command.split(" ", 1)[0].upper() for _, command in plaintext_relay.exchanges]
    assert verbs == ["EHLO", "MAIL", "RCPT", "DATA", "QUIT"]
    # One message reached one recipient, and the envelope is the approved contact.
    assert plaintext_relay.mail_from == [SENDER_ADDRESS]
    assert plaintext_relay.recipients == [CONTACT_EMAIL]
    assert len(plaintext_relay.payloads) == 1


def test_the_headers_on_the_wire_are_the_ones_the_sender_reported(
    plaintext_relay: LoopbackRelay,
) -> None:
    sender = create_smtp_sender(settings_for(plaintext_relay))

    reported = sender.send(message())

    delivered = parse(plaintext_relay.payloads[0])
    assert delivered["To"] == CONTACT_EMAIL
    assert delivered["From"] == SENDER_ADDRESS
    assert delivered["Subject"] == SUBJECT
    # The id the transport returns is the id on the wire, which is what a bounce
    # or a provider's de-duplication would quote back.
    assert delivered["Message-ID"] == reported
    assert delivered["X-Idempotency-Key"] == IDEMPOTENCY_KEY


def test_a_resend_is_byte_identical_on_the_wire(plaintext_relay: LoopbackRelay) -> None:
    """At-least-once is only survivable if the second copy is recognisable.

    The row commits after ``send`` returns, so a crash in between sends the same
    message again. A randomised Message-ID would make attempt two a different
    message to every relay that de-duplicates on that header; the derived one makes
    the bytes identical.
    """
    sender = create_smtp_sender(settings_for(plaintext_relay))

    first = sender.send(message())
    second = sender.send(message())

    assert first == second
    assert len(plaintext_relay.payloads) == 2
    assert plaintext_relay.payloads[0] == plaintext_relay.payloads[1]


def test_a_relay_refusal_over_the_wire_is_redacted_before_it_is_stored(
    plaintext_relay: LoopbackRelay,
) -> None:
    """The refusal class a real relay produces is the one the redaction must cover.

    ``smtplib`` raises :class:`smtplib.SMTPRecipientsRefused` for a refused
    ``RCPT``, whose text is the relay's own words with the address quoted inside
    them. That text becomes ``Notification.last_error``, which ``GET
    /notifications`` renders to a viewer who may not see contact values anywhere
    else.
    """
    plaintext_relay.rcpt_status = 550

    with pytest.raises(SenderError) as raised:
        create_smtp_sender(settings_for(plaintext_relay)).send(message())

    stored = str(raised.value)
    assert CONTACT_EMAIL not in stored
    assert MASK in stored
    # The diagnostic an operator needs survives: which code, and what it said.
    assert "550" in stored
    assert "recipient address rejected" in stored
    # Nothing was transmitted past the refused recipient.
    assert plaintext_relay.payloads == []


def test_a_silent_relay_cannot_hold_the_row_open() -> None:
    """The timeout is a ceiling on the socket, not a keyword argument.

    Dispatch happens inside the transaction holding the row's ``FOR UPDATE`` lock
    and the scheduler tick behind it is serial, so a provider that accepts the
    connection and then says nothing must fail the send in bounded time rather than
    hold a locked row — and every row behind it — open indefinitely.
    """
    relay = start_relay()
    relay.silent = True
    try:
        sender = create_smtp_sender(settings_for(relay, smtp_timeout_seconds=0.5))

        started = time.monotonic()
        with pytest.raises(SenderError):
            sender.send(message())
        elapsed = time.monotonic() - started

        # Generous against the 0.5s setting so a loaded host does not flake, and
        # far below the relay's own 30s silence — so this passes only because the
        # socket timeout is real. Remove the setting and this test hangs instead.
        assert elapsed < 2.0
        assert relay.payloads == []
    finally:
        relay.close()


# --- TLS and credentials ---------------------------------------------------
def test_the_credentials_cross_only_an_encrypted_socket(tls_relay: LoopbackRelay) -> None:
    """TLS before auth, proven at the relay rather than at the call site.

    The relay records for every command whether the socket was already encrypted,
    and refuses ``AUTH`` on an unencrypted one, so this cannot pass by the client
    merely *calling* ``starttls`` first: a handshake has to have completed.
    """
    sender = create_smtp_sender(
        settings_for(tls_relay, smtp_starttls=True, smtp_username="mailer", smtp_password=PASSWORD)
    )

    sender.send(message())

    verbs = [command.split(" ", 1)[0].upper() for _, command in tls_relay.exchanges]
    # The handshake happened and the extension list was re-read afterwards: RFC 3207
    # requires the client to discard what it learned in the clear.
    assert verbs[:4] == ["EHLO", "STARTTLS", "EHLO", "AUTH"]
    # Nothing authentication-shaped ever travelled over the clear.
    auth_exchanges = [entry for entry in tls_relay.exchanges if entry[1].startswith("AUTH")]
    assert [encrypted for encrypted, _ in auth_exchanges] == [True]
    assert tls_relay.authenticated == ["mailer"]
    # The configured password really arrived — compared, never stored, so a failing
    # assertion cannot print a credential that also sits in a settings object.
    assert tls_relay.password_matched is True
    assert len(tls_relay.payloads) == 1


def test_a_relay_that_offers_no_tls_fails_rather_than_sending_in_the_clear(
    plaintext_relay: LoopbackRelay,
) -> None:
    """With ``SMTP_STARTTLS=true`` a cleartext relay is a failure, not a downgrade.

    Sending anyway would put customer contact details on the wire in a deployment
    configured to require TLS, so the send must fail and the row must park where an
    operator sees it.
    """
    sender = create_smtp_sender(settings_for(plaintext_relay, smtp_starttls=True))

    with pytest.raises(SenderError) as raised:
        sender.send(message())

    assert "STARTTLS" in str(raised.value)
    assert plaintext_relay.payloads == []
