"""Notification transports (F10).

:mod:`app.workers.dispatch` owns *whether* a message may be sent; this module owns
*how* it leaves the process. The split is the one the event side already makes —
the protocol (:class:`~app.workers.dispatch.MessageSender`) lives with the policy
in ``dispatch``, and the concrete transport lives here in ``senders``, exactly as
:class:`~app.workers.outbox_relay.EventPublisher` lives in ``outbox_relay`` and
:class:`~app.workers.publishers.KafkaPublisher` lives in ``publishers``. Two
consequences that are the point of doing it this way: the policy module never
imports ``smtplib``, and a test of the consent and quiet-hours rules never needs a
transport at all.

Four properties this adapter is built around:

1. **A normal return means the relay accepted the message.** ``send`` returns only
   after ``DATA`` was answered with a 2xx, which is the same rule the outbox relay
   holds Kafka to. Fire-and-forget would convert a relay outage into silent
   message loss, and the row would be marked ``sent`` for a message that never
   existed.

2. **The conversation is bounded.** ``SMTP_TIMEOUT_SECONDS`` reaches ``smtplib`` as
   the socket timeout, so connect, TLS, auth and ``DATA`` all have a ceiling. The
   send happens *inside* the transaction that holds the row's ``FOR UPDATE`` lock
   (see :mod:`app.workers.notification_scheduler`), so an unbounded send would
   hold a locked row — and the serial tick loop behind it — open for as long as a
   provider cared to stay silent.

3. **The Message-ID is derived, not random.** ``email.utils.make_msgid`` is the
   obvious call and would be wrong here; see :meth:`SmtpSender._message_id`.

4. **The relay is authenticated, not merely encrypted.** ``starttls()`` is given an
   explicit verifying context, because smtplib's default accepts *any*
   certificate — see :func:`build_ssl_context`. Encryption without that check
   protects a conversation with whoever is in the middle of it.

One adapter per channel, each refusing the others. :class:`SmtpSender` implements
e-mail, :class:`TextbeeSender` implements SMS, and ``voice`` has no adapter at all
— there is no gateway to send it through. A transport therefore refuses a channel
it does not implement rather than delivering, say, a ``voice`` reminder as an
e-mail. The refusal is a :class:`~app.workers.dispatch.SenderError`, which the
policy records as a failed attempt: the row spends its retry budget and parks
``failed`` where an operator sees it, instead of a channel quietly changing on the
way out.

:class:`RoutingSender` is how the two compose: it holds one delegate per channel
and reports the union as its :attr:`~RoutingSender.channels`, so a deployment with
a relay and no gateway (or the reverse) declares exactly what it can deliver and
:func:`~app.workers.dispatch.dispatch_notification` can refuse the rest *before*
reading a contact value. That set, not a single "is a transport configured" flag,
is the thing the policy asks.

Content is also not here yet, beyond a placeholder body: ``template`` names the
milestone and D10-06 defers real rendering until there is a template table. What
this module does compose is the envelope — sender, recipient, subject, Message-ID
and the idempotency header — because that is transport vocabulary rather than
product copy.
"""

from __future__ import annotations

import hashlib
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Any, Protocol, cast

from app.core.config import Settings
from app.core.logging import redact_text
from app.db.enums import Channel
from app.workers.dispatch import (
    MessageSender,
    OutgoingMessage,
    SenderError,
    UnavailableSender,
)

__all__ = [
    "SUBJECT",
    "HttpTransport",
    "RoutingSender",
    "SmtpClient",
    "SmtpSender",
    "TextbeeSender",
    "build_sender",
    "build_ssl_context",
    "create_smtp_sender",
    "create_textbee_sender",
    "open_smtp_client",
]

#: The one subject line every reminder carries.
#:
#: Deliberately free of the milestone code: ``T-30`` is this system's internal
#: vocabulary and means nothing to a customer. It is also constant, which keeps a
#: re-send byte-identical — see :meth:`SmtpSender._message_id`.
SUBJECT = "Contract expiry reminder"

