"""Malware scanning for uploaded documents (P2-A's third ingredient).

``P2-A`` described an upload as "magic-byte + ClamAV + sha256 dedup", and until now
only two of those three existed: :func:`app.core.document_text.document_kind`
decides the format from the bytes rather than from a name the client chose, and
:func:`app.core.documents.document_digest` keys the blob by its own SHA-256. This
module is the third, and it is the only one that needs something running beside the
application, which is why it is a *switch* (``VIRUS_SCAN_TRANSPORT``) rather than a
rule that is always on.

Three properties, each a decision:

**Unexamined is not clean.** ``VIRUS_SCAN_TRANSPORT=none`` is the default and it is
honest: it says no scanner looked, not that a scanner approved. That is why
:func:`build_scanner` returns ``None`` rather than a no-op scanner — a
:class:`DocumentScanner` that silently returns is a thing a caller can mistake for
a verdict, and the one place that has to get this right is the route that decides
whether to write bytes to disk.

**Fail closed, three ways.** clamd can answer *clean*, answer *there is a signature*,
answer *something this build does not understand*, or not answer at all. Only the
first permits the document to be stored. The other three raise, and they raise
differently because the operator response differs: an infection is a security
event, silence is a daemon/network problem, and an unrecognised reply is a
version/config problem. Collapsing the last two into one exception is how an
operator spends an afternoon checking a network that is fine.

**The reply is data, not a message.** A clamd answer arrives over a plain,
unauthenticated socket, so it is untrusted input: a signature name is far-end text
and would be reflected into an API response and a log line if this module put it in
an exception. It does not. Errors carry the *fact* — infected, unreachable,
unreadable — and never the reply, the document bytes, or the endpoint. The same
rule :class:`~app.workers.senders.AriVoiceSender` follows with an HTTP status.

The protocol is ClamAV's ``INSTREAM``: ``zINSTREAM\\0``, then the payload as
length-prefixed chunks, then a zero-length chunk. Chunking matters — a document is
bounded by ``DOCUMENT_MAX_BYTES`` but a single ``sendall`` of ten megabytes would
ask the kernel to buffer all of it at once, and clamd's own receive buffer is
smaller than that.
"""

from __future__ import annotations

import socket
from typing import Protocol

from app.core.config import Settings

__all__ = [
    "ClamdScanner",
    "DocumentInfectedError",
    "DocumentScanError",
    "DocumentScanUnavailableError",
    "DocumentScanUnreadableError",
    "DocumentScanner",
    "build_scanner",
]

#: The command clamd expects at the head of a stream scan. The trailing NUL is part
#: of the protocol, not a C-style terminator this module chose.
_INSTREAM = b"zINSTREAM\x00"

#: How much payload is framed per ``sendall``. 8 KiB is small enough to keep the
#: kernel buffer and clamd's receive buffer from ever disagreeing about how much is
#: outstanding, and large enough that a 10 MiB document is ~1280 frames rather than
#: a syscall per kilobyte.
_CHUNK_BYTES = 8192

#: A zero-length chunk is what ends the stream. Four bytes rather than a delimiter
#: because the length prefix is already the framing: a document may contain any byte
#: sequence, including whatever a delimiter would have been.
_STREAM_END = b"\x00\x00\x00\x00"

#: The clean verdict, verbatim. Matched exactly rather than by prefix: "stream: OK"
#: is the whole language clamd speaks for success, and anything else — an empty
#: reply, a version banner, a partial line — must not be read as approval.
_CLEAN = b"stream: OK"

#: What every positive verdict ends with. Checked as a suffix so the signature name
#: in between is never parsed, stored, or logged.
_FOUND = b" FOUND"

#: Ceiling on the reply. A verdict line is tens of bytes; the cap exists so a daemon
#: that answers with an endless stream is cut off rather than buffered.
_MAX_REPLY_BYTES = 4096


class DocumentScanError(RuntimeError):
    """Base for every refusal this module raises.

    A refusal means exactly one thing to a caller: **do not store the document**.
    Nothing is stored on this path, so the bytes are discarded when the request
    ends and a retry is a fresh decision.
    """


class DocumentInfectedError(DocumentScanError):
    """Raised when the scanner matched the document against a signature.

    Deliberately carries no signature name. It is far-end text, it would be
    reflected into a 422 body, and the actionable fact for the caller is "this
    document is not accepted" — the signature belongs in clamd's own log, where an
    operator reads it without this process having to vouch for its content.
    """


class DocumentScanUnavailableError(DocumentScanError):
    """Raised when no scanner answered: refused connection, timeout, dead socket.

    Named separately from :class:`DocumentScanUnreadableError` because the fix is
    different: this one is "is clamd running and reachable", the other is "is it the
    daemon we think it is". A caller that sees neither will check both.
    """


