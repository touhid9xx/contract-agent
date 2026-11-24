"""Tests for the voice transport and the ARI dialler behind it.

Exercised against a fake HTTP transport, the way ``test_sms_sender.py`` exercises
the gateway and ``test_senders.py`` the SMTP relay: no Asterisk, no trunk and no
phone number belonging to anyone real.

Four properties get most of the attention here:

* the **dialled number never leaves in an error**. ``Notification.last_error`` is
  rendered by ``GET /notifications`` to any viewer, and the dial string is the
  recipient once rendered. An error column must not become a second place a
  contact value leaks out of.
* the **password never leaves in a repr**, and neither does the endpoint — the
  endpoint is the template that becomes a phone number.
* the endpoint really is a **template**: the number is substituted, and braces
  that are not the placeholder survive rather than raising ``KeyError``.
* ``voice_transport=ari`` buys **a channel that is created**, not a customer who
  heard anything. What the call becomes is the Stasis application's business, and
  the acknowledgement rule below says so: a reply this parser cannot read still
  means the call was placed.
"""

from __future__ import annotations

import base64
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from app.core.config import ARI_NUMBER_PLACEHOLDER, Settings
from app.db.enums import Channel
from app.workers.dispatch import (
    MessageSender,
    OutgoingMessage,
    SenderError,
    UnavailableSender,
)
from app.workers.senders import (
    AriVoiceSender,
    RoutingSender,
    build_sender,
    create_ari_sender,
)

#: The approved contact value. A dial must reach it, and no error text may repeat it.
CONTACT_NUMBER = "+8801700000000"
USERNAME = "ari-user"
PASSWORD = "ari_test_secret_do_not_log"
ENDPOINT = f"PJSIP/{ARI_NUMBER_PLACEHOLDER}@trunk"
BASE_URL = "http://127.0.0.1:8088"
APP = "contract-reminders"

MINIMAL_ENV = {
    "app_env": "test",
    "log_json": False,
    "voice_transport": "ari",
    "ari_base_url": BASE_URL,
    "ari_username": USERNAME,
    "ari_password": PASSWORD,
    "ari_endpoint": ENDPOINT,
}


def voice_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(MINIMAL_ENV)
    values.update(overrides)
    return Settings(**values)


def message(channel: Channel = Channel.VOICE, recipient: str = CONTACT_NUMBER) -> OutgoingMessage:
    return OutgoingMessage(
        channel=channel,
        template="T-30",
        contract_id=4242,
        idempotency_key="contract-4242-voice-T-30",
        recipient=recipient,
    )


class RecordingTransport:
    """A dialler that accepts everything and remembers the request it was given."""

    def __init__(self, reply: bytes = b'{"id": "channel-1"}') -> None:
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

    def url(self, index: int = 0) -> str:
        return self.requests[index].full_url

    def params(self, index: int = 0) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.url(index)).query)