#: Placeholder body. No extracted value appears in it: the contract value, the
#: counterparty and the dates all stay in the database, and the recipient already
#: knows which contract they hold. The reference id is what makes a reply
#: actionable without putting anything sensitive in transit.
_BODY = (
    "Hello,\n"
    "\n"
    "This is a reminder that a contract we hold for you is approaching its\n"
    "expiry date. Please reply to this message if you would like to discuss\n"
    "renewal.\n"
    "\n"
    "Contract reference: {contract_id}\n"
)


class SmtpClient(Protocol):
    """The slice of :class:`smtplib.SMTP` this adapter uses.

    Narrowed to a protocol so the real send path — TLS, auth, envelope, ``DATA``
    and the return value — is exercised against a fake client, the same way
    :class:`~app.workers.publishers.Producer` lets ``KafkaPublisher`` be tested
    without a broker.
    """

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]: ...

    def login(self, user: str, password: str) -> tuple[int, bytes] | None: ...

    def send_message(self, msg: EmailMessage) -> dict[str, tuple[int, bytes]]: ...

    def __enter__(self) -> SmtpClient: ...

    def __exit__(self, *args: object) -> bool | None: ...


class SmtpSender:
    """Delivers one message per SMTP conversation.

    One conversation per message rather than one pooled connection: the
    scheduler sends one row per tick, so a pool would mostly hold sockets open
    against a relay that has already timed them out. It also keeps the failure
    mode small — a broken connection can only affect the message being sent.
    """

    def __init__(
        self,
        connect: Callable[[], SmtpClient],
        *,
        sender_address: str,
        username: str = "",
        password: str = "",
        starttls: bool = True,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._connect = connect
        self._sender_address = sender_address
        self._username = username
        self._password = password
        self._starttls = starttls
        # A verifying context, always, and never smtplib's own default. Passing
        # ``context=None`` to ``starttls()`` lets smtplib build
        # ``ssl._create_stdlib_context()`` — ``CERT_NONE``, ``check_hostname``
        # off — which encrypts to whatever answered the socket and would hand the
        # credentials to a machine in the middle. ``None`` here therefore means
        # "verify against the system trust store", not "do not verify".
        self._context: ssl.SSLContext | None = None
        if starttls:
            self._context = ssl_context if ssl_context is not None else ssl.create_default_context()
        # Taken from the From address so the Message-ID sits in the same domain
        # as the envelope sender, which is what receiving MTAs expect of it.
        self._domain = sender_address.rpartition("@")[2] or "localhost"

    @property
    def channels(self) -> frozenset[Channel]:
        """E-mail only, and always: this transport implements no other channel.

        An instance of this class only exists when ``NOTIFICATION_TRANSPORT=smtp``
        and the relay's host, sender and credentials have already passed start-up
        validation, so "can this transport send at all" is answered by the fact
        that the object was constructed. Whether a *particular* relay is talking
        to us right now is a per-send question, and that is what ``send`` reports.
        """
        return frozenset({Channel.EMAIL})

    def send(self, message: OutgoingMessage) -> str | None:
        """Deliver one message, returning the Message-ID we minted for it.

        Returns rather than raises on relay failure being the caller's rule: a
        :class:`SenderError` is recorded by the policy as an attempt that failed,
        while anything else would escape past the row and leave it looking
        untouched.
        """
        if message.channel is not Channel.EMAIL:
            # Not a transport hiccup: this transport has no way to deliver it, and
            # saying so beats sending an e-mail in place of a phone call.
            raise SenderError(
                f"the SMTP transport cannot deliver channel={message.channel.value}; "
                "email is the only channel it implements"
            )

        message_id = self._message_id(message)
        self.deliver(self._compose(message, message_id))
        return message_id

    def deliver(self, composed: EmailMessage) -> None:
        """Hand one already-composed message to the relay.

        Public, and separate from :meth:`send`, because the attention watch has
        something to say that this class would compose into the wrong message: an
        operator alert has a different subject, no contract and no idempotency
        slot. What it needs is everything *below* the envelope — the verifying TLS
        context, the login, the bounded socket, and an error text scrubbed of
        addresses — and a second copy of that block would be a second place for the
        context to be forgotten.

        Returning nothing is the same rule the reminder path holds: a normal return
        means the relay accepted the message, and anything else is a
        :class:`~app.workers.dispatch.SenderError`.
        """
        try:
            with self._connect() as client:
                if self._starttls:
                    # Explicit, so the relay's certificate and hostname are both
                    # checked. See ``__init__``: the argument is not optional in
                    # spirit, only in signature.
                    client.starttls(context=self._context)
                if self._username:
                    client.login(self._username, self._password)
                client.send_message(composed)
        except (smtplib.SMTPException, OSError) as exc:
            raise SenderError(_describe(exc)) from exc

    def _compose(self, message: OutgoingMessage, message_id: str) -> EmailMessage:
        """Build the envelope around ``message``."""
        composed = EmailMessage()
        composed["Subject"] = SUBJECT
        composed["From"] = self._sender_address
        composed["To"] = message.recipient
        composed["Message-ID"] = message_id
        # Carried to the relay so a duplicate is identifiable there and not only
        # in this database. Non-standard on purpose — SMTP has no idempotency
        # header — and free to add, because an unknown ``X-`` header is ignored.
        composed["X-Idempotency-Key"] = message.idempotency_key
        composed.set_content(_BODY.format(contract_id=message.contract_id))
        return composed

    def _message_id(self, message: OutgoingMessage) -> str:
        """The Message-ID, derived from the slot instead of randomised.

        Delivery here is at-least-once: the row commits after ``send`` returns, so
        a process that dies between "relay accepted" and "row marked sent" sends
        again on the next tick. That is the documented bargain
        (:mod:`app.workers.notification_scheduler`) and it is only survivable if
        the second attempt is *recognisable* as a duplicate.

        ``email.utils.make_msgid`` embeds a random component, so the re-send would
        arrive under a different Message-ID and be a genuinely distinct message to
        every relay and provider that de-duplicates on that header. Hashing the
        idempotency key instead makes both attempts identical in the one field a
        provider can collapse them by — which is all a transport can do about the
        duplication itself.
        """
        digest = hashlib.sha256(message.idempotency_key.encode("utf-8")).hexdigest()
        return f"<{digest}@{self._domain}>"


#: The body of an SMS reminder.
#:
#: Same rule as ``_BODY``: no extracted value crosses the channel, because the
#: recipient already knows which contract they hold. It is shorter because the
#: channel is.
#:
#: It carries **no opt-out instruction, deliberately**. "Reply STOP to stop these
#: messages" is the usual wording and it is absent because this template cannot know
#: whether the deployment can read the reply: ``INBOUND_TRANSPORT=none`` is legal and
#: is the default, so in that configuration the sentence would promise an opt-out that
#: lands nowhere — worse than saying nothing. The body is static for the same reason:
#: wording it per configuration would mean two messages, two tests, and a send path
#: that decides what to say. ``app.api.v1.inbound`` does honour STOP where the route is
#: configured, and ``Settings._reject_insecure_production_defaults`` refuses to send
#: outside development unless it is.
_SMS_BODY = "Reminder: contract {contract_id} with us is approaching its expiry date."


class HttpTransport(Protocol):
    """The one HTTP call this module makes.

    Narrowed to a protocol for the same reason :class:`SmtpClient` is: the real send
    path — URL, headers, body and the gateway's reply — is exercised against a fake,
    so no test needs a gateway account or a socket.
    """

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes: ...


class TextbeeSender:
    """Delivers one SMS per textbee gateway call.

    One request per message, like the SMTP adapter's one conversation per message,
    and for the same reason: the scheduler sends one row per tick while holding that
    row's lock, so a pooled connection would mostly hold sockets open against a
    gateway that has already dropped them.

    Nothing about the recipient is written into an error. ``Notification.last_error``
    is rendered by ``GET /notifications`` to any viewer, and a gateway's refusal
    routinely quotes the number it rejected — the same leak :func:`_describe` scrubs
    for e-mail.
    """

    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_key: str,
        device_id: str,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._device_id = device_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        # The API key is a credential and must not reach a log line through an
        # accidental repr, which is the same reason the SMTP password never does.
        return f"TextbeeSender(base_url={self._base_url!r}, device_id={self._device_id!r})"

    @property
    def channels(self) -> frozenset[Channel]:
        """SMS only: this transport has no way to deliver anything else."""
        return frozenset({Channel.SMS})

    def send(self, message: OutgoingMessage) -> str | None:
        """Deliver one SMS, returning the gateway's id when it supplies one."""
        if message.channel is not Channel.SMS:
            raise SenderError(
                f"the SMS transport cannot deliver channel={message.channel.value}; "
                "sms is the only channel it implements"
            )

        body = {
            "recipients": [message.recipient],
            "message": _SMS_BODY.format(contract_id=message.contract_id),
        }
        # The scheme is fixed by settings and the host is operator configuration
        # validated at start-up (https, or loopback); bandit flags a non-literal URL.
        request = urllib.request.Request(  # noqa: S310  # nosec B310
            f"{self._base_url}/api/v1/gateway/devices/{self._device_id}/send-sms",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-api-key": self._api_key},
            method="POST",
        )
        try:
            raw = self._transport(request, self._timeout)
        except urllib.error.HTTPError as exc:
            # The status code is the actionable part and is all this carries: the
            # body is gateway prose that routinely quotes the rejected recipient.
            raise SenderError(f"SMS delivery failed (HTTP {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SenderError(f"SMS delivery failed: {_failure_name(exc)}") from exc
        return _sms_provider_id(raw)


def _sms_provider_id(raw: bytes) -> str | None:
    """The gateway's message id, or ``None`` when it does not name one.

    Absent is **not** a failure: the row is marked sent because the call returned
    normally, the same acknowledgement rule the outbox relay uses. A body this
    parser cannot read must not turn an accepted send into a failed attempt, because
    the retry would be a second message to a real person.
    """
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    data = parsed.get("data") if isinstance(parsed, dict) else None
    for candidate in (data, parsed):
        if isinstance(candidate, dict):
            for key in ("id", "messageId", "message_id"):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


class RoutingSender:
    """Delivers each message on whichever transport implements its channel.

    Composition rather than a second switch: a deployment may have a relay and a
    gateway at once, and the channel already on the row says which one applies.
    :attr:`channels` is the union of what the delegates implement, and that is what
    lets ``dispatch_notification`` refuse an undeliverable channel *before* the
    contact value is read.
    """

    def __init__(self, senders: Mapping[Channel, MessageSender]) -> None:
        self._senders = dict(senders)

    def __repr__(self) -> str:
        return f"RoutingSender(channels={[c.value for c in sorted(self._senders)]})"

    @property
    def channels(self) -> frozenset[Channel]:
        """Every channel at least one delegate implements."""
        return frozenset(self._senders)

    def send(self, message: OutgoingMessage) -> str | None:
        """Delegate to the transport for ``message.channel``.

        A missing delegate raises rather than returning quietly: the policy has
        already refused this case by asking :attr:`channels`, and this branch exists
        so a future caller that skips that check still fails visibly instead of
        reporting a send that never happened.
        """
        sender = self._senders.get(message.channel)
        if sender is None:
            raise SenderError(f"no transport implements channel={message.channel.value}")
        return sender.send(message)


def create_textbee_sender(settings: Settings) -> TextbeeSender:
    """Build the SMS sender for the configured gateway."""
    return TextbeeSender(
        _urlopen_transport,
        api_key=settings.textbee_api_key,
        device_id=settings.textbee_device_id,
        base_url=settings.textbee_base_url,
        timeout_seconds=settings.sms_timeout_seconds,
    )


def _urlopen_transport(request: urllib.request.Request, timeout: float) -> bytes:
    """The real HTTP call, isolated so a test can substitute one."""
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
        # ``urlopen`` is typed ``Any``, so its ``read()`` is too. Naming the shape we
        # rely on keeps ``Any`` from reaching a ``bytes`` return: a gateway that ever
        # answered with something else fails here, loudly, rather than downstream.
        return bytes(response.read())


def _failure_name(exc: BaseException) -> str:
    """The name of what went wrong, for a message that may not quote the gateway.

    ``URLError`` hides its detail behind ``.reason``, which is either an exception
    (``socket.gaierror``, an ``ssl.SSLError``) or the string smtplib/urllib got
    from the socket layer. Naming ``.reason`` directly is worse than useless —
    ``type("...").__name__`` is ``"str"``, which tells an operator nothing — so
    the raised exception is named first, and a nested exception's type is added
    after it. Only *types* cross this boundary: a type name cannot carry a
    recipient, which is the one thing this text may never do.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        return f"{type(exc).__name__} ({type(reason).__name__})"
    return type(exc).__name__


def _describe(exc: BaseException) -> str:
    """A failure message that is safe to store on the row.

    ``Notification.last_error`` is rendered by ``GET /notifications`` to any
    viewer, including a viewer who may not see recipient values anywhere else.
    A relay's refusal routinely quotes the failing address
    (``550 5.1.1 <someone@example.com>: recipient rejected``), so the text is
    passed through the logging layer's redactor before it is stored: an error
    column must not become a second place a contact value leaks out of.
    """
    detail = redact_text(str(exc)).strip() or type(exc).__name__
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return f"SMTP delivery failed ({code}): {detail}"
    return f"SMTP delivery failed: {detail}"


def open_smtp_client(settings: Settings) -> SmtpClient:
    """Open one SMTP conversation, bounded by ``SMTP_TIMEOUT_SECONDS``.

    ``smtplib`` applies ``timeout`` to the socket, so the same value bounds the
    connect, the TLS handshake, the auth exchange and the ``DATA`` response. No
    later call needs its own timeout, which is why there is no second setting.
    """
    client = smtplib.SMTP(
        host=settings.smtp_host,
        port=settings.smtp_port,
        timeout=settings.smtp_timeout_seconds,
    )
    # `smtplib` carries no type information for this constructor; the protocol
    # above is the contract the rest of the module is written against.
    return cast(SmtpClient, client)


def build_ssl_context(settings: Settings) -> ssl.SSLContext:
    """The TLS context the relay must satisfy.

    ``ssl.create_default_context()`` verifies the chain *and* the hostname, which
    are the two things a machine in the middle cannot forge. ``SMTP_CA_BUNDLE``
    replaces the system trust store rather than disabling verification, so an
    internal relay with a private CA is trusted by naming that CA.
    """
    cafile = settings.smtp_ca_bundle.strip()
    return ssl.create_default_context(cafile=cafile or None)


def create_smtp_sender(settings: Settings) -> SmtpSender:
    """Build the sender for the configured relay.

    The connection is opened per send, so what is passed in is a factory rather
    than a connection: a client captured here would be a socket held open from
    process start until the first message. The TLS context, by contrast, is built
    once here: it is not tied to a socket, and loading a CA bundle per message
    would be cost paid on the send path for no gain.
    """

    def connect() -> SmtpClient:
        return open_smtp_client(settings)

    return SmtpSender(
        connect,
        sender_address=settings.smtp_from,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
        ssl_context=build_ssl_context(settings) if settings.smtp_starttls else None,
    )


def build_sender(settings: Settings) -> MessageSender:
    """Select the transport(s) the settings name, one per channel.

    Two independent switches, because the channels are independent: a deployment may
    hold a CSIRT-blessed relay and no gateway, or a gateway and no relay, and either
    way the other channel must refuse rather than silently send nothing.

    When neither switch names a transport the stand-in is returned with a reason
    naming *both* settings, so an operator reading a 409 or a row's ``last_error``
    learns every value to change, not just the first.
    """
    by_channel: dict[Channel, MessageSender] = {}
    if settings.notification_transport == "smtp":
        by_channel[Channel.EMAIL] = create_smtp_sender(settings)
    if settings.sms_transport == "textbee":
        by_channel[Channel.SMS] = create_textbee_sender(settings)
    if not by_channel:
        return UnavailableSender(
            reason=(
                f"NOTIFICATION_TRANSPORT={settings.notification_transport} and "
                f"SMS_TRANSPORT={settings.sms_transport}"
            )
        )
    return RoutingSender(by_channel)
