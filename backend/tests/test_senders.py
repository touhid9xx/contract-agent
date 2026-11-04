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
from email.message import EmailMessage
from typing import Any

import pytest

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
    SmtpSender,
    build_sender,
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

    def starttls(self) -> tuple[int, bytes]:
        self._relay.calls.append("starttls")
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
    assert sender.available is False
    with pytest.raises(SenderError) as raised:
        sender.send(message())
    # The operator reading a 409 or a stored error learns which value to change,
    # rather than only that nothing is configured.
    assert "NOTIFICATION_TRANSPORT=none" in str(raised.value)


def test_the_smtp_setting_selects_the_smtp_sender() -> None:
    sender = build_sender(smtp_settings())

    assert isinstance(sender, SmtpSender)
    assert sender.available is True


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
