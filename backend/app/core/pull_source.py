"""Pull-mode ingestion (P2-H): reach a company API, or refuse to.

Every ingestion route before this one asks a client to *send* us a contract. This
one inverts the direction: an operator names a URL and **we** open the connection.
That single inversion is the whole risk, because a caller-chosen URL decides which
host the API process opens a socket to — the server-side request forgery lever,
where a "contracts API" pointed at ``http://169.254.169.254/latest/meta-data/``
reads a cloud instance's credentials instead of a contract.

Four refusals, each closing a different way of abusing it:

1. **Only https, and only a public address.** The scheme must be ``https``, the
   URL may not carry a username or password, and — the part an allow-list of
   hostnames would miss — every address the host resolves to must be global.
   A public name that resolves to ``10.0.0.7`` is refused exactly like a literal
   ``http://10.0.0.7/``. There is no private, loopback, link-local or reserved
   exception in a deployment; ``PULL_ALLOW_PRIVATE_HOSTS`` exists for a local
   double and staging/production refuse to boot with it set.
2. **No redirect is followed.** A redirect is the standard way past a check like
   (1): the address that was validated is the first hop, and the second is chosen
   by whoever answers. The opener therefore has a redirect handler that raises
   instead of calling back, so a 3xx is reported as a failure.
3. **The body is bounded.** ``Content-Length`` is refused when it already exceeds
   the cap, and the read itself stops one byte past it, so a source that lies
   about its length still cannot stream unbounded bytes into a request thread.
4. **The body must be JSON of a known shape.** Anything else is a configuration
   error — the operator pointed the pull at the wrong endpoint — rather than an
   empty import that looks like a quiet success.

What this module deliberately does **not** do is decide what a contract is. It
returns parsed JSON; :mod:`app.api.v1.contracts` validates each record with the
same ``ContractCreateRequest`` the upload route uses and writes the rows, so there
is exactly one definition of a valid contract in the build.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from app.core.config import Settings

__all__ = [
    "PullPayloadError",
    "PullSource",
    "PullSourceError",
    "PullTooLargeError",
    "PullUnavailableError",
    "PullUrlRefusedError",
    "PulledPayload",
    "build_pull_source",
    "check_pull_url",
]

#: Port used to resolve an address when the URL names none. The check only needs
#: *an* address for the host, and 443 is the one an https URL would use.
_DEFAULT_PORT: Final = 443


class PullSourceError(RuntimeError):
    """Base class for everything a pull can go wrong with.

    A ``RuntimeError`` rather than a ``ValueError``: some of these are the
    caller's fault (a refused URL) and some are the source's (a 500, a reset
    connection), and the route maps each to a different status — so they share a
    base for catching and keep the distinction in the subclasses.
    """


class PullUrlRefusedError(PullSourceError):
    """The URL names a target this build will not open a socket to."""


class PullUnavailableError(PullSourceError):
    """The source answered, but not usefully: 4xx/5xx, a redirect, a reset."""


class PullTooLargeError(PullSourceError):
    """The source's body exceeds ``PULL_MAX_BYTES``."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"the source's body is {size} bytes, over the {limit}-byte limit for one pull"
        )
        self.size = size
        self.limit = limit


class PullPayloadError(PullSourceError):
    """The body was not JSON, or not the shape a contract import expects."""


@dataclass(frozen=True, slots=True)
class PulledPayload:
    """What one successful pull produced, before anything is written."""

    #: The URL as the operator wrote it, echoed back so the response and the log
    #: line can be matched to the request without re-deriving it.
    url: str
    #: Parsed JSON. ``object`` rather than ``Any`` so every reader has to narrow
    #: it — the shape is not known until :func:`contracts_from` has looked.
    document: object
    #: Bytes actually read, for the log line. Not the compressed size, if the
    #: source chose to compress: this is what crossed the socket into the cap.
    bytes_read: int


