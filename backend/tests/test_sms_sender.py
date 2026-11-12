"""Tests for the SMS transport and the per-channel routing in front of it.

The gateway adapter is exercised against a fake HTTP transport, the same way
``test_senders.py`` exercises the SMTP adapter against a fake client and
``test_publishers.py`` exercises the Kafka publisher against a fake producer: no
gateway account, no socket and no phone number belonging to anyone real.

Three properties get most of the attention here:

* the **recipient never leaves in an error**. ``Notification.last_error`` is
  rendered by ``GET /notifications`` to any viewer, and a gateway's refusal quotes
  the number it rejected. An error column must not become a second place a contact
  value leaks out of.
* the **API key never leaves in a repr**. It is a credential, and a log line is
  where a credential ends up by accident.
* the body carries **no opt-out promise**. Nothing this build ships reads a reply,
  so telling a customer to "reply STOP" would be a sentence the system cannot
  honour — see ``senders._SMS_BODY``.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest

from app.core.config import Settings
from app.db.enums import Channel
from app.workers.dispatch import (
    MessageSender,
    OutgoingMessage,
    SenderError,
    UnavailableSender,
)
from app.workers.senders import (
    RoutingSender,
    TextbeeSender,
    build_sender,
    create_textbee_sender,
)

#: The approved contact value. A send must reach it, and no error text may repeat it.
CONTACT_PHONE = "+8801700000000"
API_KEY = "tb_test_key_do_not_log"
DEVICE_ID = "device-123"

MINIMAL_ENV = {
    "app_env": "test",
    "log_json": False,
    "sms_transport": "textbee",
    "textbee_api_key": API_KEY,
    "textbee_device_id": DEVICE_ID,
}


def sms_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(MINIMAL_ENV)
    values.update(overrides)
    return Settings(**values)


def message(channel: Channel = Channel.SMS, recipient: str = CONTACT_PHONE) -> OutgoingMessage:
    return OutgoingMessage(
        channel=channel,
        template="T-30",
        contract_id=4242,
        idempotency_key="contract-4242-sms-T-30",
        recipient=recipient,
    )


class RecordingTransport:
    """A gateway that accepts everything and remembers the request it was given."""

    def __init__(self, reply: bytes = b'{"data": {"id": "msg-1"}}') -> None:
        self.reply = reply
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.reply

    # -- reading back what was sent ------------------------------------
    def headers(self, index: int = 0) -> dict[str, str]:
        """The request headers, lower-cased so a comparison cannot depend on case."""
        return {key.lower(): value for key, value in self.requests[index].header_items()}

    def payload(self, index: int = 0) -> dict[str, Any]:
        raw = self.requests[index].data
        # ``Request.data`` is typed as a union because a caller may hand it a file
        # object; this adapter only ever sends a serialised dict, so narrowing keeps
        # the read honest instead of casting the union away.
        if not isinstance(raw, bytes):
            raise AssertionError(f"the body was {type(raw).__name__}, not bytes")
        parsed: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return parsed

    def url(self, index: int = 0) -> str:
        return self.requests[index].full_url


class RefusingTransport:
    """A gateway that answered, and refused."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        raise self._error


def sender(transport: Any = None, **overrides: Any) -> TextbeeSender:
    return TextbeeSender(
        transport if transport is not None else RecordingTransport(),
        api_key=overrides.get("api_key", API_KEY),
        device_id=overrides.get("device_id", DEVICE_ID),
        base_url=overrides.get("base_url", "https://api.textbee.dev"),
        timeout_seconds=overrides.get("timeout_seconds", 10.0),
    )


# --- the request -----------------------------------------------------------
def test_the_request_goes_to_the_device_route_with_the_key_in_a_header() -> None:
    transport = RecordingTransport()
    textbee = sender(transport)

    textbee.send(message())

    assert transport.url() == (
        f"https://api.textbee.dev/api/v1/gateway/devices/{DEVICE_ID}/send-sms"
    )
    headers = transport.headers()
    assert headers["x-api-key"] == API_KEY
    assert headers["content-type"] == "application/json"
    assert transport.requests[0].get_method() == "POST"


