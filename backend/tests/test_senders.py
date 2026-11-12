"""Tests for the SMTP notification transport (F10).

The adapter is exercised against a fake SMTP client, the same way
``test_publishers.py`` exercises ``KafkaPublisher`` against a fake producer: no
relay, no network and no credentials are needed to pin the behaviour that matters.
What is asserted here is the *envelope* — who the message goes to, what identifies
it, that the conversation is bounded and closed, and what a relay refusal is
allowed to say afterwards.

The property that gets the most attention is the last one. A relay's refusal quotes
the failing address, and that text is stored on ``Notification.last_error``, which
``GET /notifications`` renders to any viewer. An error column must not become a
second place a contact value leaks out of.
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core.config import Settings
from app.core.logging import MASK
from app.db.enums import Channel
from app.workers.dispatch import (
    MessageSender,
    OutgoingMessage,
    SenderError,
    UnavailableSender,
)
from app.workers.senders import (
    SUBJECT,
    RoutingSender,
    SmtpSender,
    build_sender,
    build_ssl_context,
    create_smtp_sender,
    open_smtp_client,
)

#: The approved contact value. A send must reach it, and no error text may repeat it.
CONTACT_EMAIL = "a.rahman@example.test"
SENDER_ADDRESS = "reminders@example.com"
IDEMPOTENCY_KEY = "17:T-60:2026-03-01"


# --- doubles ---------------------------------------------------------------
class FakeClient:
    """One SMTP conversation, recording the order of what was asked of it."""

    def __init__(self, relay: FakeRelay) -> None:
        self._relay = relay

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
        self._relay.calls.append("starttls")
        # Kept so a test can assert what the client was asked to verify, rather
        # than trusting that an argument was passed somewhere.
        self._relay.tls_contexts.append(context)
        return (220, b"ready to start TLS")

    def login(self, user: str, password: str) -> tuple[int, bytes] | None:
        # The password is recorded as a flag, never as a value: a test double that
        # stores it is a second place a credential could be printed on failure.
        self._relay.calls.append(f"login:{user}:{bool(password)}")
        return (235, b"authentication successful")

    def send_message(self, msg: EmailMessage) -> dict[str, tuple[int, bytes]]:
        self._relay.calls.append("send")
        self._relay.messages.append(msg)
        if self._relay.failure is not None:
            raise self._relay.failure
        return {}

    def __enter__(self) -> FakeClient:
        self._relay.calls.append("enter")
        return self

    def __exit__(self, *args: object) -> bool | None:
        self._relay.calls.append("exit")
        return None


class FakeRelay:
    """A transport factory that hands out :class:`FakeClient` conversations."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self.messages: list[EmailMessage] = []
        self.tls_contexts: list[ssl.SSLContext | None] = []
        self.failure = failure

    def __call__(self) -> FakeClient:
        return FakeClient(self)


# --- helpers ---------------------------------------------------------------
def smtp_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "log_json": False,
        "notification_transport": "smtp",
        "smtp_host": "relay.example.com",
        "smtp_from": SENDER_ADDRESS,
    }
    values.update(overrides)
    return Settings(**values)


def make_sender(relay: FakeRelay, **overrides: Any) -> SmtpSender:
    """A sender wired the way ``create_smtp_sender`` wires it, over a fake relay."""
    settings = smtp_settings(**overrides)
    return SmtpSender(
        relay,
        sender_address=settings.smtp_from,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
    )


def message(channel: Channel = Channel.EMAIL, **overrides: Any) -> OutgoingMessage:
    values: dict[str, Any] = {
        "channel": channel,
        "template": "T-60",
        "contract_id": 17,
        "idempotency_key": IDEMPOTENCY_KEY,
        "recipient": CONTACT_EMAIL,
    }
    values.update(overrides)
    return OutgoingMessage(**values)


# --- selection -------------------------------------------------------------
def test_the_default_transport_refuses_and_names_the_setting_it_read() -> None:
    sender: MessageSender = build_sender(Settings(app_env="test", log_json=False))

    assert isinstance(sender, UnavailableSender)
    # No channels rather than "a transport is off": that is the fact the policy asks
    # about, and it is what makes every channel refuse before a recipient is read.
    assert sender.channels == frozenset()
    with pytest.raises(SenderError) as raised:
        sender.send(message())
    # The operator reading a 409 or a stored error learns which value to change,
    # rather than only that nothing is configured.
    assert "NOTIFICATION_TRANSPORT=none" in str(raised.value)