def _public_addresses(host: str, port: int | None) -> None:
    """Refuse unless every address ``host`` resolves to is globally routable.

    Every address, not the first: a name with one public and one private record is
    a name whose second answer is the interesting one, and which get picked is the
    resolver's business rather than ours.

    The failure messages name the host and the address, never the full URL. A URL
    is operator-supplied and a query string in one can carry a token; the operator
    already knows what they typed, so the part worth printing is which address was
    refused and why.
    """
    try:
        infos = socket.getaddrinfo(host, port or _DEFAULT_PORT, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PullUrlRefusedError(
            f"the source host {host!r} does not resolve: {exc.strerror or exc}"
        ) from exc

    if not infos:
        raise PullUrlRefusedError(f"the source host {host!r} resolved to no address")

    for info in infos:
        # ``info[4]`` is the sockaddr; element 0 is the address, and an IPv6 one
        # may carry a ``%zone`` suffix that ``ip_address`` will not parse.
        address = str(info[4][0])
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise PullUrlRefusedError(
                f"the source host {host!r} resolved to {address!r}, which is not an "
                "address this build will open a socket to"
            ) from exc
        if not parsed.is_global:
            raise PullUrlRefusedError(
                f"the source host {host!r} resolves to {address!r}, which is not a "
                "public address. A pull reaches the public internet and nothing else: "
                "an internal address would let whoever names it make this process "
                "reach its own network."
            )


def check_pull_url(url: str, *, allow_private_hosts: bool) -> str:
    """Validate a source URL, returning it without surrounding whitespace.

    ``allow_private_hosts`` is the local-double door and nothing else: it permits
    ``http`` and skips the address check so a test (or a developer pointing at a
    fake company API on their machine) can exercise the real transport. The
    settings validator refuses it outside development and test, because a
    deployment that can be told to fetch ``http://10.0.0.7/`` is a deployment whose
    network is reachable by anyone who can reach the API.
    """
    text = url.strip()
    if not text:
        raise PullUrlRefusedError("the source URL is empty")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if not host:
        raise PullUrlRefusedError(f"the source URL names no host: {text!r}")
    if parts.username or parts.password:
        # Refused rather than stripped: a credential in a URL would be sent to the
        # source, echoed in the response, and written to the log line that names
        # the URL. A source API that needs a credential needs a different feature.
        raise PullUrlRefusedError(
            "the source URL must not carry a username or password; a credential in a "
            "URL is sent in clear text in the request line and cannot be kept out of "
            "the log"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise PullUrlRefusedError(f"the source URL's port is not a number: {text!r}") from exc

    if scheme == "https":
        pass
    elif scheme == "http":
        if not allow_private_hosts:
            raise PullUrlRefusedError(
                "only https source URLs may be pulled. An http source would send this "
                "deployment's request to a host that cannot prove it is the one named."
            )
    else:
        raise PullUrlRefusedError(
            f"only an http(s) source URL may be pulled; got scheme {scheme or '(none)'!r}"
        )

    if not allow_private_hosts:
        _public_addresses(host, port)
    return text


class _RefuseRedirects(HTTPRedirectHandler):
    """A redirect handler that refuses instead of following.

    ``build_opener`` replaces its default handler with this one because it is a
    subclass of it. Returning ``None`` from ``redirect_request`` would make urllib
    raise a plain ``HTTPError``; raising here is what lets the refusal say *why* a
    3xx is not followed, which is the sentence an operator needs when their API
    moved and the pull started failing.
    """

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise PullUnavailableError(
            f"the source answered {code} and pointed at another address. A pull "
            "follows no redirect: the address that was checked is the first hop, "
            "and the second one would be chosen by whoever answered. Point the pull "
            "at the address it ends up at."
        )


class PullSource:
    """One configured pull: a timeout, a byte cap, and an opener.

    Built per request from the running app's settings rather than held on
    ``app.state`` — there is nothing to pool here, and constructing it where it is
    used is what keeps the cap and the timeout in one place.
    """

    def __init__(
        self,
        *,
        timeout: float,
        max_bytes: int,
        allow_private_hosts: bool,
        max_contracts: int,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._allow_private_hosts = allow_private_hosts
        self._max_contracts = max_contracts

    @property
    def max_contracts(self) -> int:
        """How many contracts one pull may carry. ``0`` is not a legal setting."""
        return self._max_contracts

    def fetch(self, url: str) -> PulledPayload:
        """Fetch and parse one source URL. Raises a :class:`PullSourceError`."""
        target = check_pull_url(url, allow_private_hosts=self._allow_private_hosts)
        # An explicit verifying context rather than the stdlib default: the default
        # is already verifying in a modern build, but stating it here means a future
        # edit cannot quietly switch it off and still look like this code.
        opener = build_opener(
            _RefuseRedirects(),
            HTTPSHandler(context=ssl.create_default_context()),
        )
        # S310 flags any non-literal URL, and this one is caller-supplied — which is
        # exactly why it is not waved through. ``check_pull_url`` ran on the line
        # above and refused everything that is not https (http only under the
        # local-double flag), a URL carrying a credential, and every host that
        # resolves to a private address; ``_RefuseRedirects`` stops a compliant host
        # from redirecting the socket somewhere the check never saw. The flag is
        # about the *shape* of the call, and the shape is what the guard fixes.
        request = Request(  # noqa: S310  # nosec B310
            target, method="GET", headers={"Accept": "application/json"}
        )

        try:
            with opener.open(request, timeout=self._timeout) as response:
                claimed = response.headers.get("Content-Length")
                if claimed is not None and claimed.isdigit() and int(claimed) > self._max_bytes:
                    # Refused before reading: a source that announces a 2 GiB body
                    # must not be given the chance to send one.
                    raise PullTooLargeError(int(claimed), self._max_bytes)
                body = response.read(self._max_bytes + 1)
        except HTTPError as exc:
            raise PullUnavailableError(f"the source answered {exc.code} ({exc.reason})") from exc
        except (URLError, OSError) as exc:
            # Reaches here for a refused connection, a DNS failure, a timeout and a
            # TLS failure alike. The reason is the useful part and none of it is a
            # credential: the URL carried none by construction.
            raise PullUnavailableError(f"the source could not be reached: {exc}") from exc

        if len(body) > self._max_bytes:
            raise PullTooLargeError(len(body), self._max_bytes)

        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PullPayloadError(
                "the source's body is not UTF-8 text, so it cannot be JSON"
            ) from exc

        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PullPayloadError(f"the source's body is not JSON: {exc.msg}") from exc

        return PulledPayload(url=target, document=document, bytes_read=len(body))

    def contracts_from(self, document: object) -> list[dict[str, object]]:
        """Narrow a parsed body to the contract records it carries.

        Three shapes are accepted, because three are what a company API plausibly
        returns and refusing two of them would be an argument, not a guard: a
        single contract object, an array of them, or an object with a
        ``contracts`` array. Everything else is refused, including an *empty*
        array — a pull that yields nothing means the operator pointed it at the
        wrong endpoint or the wrong page, and reporting "0 imported" as a success
        would hide that.
        """
        if isinstance(document, list):
            rows: object = document
        elif isinstance(document, dict):
            rows = document.get("contracts", document)
        else:
            raise PullPayloadError(
                "the source's body must be a JSON object or array of contracts; got "
                f"{type(document).__name__}"
            )

        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise PullPayloadError("the source's 'contracts' member must be a JSON array")
        if not rows:
            raise PullPayloadError(
                "the source answered with no contracts. A pull that imports nothing "
                "is a URL pointing at the wrong place, not a successful one."
            )
        if len(rows) > self._max_contracts:
            raise PullPayloadError(
                f"the source answered with {len(rows)} contracts, over the limit of "
                f"{self._max_contracts} for one pull. Page the source, or raise "
                "PULL_MAX_CONTRACTS deliberately."
            )

        records: list[dict[str, object]] = []
        for position, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise PullPayloadError(
                    f"contract {position} is a JSON {type(row).__name__}, not an object"
                )
            records.append(row)
        return records


def build_pull_source(settings: Settings) -> PullSource:
    """The pull this deployment is configured for."""
    return PullSource(
        timeout=settings.pull_timeout_seconds,
        max_bytes=settings.pull_max_bytes,
        allow_private_hosts=settings.pull_allow_private_hosts,
        max_contracts=settings.pull_max_contracts,
    )