def test_the_recipient_and_a_value_free_body_are_what_cross_the_wire() -> None:
    transport = RecordingTransport()

    sender(transport).send(message())

    payload = transport.payload()
    assert payload["recipients"] == [CONTACT_PHONE]
    body = payload["message"]
    # The reference is what makes the message actionable; no extracted value rides
    # along, for the same reason the e-mail body carries none.
    assert "4242" in body
    # And no promise this build cannot keep.
    assert "STOP" not in body.upper()


def test_the_send_is_bounded_by_the_configured_timeout() -> None:
    transport = RecordingTransport()

    sender(transport, timeout_seconds=3.5).send(message())

    assert transport.timeouts == [3.5]


def test_the_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    transport = RecordingTransport()

    sender(transport, base_url="https://api.textbee.dev/").send(message())

    assert "//api/v1" not in transport.url()


# --- the reply -------------------------------------------------------------
def test_the_gateway_id_is_returned_when_it_names_one() -> None:
    assert sender().send(message()) == "msg-1"


def test_an_unreadable_reply_is_not_a_failure() -> None:
    """The call returned normally, so the message was accepted.

    Raising here would turn an accepted send into a failed attempt, and the retry
    would be a second message to a real person.
    """
    for reply in (b"", b"not json", b"[]", b'{"data": {}}'):
        assert sender(RecordingTransport(reply)).send(message()) is None


# --- channel discipline ----------------------------------------------------
def test_the_transport_declares_sms_only() -> None:
    assert sender().channels == frozenset({Channel.SMS})


def test_it_refuses_a_channel_it_does_not_implement() -> None:
    with pytest.raises(SenderError) as raised:
        sender().send(message(channel=Channel.EMAIL))

    assert "email" in str(raised.value)


# --- what a failure is allowed to say --------------------------------------
def test_an_http_refusal_names_the_code_and_not_the_number() -> None:
    error = urllib.error.HTTPError(
        "https://api.textbee.dev/x",
        422,
        f"invalid recipient {CONTACT_PHONE}",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )

    with pytest.raises(SenderError) as raised:
        sender(RefusingTransport(error)).send(message())

    text = str(raised.value)
    assert "422" in text
    assert CONTACT_PHONE not in text


def test_a_transport_failure_names_the_cause_and_not_the_number() -> None:
    error = urllib.error.URLError(f"name resolution failed for {CONTACT_PHONE}")

    with pytest.raises(SenderError) as raised:
        sender(RefusingTransport(error)).send(message())

    text = str(raised.value)
    assert "URLError" in text
    assert CONTACT_PHONE not in text


def test_a_nested_cause_is_named_by_type_and_never_by_its_text() -> None:
    """``URLError`` keeps the interesting part in ``.reason``, which is often a string.

    The first version of this branch named ``.reason``'s own type, so a DNS failure
    was stored as ``"SMS delivery failed: str"`` — it named the wrapper's payload
    instead of the cause, and an operator reading ``Notification.last_error``
    learned nothing. The exception is named first and a nested exception's *type*
    is appended, so this text still cannot quote a host or a number.
    """
    error = urllib.error.URLError(socket.gaierror(f"name resolution failed for {CONTACT_PHONE}"))

    with pytest.raises(SenderError) as raised:
        sender(RefusingTransport(error)).send(message())

    text = str(raised.value)
    assert "URLError" in text
    assert "gaierror" in text
    assert CONTACT_PHONE not in text


def test_a_timeout_is_reported_as_a_sender_error() -> None:
    with pytest.raises(SenderError):
        sender(RefusingTransport(TimeoutError("timed out"))).send(message())


def test_the_repr_carries_no_credential() -> None:
    """A repr reaches log lines and tracebacks; the key must not ride along."""
    rendered = repr(sender())

    assert API_KEY not in rendered
    assert DEVICE_ID in rendered