def test_the_smtp_setting_selects_the_smtp_sender() -> None:
    sender = build_sender(smtp_settings())

    assert isinstance(sender, RoutingSender)
    # The router is the composition root now that the channels have separate
    # switches: a relay with no gateway must still deliver e-mail.
    assert sender.channels == frozenset({Channel.EMAIL})


def test_building_the_sender_opens_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection belongs to a send, not to start-up.

    A sender that dialled the relay when it was constructed would hold a socket
    open from process start, and would fail the API process on boot when the relay
    is merely restarting.
    """

    def refuse(**kwargs: object) -> object:
        raise AssertionError("no connection may be opened while building the sender")

    monkeypatch.setattr(smtplib, "SMTP", refuse)

    build_sender(smtp_settings())


# --- the envelope ----------------------------------------------------------
def test_a_send_delivers_one_message_to_the_approved_recipient() -> None:
    relay = FakeRelay()

    provider_id = make_sender(relay).send(message())

    assert len(relay.messages) == 1
    delivered = relay.messages[0]
    assert delivered["To"] == CONTACT_EMAIL
    assert delivered["From"] == SENDER_ADDRESS
    assert delivered["Subject"] == SUBJECT
    # The transport returns the Message-ID it minted, which is what a bounce
    # quotes; SMTP itself assigns no id.
    assert provider_id == delivered["Message-ID"]
    assert provider_id is not None
    assert provider_id.endswith("@example.com>")


def test_the_body_names_the_contract_but_carries_no_extracted_value() -> None:
    relay = FakeRelay()

    make_sender(relay).send(message())

    body = relay.messages[0].get_content()
    assert "17" in body
    # Nothing that F9/F10 read off the contract travels in the message: the value,
    # the counterparty and the dates all stay in the database.
    assert CONTACT_EMAIL not in body


def test_the_idempotency_key_travels_so_a_provider_can_dedupe() -> None:
    relay = FakeRelay()

    make_sender(relay).send(message())

    assert relay.messages[0]["X-Idempotency-Key"] == IDEMPOTENCY_KEY


# --- the Message-ID --------------------------------------------------------
def test_the_message_id_is_derived_from_the_slot_not_from_a_clock() -> None:
    """A re-send must be identical in the field a provider de-duplicates on.

    Delivery is at-least-once: the row commits after the transport returns, so a
    crash in between sends the same message again on the next tick. A randomised
    Message-ID would make the second attempt a *different message* to every relay
    that de-duplicates on that header, which is the one duplicate this transport
    can make collapsible.
    """
    relay = FakeRelay()
    sender = make_sender(relay)

    first = sender.send(message())
    second = sender.send(message())

    assert first == second


def test_two_slots_do_not_share_a_message_id() -> None:
    relay = FakeRelay()
    sender = make_sender(relay)

    first = sender.send(message())
    second = sender.send(message(idempotency_key="17:T-30:2026-03-01"))

    assert first != second


# --- channel capability ----------------------------------------------------
@pytest.mark.parametrize("channel", [Channel.SMS, Channel.VOICE])
def test_a_channel_this_transport_cannot_deliver_is_refused(channel: Channel) -> None:
    relay = FakeRelay()

    with pytest.raises(SenderError) as raised:
        make_sender(relay).send(message(channel=channel))

    # Refused, not substituted: delivering a voice reminder by e-mail would be a
    # different promise kept than the one the decision recorded.
    assert channel.value in str(raised.value)
    assert relay.calls == []


# --- failure handling ------------------------------------------------------
def test_a_relay_refusal_is_a_sender_error_so_the_attempt_is_recorded() -> None:
    relay = FakeRelay(smtplib.SMTPResponseException(451, b"4.3.0 try again later"))

    with pytest.raises(SenderError):
        make_sender(relay).send(message())


def test_a_refusal_keeps_the_smtp_status_code() -> None:
    relay = FakeRelay(smtplib.SMTPResponseException(451, b"4.3.0 try again later"))

    with pytest.raises(SenderError) as raised:
        make_sender(relay).send(message())

    assert "(451)" in str(raised.value)


def test_a_refusal_never_repeats_the_recipient_address() -> None:
    """The redaction that matters: this text is stored and then served over HTTP.

    A relay's refusal routinely quotes the address that failed. ``last_error`` is
    rendered by ``GET /notifications`` to a viewer who may not be allowed to see
    contact values anywhere else, so the relay's own words are scrubbed before
    they are kept.
    """
    relay = FakeRelay(
        smtplib.SMTPResponseException(
            550, f"5.1.1 <{CONTACT_EMAIL}>: recipient address rejected".encode()
        )
    )

    with pytest.raises(SenderError) as raised:
        make_sender(relay).send(message())

    stored = str(raised.value)
    assert CONTACT_EMAIL not in stored
    assert MASK in stored
    # The diagnostic the operator needs survives the redaction.
    assert "550" in stored
    assert "recipient address rejected" in stored


def test_a_connection_failure_is_a_sender_error() -> None:
    """A timeout must not escape as ``OSError`` past the row it belongs to."""

    relay = FakeRelay(TimeoutError("timed out"))

    with pytest.raises(SenderError):
        make_sender(relay).send(message())


def test_an_empty_relay_message_still_produces_an_error() -> None:
    relay = FakeRelay(smtplib.SMTPServerDisconnected())

    with pytest.raises(SenderError) as raised:
        make_sender(relay).send(message())

    assert "SMTP delivery failed" in str(raised.value)


def test_the_conversation_is_closed_even_when_delivery_fails() -> None:
    relay = FakeRelay(smtplib.SMTPResponseException(550, b"rejected"))

    with pytest.raises(SenderError):
        make_sender(relay).send(message())

    assert relay.calls[-1] == "exit"


# --- TLS and authentication ------------------------------------------------
def test_tls_is_negotiated_before_the_credentials_are_offered() -> None:
    relay = FakeRelay()

    make_sender(relay, smtp_username="mailer", smtp_password="hunter2").send(message())

    assert relay.calls == ["enter", "starttls", "login:mailer:True", "send", "exit"]


def test_nothing_is_authenticated_without_credentials() -> None:
    relay = FakeRelay()

    make_sender(relay).send(message())

    assert relay.calls == ["enter", "starttls", "send", "exit"]


def test_starttls_is_skipped_when_the_relay_does_not_offer_it() -> None:
    """Development relays (MailHog, Mailpit) have no certificate to present."""
    relay = FakeRelay()

    make_sender(relay, smtp_starttls=False).send(message())

    assert "starttls" not in relay.calls
    assert relay.tls_contexts == []


def test_the_relay_is_asked_to_prove_itself_not_merely_to_encrypt() -> None:
    """The context handed to ``starttls`` verifies, because smtplib's does not.

    ``SMTP.starttls()`` with no ``context`` builds ``ssl._create_stdlib_context()``
    — ``verify_mode=CERT_NONE``, ``check_hostname=False``. The session would be
    encrypted to whatever answered the socket, and the credentials would go with
    it. Asserting on the flags rather than on "a context was passed" is deliberate:
    a context built with ``CERT_NONE`` would satisfy the weaker claim.
    """
    relay = FakeRelay()

    make_sender(relay).send(message())

    assert len(relay.tls_contexts) == 1
    context = relay.tls_contexts[0]
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_sender_never_falls_back_to_an_unverified_context() -> None:
    """``ssl_context=None`` means "system trust store", not "do not verify".

    ``SmtpSender`` is constructed directly in several places, so the safe default
    has to live in the class rather than only in the settings-driven factory.
    """
    relay = FakeRelay()
    sender = SmtpSender(relay, sender_address=SENDER_ADDRESS)

    sender.send(message())

    context = relay.tls_contexts[0]
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED


def _ca_pem(common_name: str = "internal-ca.test") -> str:
    """A real, throwaway certificate to name as the CA bundle.

    Generated rather than stubbed: ``ssl`` refuses to build a context from an empty
    PEM file outright (``no certificate or crl found``), so a placeholder would fail
    here for a reason unrelated to what is under test. Committing one is not an
    option either — a PEM carrying a private key is exactly what the ``gitleaks``
    hook is for.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def test_a_configured_ca_bundle_builds_a_verifying_context(tmp_path: Path) -> None:
    """``SMTP_CA_BUNDLE`` replaces the trust store; it never disables checking.

    The socket suite is what proves a bundle changes what is *accepted*. This is
    the half a unit test can see: naming a bundle does not weaken the context.
    """
    bundle = tmp_path / "internal-ca.pem"
    bundle.write_text(_ca_pem(), encoding="utf-8")

    context = build_ssl_context(smtp_settings(smtp_ca_bundle=str(bundle)))

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_bundle_that_ssl_cannot_load_is_a_boot_error(tmp_path: Path) -> None:
    """Existing is not the same as loadable, and the difference is a boot error.

    An empty file passes ``is_file`` and then makes ``ssl`` raise when the context
    is built. The context is built on the *send* path, inside the row lock, so a
    bundle that cannot be parsed would surface as a relay fault on the first
    message instead of as a start-up failure. ``Settings`` therefore loads it.
    """
    bundle = tmp_path / "internal-ca.pem"
    bundle.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="SMTP_CA_BUNDLE must be a PEM file"):
        smtp_settings(smtp_ca_bundle=str(bundle))