class DocumentScanUnreadableError(DocumentScanError):
    """Raised when the scanner answered something this build does not recognise.

    An unrecognised reply is refused rather than assumed clean. ``INSTREAM`` has
    three answers; a fourth means a proxy, a different daemon on the port, a
    truncated read, or a version this module has never seen — and reading any of
    those as approval would turn a misconfiguration into an accepted document.
    """


class DocumentScanner(Protocol):
    """What the ingestion route asks before it writes a document to disk.

    Deliberately one method that returns ``None``: a refusal is an exception, so
    there is no falsy return value that a caller can forget to check. The same
    shape :meth:`app.core.documents.DocumentStore.save` uses.
    """

    def scan(self, payload: bytes) -> None:
        """Return normally when the document may be stored; raise otherwise."""
        ...


class ClamdScanner:
    """One TCP conversation with clamd per document.

    A connection per scan rather than a pool: ingestion is an occasional write, and
    a pool would be a second lifecycle to close on shutdown for a call that happens
    once per upload. clamd is happy with either.

    TCP rather than clamd's unix socket because the two processes normally live in
    different containers, where a socket path names a file this process cannot open.
    That has a consequence the settings file owns: the protocol is plain and
    unauthenticated, so ``CLAMD_HOST`` is refused at start-up when it is a public
    address.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        # The endpoint is operator configuration rather than a secret, so it is safe
        # to print and useful in a start-up line. Nothing about a document appears
        # here because nothing about a document is stored on this object.
        return f"ClamdScanner(endpoint={self._host}:{self._port})"

    def scan(self, payload: bytes) -> None:
        """Scan ``payload`` and return only if clamd answered clean."""
        try:
            connection = socket.create_connection((self._host, self._port), timeout=self._timeout)
        except OSError as exc:
            # ``OSError`` covers ``ConnectionRefusedError``, a DNS failure and a
            # connect timeout alike. Only the type name crosses this boundary: the
            # resolver's message can quote the host, and this text can reach a 503.
            raise DocumentScanUnavailableError(
                f"no scanner answered ({type(exc).__name__})"
            ) from exc

        try:
            connection.settimeout(self._timeout)
            connection.sendall(_INSTREAM)
            for start in range(0, len(payload), _CHUNK_BYTES):
                chunk = payload[start : start + _CHUNK_BYTES]
                connection.sendall(len(chunk).to_bytes(4, "big") + chunk)
            connection.sendall(_STREAM_END)
            reply = _read_reply(connection)
        except TimeoutError as exc:
            # A daemon that accepts the connection and then says nothing is the
            # failure ``CLAMD_TIMEOUT_SECONDS`` exists for, and it is per operation:
            # a large but healthy scan keeps making progress and never trips it.
            raise DocumentScanUnavailableError(
                "the scanner accepted the document and then stopped answering"
            ) from exc
        except OSError as exc:
            raise DocumentScanUnavailableError(
                f"the connection to the scanner failed ({type(exc).__name__})"
            ) from exc
        finally:
            connection.close()

        _verdict(reply)


def _read_reply(connection: socket.socket) -> bytes:
    """Read one NUL-terminated reply, bounded by :data:`_MAX_REPLY_BYTES`.

    Clamd terminates its verdict with a NUL, so EOF and the terminator are both
    accepted: a daemon that closes immediately after answering is not a different
    answer, and treating it as one would refuse a clean document.
    """
    received = bytearray()
    while len(received) < _MAX_REPLY_BYTES:
        piece = connection.recv(256)
        if not piece:
            break
        received.extend(piece)
        if b"\x00" in piece:
            break
    return bytes(received)


def _verdict(reply: bytes) -> None:
    """Translate a clamd reply, refusing anything that is not a clean verdict."""
    line = reply.split(b"\x00", 1)[0].strip()
    if line.endswith(_FOUND):
        raise DocumentInfectedError(
            "the document matched a known malware signature and was not stored"
        )
    if line == _CLEAN:
        return
    # Nothing from ``line`` is included: it is far-end text, it may be arbitrarily
    # long or arbitrarily shaped, and this message reaches an API response.
    raise DocumentScanUnreadableError(
        "the scanner answered something this build does not recognise; the "
        "document was not stored. Check CLAMD_HOST names a clamd and not a proxy."
    )


def build_scanner(settings: Settings) -> DocumentScanner | None:
    """The scanner this deployment is configured for, or ``None`` for none.

    ``None`` is a real answer and it is why this returns an optional rather than a
    no-op scanner. ``VIRUS_SCAN_TRANSPORT=none`` means documents are stored
    **unexamined** — not inspected and found clean — and the difference is only
    visible to the caller if the absence is expressible. A :class:`DocumentScanner`
    that returns without looking would let the ingestion route report a scan that
    never happened; an explicit ``None`` makes the route state the situation.
    """
    if settings.virus_scan_transport == "clamd":
        return ClamdScanner(
            host=settings.clamd_host.strip(),
            port=settings.clamd_port,
            timeout_seconds=settings.clamd_timeout_seconds,
        )
    return None