class RefusingTransport:
    """A dialler that answered, and refused."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        raise self._error


def sender(transport: Any = None, **overrides: Any) -> AriVoiceSender:
    return AriVoiceSender(
        transport if transport is not None else RecordingTransport(),
        username=overrides.get("username", USERNAME),
        password=overrides.get("password", PASSWORD),
        base_url=overrides.get("base_url", BASE_URL),
        endpoint=overrides.get("endpoint", ENDPOINT),
        app=overrides.get("app", APP),
        caller_id=overrides.get("caller_id", ""),
        ring_seconds=overrides.get("ring_seconds", 25),
    )


# --- the request -----------------------------------------------------------
def test_the_call_is_created_on_the_channels_route() -> None:
    transport = RecordingTransport()

    sender(transport).send(message())

    assert transport.url().startswith(f"{BASE_URL}/ari/channels?")
    assert transport.requests[0].get_method() == "POST"


def test_the_credentials_ride_in_a_basic_auth_header() -> None:
    transport = RecordingTransport()

    sender(transport).send(message())

    expected = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode("ascii")
    assert transport.headers()["authorization"] == f"Basic {expected}"


def test_the_dialled_endpoint_is_the_template_with_the_number_substituted() -> None:
    transport = RecordingTransport()

    sender(transport).send(message())

    assert transport.params()["endpoint"] == [f"PJSIP/{CONTACT_NUMBER}@trunk"]


def test_the_number_cannot_split_the_query_string() -> None:
    """An international number starts with ``+``.

    Interpolated into a query string by hand, that ``+`` decodes as a space and the
    endpoint arrives mangled. ``urlencode`` is what keeps it one parameter, so this
    pins the encoded form rather than only the decoded value.
    """
    transport = RecordingTransport()

    sender(transport).send(message())

    assert "+" not in urllib.parse.urlsplit(transport.url()).query
    assert len(transport.params()["endpoint"]) == 1


def test_the_stasis_app_and_ring_bound_are_sent() -> None:
    transport = RecordingTransport()

    sender(transport, ring_seconds=12).send(message())

    params = transport.params()
    assert params["app"] == [APP]
    assert params["timeout"] == ["12"]


def test_a_caller_id_is_sent_only_when_one_is_configured() -> None:
    """An empty caller ID must be omitted, not sent as an empty parameter.

    Asterisk would otherwise try to present "" and refuse the call, turning a
    deliberately optional setting into a required one.
    """
    without = RecordingTransport()
    sender(without).send(message())

    with_caller_id = RecordingTransport()
    sender(with_caller_id, caller_id="+8801700000001").send(message())

    assert "callerId" not in without.params()
    assert with_caller_id.params()["callerId"] == ["+8801700000001"]


def test_the_http_call_is_bounded_by_the_ring_bound() -> None:
    """One number, not two.

    ``voice_ring_seconds`` is both how long the far end may ring and how long this
    process may wait: the dial happens inside the transaction that holds the row's
    lock, so a second, longer bound would let a silent call hold it open.
    """
    transport = RecordingTransport()

    sender(transport, ring_seconds=12).send(message())

    assert transport.timeouts == [12.0]


def test_a_brace_that_is_not_the_placeholder_survives() -> None:
    """The endpoint is operator configuration, so it may hold braces of its own.

    ``str.format`` would raise ``KeyError`` there and surface as a failed send for a
    reason that has nothing to do with dialling.
    """
    transport = RecordingTransport()

    sender(transport, endpoint="PJSIP/{number}@trunk/{weird}").send(message())

    assert transport.params()["endpoint"] == [f"PJSIP/{CONTACT_NUMBER}@trunk/{{weird}}"]


# --- the reply -------------------------------------------------------------
def test_the_channel_id_is_returned_when_it_names_one() -> None:
    assert sender().send(message()) == "channel-1"


def test_an_unreadable_reply_is_not_a_failure() -> None:
    """The call returned, so the channel was created.

    Raising here would turn a placed call into a failed attempt, and the retry
    would ring a real customer a second time.
    """
    for reply in (b"", b"not json", b"[]", b'{"id": ""}', b'{"id": 7}'):
        assert sender(RecordingTransport(reply)).send(message()) is None


# --- channel discipline ----------------------------------------------------
def test_the_transport_declares_voice_only() -> None:
    assert sender().channels == frozenset({Channel.VOICE})


@pytest.mark.parametrize("channel", [Channel.EMAIL, Channel.SMS])
def test_it_refuses_a_channel_it_does_not_implement(channel: Channel) -> None:
    transport = RecordingTransport()

    with pytest.raises(SenderError) as raised:
        sender(transport).send(message(channel=channel))

    assert channel.value in str(raised.value)
    # Refused before the socket: no call may be placed for the wrong channel.
    assert transport.requests == []


# --- what a failure is allowed to say --------------------------------------
def test_an_http_refusal_names_the_code_and_not_the_number() -> None:
    """ARI echoes the request back in its error body, dial string included.

    So the status is carried and the body is not: ``last_error`` is rendered to
    every viewer of ``GET /notifications``.
    """
    error = urllib.error.HTTPError(
        f"{BASE_URL}/ari/channels",
        409,
        f"channel conflict for {CONTACT_NUMBER}",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )

    with pytest.raises(SenderError) as raised:
        sender(RefusingTransport(error)).send(message())

    text = str(raised.value)
    assert "409" in text
    assert CONTACT_NUMBER not in text


def test_a_transport_failure_names_the_cause_and_not_the_number() -> None:
    error = urllib.error.URLError(f"cannot reach {BASE_URL} for {CONTACT_NUMBER}")

    with pytest.raises(SenderError) as raised:
        sender(RefusingTransport(error)).send(message())

    text = str(raised.value)
    assert "voice call failed" in text
    assert "URLError" in text
    assert CONTACT_NUMBER not in text


def test_a_nested_cause_is_named_by_type_and_never_by_its_text() -> None:
    """``URLError`` keeps the interesting part in ``.reason``, often a string.

    Named by type, so the message cannot quote a host or a number — the same fix the
    SMS adapter needed after it stored ``"SMS delivery failed: str"``.
    """
    error = urllib.error.URLError(socket.gaierror(f"name resolution failed for {CONTACT_NUMBER}"))

    with pytest.raises(SenderError) as raised:
        sender(RefusingTransport(error)).send(message())

    text = str(raised.value)
    assert "URLError" in text
    assert "gaierror" in text
    assert CONTACT_NUMBER not in text


def test_a_timeout_is_reported_as_a_sender_error() -> None:
    with pytest.raises(SenderError):
        sender(RefusingTransport(TimeoutError("timed out"))).send(message())


def test_the_repr_carries_neither_the_password_nor_the_endpoint() -> None:
    """A repr reaches log lines and tracebacks.

    The password is a credential, and the endpoint is a phone number waiting to be
    one — neither may ride along.
    """
    rendered = repr(sender())

    assert PASSWORD not in rendered
    assert "PJSIP" not in rendered
    assert APP in rendered


# --- selection -------------------------------------------------------------
def test_the_voice_switch_alone_yields_a_router_that_dials_only() -> None:
    built: MessageSender = build_sender(voice_settings())

    assert isinstance(built, RoutingSender)
    assert built.channels == frozenset({Channel.VOICE})
    # The other channels must refuse rather than borrow the dialler.
    for other in (Channel.EMAIL, Channel.SMS):
        with pytest.raises(SenderError):
            built.send(message(channel=other))


def test_all_three_switches_compose_into_one_router() -> None:
    built = build_sender(
        voice_settings(
            notification_transport="smtp",
            smtp_host="relay.example.test",
            smtp_from="reminders@example.test",
            sms_transport="textbee",
            textbee_api_key="tb_test_key_do_not_log",
            textbee_device_id="device-123",
        )
    )

    assert isinstance(built, RoutingSender)
    assert built.channels == frozenset(Channel)


def test_no_switch_at_all_still_names_voice_in_the_reason() -> None:
    """The stand-in's reason must name every switch, voice included.

    A reason naming two of three sends the operator looking in the wrong place.
    """
    built = build_sender(Settings(app_env="test", log_json=False))

    assert isinstance(built, UnavailableSender)
    with pytest.raises(SenderError) as raised:
        built.send(message())
    assert "VOICE_TRANSPORT=none" in str(raised.value)


def test_building_the_voice_sender_opens_no_connection() -> None:
    """The call belongs to a send, not to start-up: the API process must boot with
    Asterisk down, and the scheduler must fail one row rather than the run."""
    built = create_ari_sender(voice_settings())

    assert isinstance(built, AriVoiceSender)


# --- configuration ---------------------------------------------------------
def test_voice_is_off_by_default() -> None:
    assert Settings(app_env="test", log_json=False).voice_transport == "none"


def test_an_empty_voice_switch_still_refuses_nothing() -> None:
    """``none`` is not a half-configured dialler: no credentials needed, no refusal."""
    settings = Settings(app_env="test", log_json=False, voice_transport="none")

    # Deliberately not "the set is empty": what this pins is that ``none`` contributes
    # nothing to it, whichever other switches the defaults happen to leave on.
    assert Channel.VOICE not in settings.deliverable_notification_channels


@pytest.mark.parametrize(
    "override",
    [
        {"ari_username": "", "ari_password": ""},
        {"ari_username": "  "},
        {"ari_password": ""},
    ],
)
def test_ari_without_both_credentials_is_refused_at_start_up(override: dict[str, Any]) -> None:
    """ARI has no authentication but Basic, so a half pair is a 401 at the first ring —
    which the scheduler performs inside the row lock."""
    with pytest.raises(ValueError, match="ARI_USERNAME and ARI_PASSWORD"):
        voice_settings(**override)


def test_a_plaintext_remote_dialler_is_refused() -> None:
    """Stricter than nothing: the query string carries the customer's number and the
    header carries the credential, so a plaintext hop is refused in every environment."""
    with pytest.raises(ValueError, match="must be https"):
        voice_settings(ari_base_url="http://asterisk.example.test:8088")


def test_a_dialler_url_with_no_host_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute URL"):
        voice_settings(ari_base_url="asterisk:8088")


def test_a_loopback_dialler_may_stay_plaintext() -> None:
    """How ARI is normally reached: a sidecar on the same host, not across a network."""
    assert voice_settings(ari_base_url="http://127.0.0.1:8088").voice_transport == "ari"


def test_an_endpoint_without_the_placeholder_is_refused_at_start_up() -> None:
    """A fixed endpoint would ring one phone for every reminder and report nothing."""
    with pytest.raises(ValueError, match="ARI_ENDPOINT must contain"):
        voice_settings(ari_endpoint="PJSIP/trunk")


def test_the_refusal_names_the_placeholder_the_sender_substitutes() -> None:
    """One constant, so the setting's help, the refusal and the sender cannot disagree
    about the token they are talking about."""
    with pytest.raises(ValueError, match="ARI_ENDPOINT must contain") as raised:
        voice_settings(ari_endpoint="PJSIP/trunk")

    assert repr(ARI_NUMBER_PLACEHOLDER) in str(raised.value)