# --- the socket bound ------------------------------------------------------
def test_the_configured_credentials_reach_the_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_smtp_sender`` is the only place settings become a sender.

    Driven end to end through the factory so a setting that is read in the wrong
    place — a username that never reaches ``login``, ``starttls`` ignored — is
    caught here rather than by the first real deployment.
    """
    relay = FakeRelay()
    # The patched factory must hand back a *conversation* (a client), not the
    # relay: ``create_smtp_sender`` passes it to ``SmtpSender`` as ``connect``,
    # whose result is used as a context manager. Calling the relay is what a
    # real ``open_smtp_client`` does with settings.
    monkeypatch.setattr("app.workers.senders.open_smtp_client", lambda _settings: relay())

    sender = create_smtp_sender(smtp_settings(smtp_username="mailer", smtp_password="hunter2"))
    sender.send(message())

    assert relay.calls == ["enter", "starttls", "login:mailer:True", "send", "exit"]


def test_the_relay_connection_is_bounded_by_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``smtplib`` applies this to the socket, so it bounds every later step too.

    The bound is a correctness requirement rather than tuning: the scheduler sends
    while holding the row's ``FOR UPDATE`` lock, so a relay that accepts the
    connection and then says nothing must not be able to hold that row open.
    """
    seen: dict[str, object] = {}

    def fake_smtp(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)

    open_smtp_client(smtp_settings(smtp_timeout_seconds=2.5))

    assert seen == {"host": "relay.example.com", "port": 587, "timeout": 2.5}


