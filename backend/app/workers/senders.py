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

Three properties this adapter is built around:

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

What is deliberately **not** here: SMS and voice. There is no gateway to send them
through, so :meth:`SmtpSender.send` refuses any non-e-mail channel rather than
delivering, say, a ``voice`` reminder as an e-mail. The refusal is a
:class:`~app.workers.dispatch.SenderError`, which the policy records as a failed
attempt: the row spends its retry budget and parks ``failed`` where an operator
sees it, instead of a channel quietly changing on the way out.

Content is also not here yet, beyond a placeholder body: ``template`` names the
milestone and D10-06 defers real rendering until there is a template table. What
this module does compose is the envelope — sender, recipient, subject, Message-ID
and the idempotency header — because that is transport vocabulary rather than
product copy.
"""

from __future__ import annotations

import hashlib
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol, cast

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
    "SmtpClient",
    "SmtpSender",
    "build_sender",
    "create_smtp_sender",
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

    def starttls(self) -> tuple[int, bytes]: ...

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
    ) -> None:
        self._connect = connect
        self._sender_address = sender_address
        self._username = username
        self._password = password
        self._starttls = starttls
        # Taken from the From address so the Message-ID sits in the same domain
        # as the envelope sender, which is what receiving MTAs expect of it.
        self._domain = sender_address.rpartition("@")[2] or "localhost"

    @property
    def available(self) -> bool:
        """Always ``True``.

        An instance of this class only exists when ``NOTIFICATION_TRANSPORT=smtp``
        and the relay's host, sender and credentials have already passed start-up
        validation, so "can this transport send at all" is answered by the fact
        that the object was constructed. Whether a *particular* relay is talking
        to us right now is a per-send question, and that is what ``send`` reports.
        """
        return True

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
        composed = self._compose(message, message_id)
        try:
            with self._connect() as client:
                if self._starttls:
                    client.starttls()
                if self._username:
                    client.login(self._username, self._password)
                client.send_message(composed)
        except (smtplib.SMTPException, OSError) as exc:
            raise SenderError(_describe(exc)) from exc
        return message_id

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


def create_smtp_sender(settings: Settings) -> SmtpSender:
    """Build the sender for the configured relay.

    The connection is opened per send, so what is passed in is a factory rather
    than a connection: a client captured here would be a socket held open from
    process start until the first message.
    """

    def connect() -> SmtpClient:
        return open_smtp_client(settings)

    return SmtpSender(
        connect,
        sender_address=settings.smtp_from,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
    )


def build_sender(settings: Settings) -> MessageSender:
    """Select the transport named by ``NOTIFICATION_TRANSPORT``.

    The two-branch body is total over ``NotificationTransport``; an unknown value
    cannot reach here because settings validation rejects it at start-up. The
    stand-in names the setting it read, so an operator reading a 409 or a row's
    ``last_error`` learns which value to change.
    """
    if settings.notification_transport == "smtp":
        return create_smtp_sender(settings)
    return UnavailableSender(reason=f"NOTIFICATION_TRANSPORT={settings.notification_transport}")