# --- routing ---------------------------------------------------------------
class StubSender:
    """A transport that records which channel it was asked to deliver."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel
        self.sent: list[Channel] = []

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset({self._channel})

    def send(self, message: OutgoingMessage) -> str | None:
        self.sent.append(message.channel)
        return f"{self._channel.value}-id"


def test_the_router_declares_the_union_of_what_its_delegates_implement() -> None:
    router = RoutingSender(
        {Channel.EMAIL: StubSender(Channel.EMAIL), Channel.SMS: StubSender(Channel.SMS)}
    )

    assert router.channels == frozenset({Channel.EMAIL, Channel.SMS})


def test_the_router_hands_each_message_to_the_transport_for_its_channel() -> None:
    email = StubSender(Channel.EMAIL)
    sms = StubSender(Channel.SMS)
    router = RoutingSender({Channel.EMAIL: email, Channel.SMS: sms})

    assert router.send(message(Channel.EMAIL)) == "email-id"
    assert router.send(message(Channel.SMS)) == "sms-id"
    assert email.sent == [Channel.EMAIL]
    assert sms.sent == [Channel.SMS]


def test_the_router_raises_rather_than_reporting_a_send_that_never_happened() -> None:
    router = RoutingSender({Channel.EMAIL: StubSender(Channel.EMAIL)})

    with pytest.raises(SenderError) as raised:
        router.send(message(Channel.SMS))

    assert "sms" in str(raised.value)


# --- selection -------------------------------------------------------------
def test_no_switch_at_all_yields_the_stand_in_naming_both_settings() -> None:
    built: MessageSender = build_sender(Settings(app_env="test", log_json=False))

    assert isinstance(built, UnavailableSender)
    assert built.channels == frozenset()
    with pytest.raises(SenderError) as raised:
        built.send(message(Channel.EMAIL))
    # Naming one setting would send the operator to the wrong place.
    assert "NOTIFICATION_TRANSPORT=none" in str(raised.value)
    assert "SMS_TRANSPORT=none" in str(raised.value)


def test_the_sms_switch_alone_yields_a_router_that_delivers_sms_only() -> None:
    built = build_sender(sms_settings())

    assert isinstance(built, RoutingSender)
    assert built.channels == frozenset({Channel.SMS})
    # The e-mail channel must still refuse rather than borrow the gateway.
    with pytest.raises(SenderError):
        built.send(message(Channel.EMAIL))


def test_both_switches_compose_into_one_router() -> None:
    built = build_sender(
        sms_settings(
            notification_transport="smtp",
            smtp_host="relay.example.test",
            smtp_from="reminders@example.test",
        )
    )

    assert isinstance(built, RoutingSender)
    assert built.channels == frozenset({Channel.EMAIL, Channel.SMS})


def test_building_the_sms_sender_opens_no_connection() -> None:
    """The call belongs to a send, not to start-up: the API process must boot with
    a gateway that is down, and the scheduler must fail one row rather than the run."""
    built = create_textbee_sender(sms_settings())

    assert isinstance(built, TextbeeSender)


# --- configuration ---------------------------------------------------------
def test_sms_is_off_by_default() -> None:
    assert Settings(app_env="test", log_json=False).sms_transport == "none"


def test_textbee_without_a_key_is_refused_at_start_up() -> None:
    with pytest.raises(ValueError, match="TEXTBEE_API_KEY"):
        sms_settings(textbee_api_key="")


def test_textbee_without_a_device_is_refused_at_start_up() -> None:
    with pytest.raises(ValueError, match="TEXTBEE_DEVICE_ID"):
        sms_settings(textbee_device_id="  ")


def test_a_plaintext_remote_gateway_is_refused() -> None:
    """Stricter than the Ollama rule on purpose: this request carries a phone
    number in the body and a credential in a header."""
    with pytest.raises(ValueError, match="must be https"):
        sms_settings(textbee_base_url="http://api.textbee.dev")


def test_a_loopback_gateway_may_stay_plaintext() -> None:
    assert sms_settings(textbee_base_url="http://127.0.0.1:8080").sms_transport == "textbee"


def test_a_gateway_url_with_no_host_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute URL"):
        sms_settings(textbee_base_url="not-a-url")


@pytest.mark.parametrize("env", ["staging", "production"])
def test_sms_is_refused_outside_development_until_a_reply_can_be_read(env: str) -> None:
    """A message a customer cannot stop is an unsolicited one.

    The adapter is real and tested, but no inbound route reads a reply yet, so the
    transport is unrepresentable in a deployed environment rather than merely
    discouraged. This test is the one to delete when that route lands.
    """
    with pytest.raises(ValueError, match="not available in"):
        sms_settings(app_env=env)