# --- settings --------------------------------------------------------------
def test_settings_refuse_smtp_without_a_host() -> None:
    with pytest.raises(ValueError, match="SMTP_HOST"):
        smtp_settings(smtp_host="   ")


def test_settings_refuse_a_sender_that_is_not_an_address() -> None:
    with pytest.raises(ValueError, match="SMTP_FROM"):
        smtp_settings(smtp_from="Reminder Bot")


def test_settings_refuse_a_display_name_as_the_sender() -> None:
    """The common paste: ``Ops Team <ops@example.com>`` cannot be a MAIL FROM."""
    with pytest.raises(ValueError, match="SMTP_FROM"):
        smtp_settings(smtp_from="Ops Team <ops@example.com>")


def test_settings_refuse_half_a_credential() -> None:
    with pytest.raises(ValueError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        smtp_settings(smtp_username="mailer")


def test_settings_accept_a_credential_pair() -> None:
    settings = smtp_settings(smtp_username="mailer", smtp_password="hunter2")

    assert settings.smtp_username == "mailer"


def test_settings_refuse_a_ca_bundle_that_is_not_there(tmp_path: Path) -> None:
    """A mistyped path must be a boot error, not a failure inside the row lock."""
    with pytest.raises(ValueError, match="SMTP_CA_BUNDLE"):
        smtp_settings(smtp_ca_bundle=str(tmp_path / "no-such-bundle.pem"))


def test_settings_refuse_a_ca_bundle_without_tls(tmp_path: Path) -> None:
    """Trusting a CA that is never consulted reads as "TLS configured" and is not."""
    bundle = tmp_path / "internal-ca.pem"
    bundle.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="SMTP_CA_BUNDLE has no effect"):
        smtp_settings(smtp_ca_bundle=str(bundle), smtp_starttls=False)


def test_settings_ignore_the_smtp_block_when_the_transport_is_none() -> None:
    """``none`` must stay bootable with every SMTP value left blank."""
    settings = Settings(app_env="test", log_json=False)

    assert settings.notification_transport == "none"
    assert settings.smtp_host == ""


def test_production_refuses_smtp_without_tls() -> None:
    with pytest.raises(ValueError, match="SMTP_STARTTLS"):
        Settings(
            app_env="production",
            secret_key="a" * 40,
            cors_origins="https://app.example.com",
            notification_transport="smtp",
            smtp_host="relay.example.com",
            smtp_from=SENDER_ADDRESS,
            smtp_starttls=False,
        )


def test_development_may_skip_tls_for_a_local_relay() -> None:
    settings = smtp_settings(app_env="development", smtp_starttls=False)

    assert settings.smtp_starttls is False
