"""The paths the browser calls, compared with the routes the API declares.

A route is named twice in this tree, and each copy is validated on its own:

* ``backend/app/api/v1/*.py`` — the decorators that mount it, which are the only copy
  that makes the endpoint answer anything;
* ``frontend/src/lib/api.ts`` — the ``request<…>(…)`` call whose string is the only copy
  that decides what the browser asks for.

The two CI jobs never meet. Backend tests call their own route literals through
``TestClient``; frontend tests stub ``fetch`` and assert about the arguments the stub
received, so they are satisfied by *any* path string. Nothing imports the other side.
Renaming ``/contracts/{contract_id}/fields`` therefore passes ruff, mypy, bandit, the
whole backend suite, typecheck, vitest and every one of the nine CI jobs, and produces a
404 that only a browser can see -- in a screen whose loading state reads exactly like a
slow server.

``frontend/src/lib/api.ts`` opens by claiming the opposite: "every endpoint is a typed
function, so a route rename becomes a compile error instead of a 404 at run time." That
is true of the *function* name and of the wire shapes it declares; it is not true of the
path or the verb, which are untyped strings ``tsc`` never looks at. The same defect
``D10-120`` shipped seven times by hand -- a name written in two compartments, each
validated alone -- one layer out from the HTTP client. So this file reads both sides and
compares them, restating neither: the routes come from the running application's own
route table, and the calls come from the module's source.

**What this does not cover, stated rather than implied.** It compares *which* route each
call reaches, not what the route does with the request: a call that sends ``POST`` to a
route that reads the body as ``PATCH`` is caught here, but a call that sends the wrong
*payload shape* to the right route is not -- the TS interfaces are the only guard on
that, and they are not derived from the Pydantic models. It also does not check the
reverse direction (a route nothing calls), because legitimately uncalled routes exist:
``/inbound/sms`` is called by the SMS relay, ``/metrics`` and ``/health`` by the
orchestrator, and several routes by the smoke journeys rather than by a screen.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
API_MODULE = ROOT / "frontend" / "src" / "lib" / "api.ts"

#: The call form this file reads: ``request<Session>("/auth/login", …)``. The definition
#: ``async function request<T>(path: string, …)`` matches too, and is skipped by looking
#: at what precedes it rather than by an offset or a line number.
CALL_MARKER = "request<"
DEFINITION_PREFIX = "function"

#: A path parameter on either side: ``{contract_id}`` in the app's table, ``${contractId}``
#: in the template literal. Both collapse to the same placeholder, so the comparison is
#: about the *shape* of the route and not about what the two languages call its parts.
PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{[^}/]*\}")

#: String literals in a call's first argument: a plain path, or a template literal with
#: ``${…}`` holes. A template's ``${…}`` may not contain a backtick, which none do here.
LITERAL = re.compile(r'"([^"\\\n]*)"|`([^`\\]*)`')

#: The verb, when the call passes one. Absent means ``GET``, which is ``request``'s own
#: default -- read from the call site rather than assumed to be the common case.
METHOD = re.compile(r'method:\s*"([A-Z]+)"')

#: ``const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1"`` — the prefix the
#: browser prepends to every path below. The fallback is the value that ships.
BASE_DEFAULT = re.compile(r'NEXT_PUBLIC_API_BASE\s*\?\?\s*"([^"]*)"')

#: ``await fetch("/health", …)`` — the one call that bypasses ``request`` because it
#: targets a route outside the versioned prefix.
DIRECT_FETCH = re.compile(r'fetch\("([^"]+)"')


def _matching(source: str, start: int, opening: str, closing: str) -> int:
    """Index just past the ``closing`` that matches the ``opening`` at ``start``.

    Quote-aware, so a ``)`` inside a string does not end a call and a ``<`` inside a
    template literal does not open a generic. Raises rather than returning a wrong span:
    a reader that silently mis-parses would make the assertions below vacuously true.
    """

    depth = 0
    quote: str | None = None
    index = start
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise AssertionError(f"unbalanced {opening!r} at offset {start}")


def _call_arguments(source: str) -> list[tuple[int, str]]:
    """The argument text of every ``request<…>(…)`` in ``source``, with its offset."""

    spans: list[tuple[int, str]] = []
    cursor = 0
    while (start := source.find(CALL_MARKER, cursor)) != -1:
        after_type = _matching(source, start + len(CALL_MARKER) - 1, "<", ">")
        if source[after_type : after_type + 1] != "(":
            raise AssertionError(f"request<…> at offset {start} is not followed by a call")
        after_call = _matching(source, after_type, "(", ")")
        spans.append((start, source[after_type + 1 : after_call - 1]))
        cursor = after_call
    return spans


def _first_argument(arguments: str) -> str:
    """Everything before the first top-level comma -- the path expression, and only it."""

    depth = 0
    quote: str | None = None
    index = 0
    while index < len(arguments):
        char = arguments[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return arguments[:index]
        index += 1
    return arguments


def _shape(path: str) -> str:
    """A route's identity: no query string, and every parameter reduced to ``{}``."""

    return PLACEHOLDER.sub("{}", path.split("?", 1)[0])


def _calls() -> set[tuple[str, str]]:
    """Every ``(verb, path shape)`` the browser can ask for, read out of the module."""

    source = API_MODULE.read_text(encoding="utf-8")
    calls: set[tuple[str, str]] = set()
    definitions = 0
    for start, arguments in _call_arguments(source):
        if source[:start].rstrip().endswith(DEFINITION_PREFIX):
            definitions += 1
            continue
        literals = LITERAL.findall(_first_argument(arguments))
        paths = [
            quoted or template
            for quoted, template in literals
            if (quoted or template).startswith("/")
        ]
        # A call that names no path is a reader failure, not a call to skip: it would
        # let the rest of the file pass while this call site went unchecked.
        assert paths, f"request at offset {start} names no path: {arguments!r}"
        verb = METHOD.search(arguments)
        for path in paths:
            calls.add((verb.group(1) if verb else "GET", _shape(path)))
    assert definitions == 1, f"expected one `function request<T>` definition, found {definitions}"
    return calls


def _routes(settings: Settings) -> set[tuple[str, str]]:
    """Every ``(verb, path shape)`` the application answers, from its own route table."""

    prefix = settings.api_v1_prefix.rstrip("/")
    routes: set[tuple[str, str]] = set()
    for route in create_app(settings).routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        # The versioned surface is mounted under the prefix and the frontend's base
        # string already carries it, so the comparison is made below it. Probes are
        # mounted at the root and keep their absolute path.
        if path.startswith(f"{prefix}/"):
            path = path[len(prefix) :]
        for verb in route.methods:
            routes.add((verb.upper(), _shape(path)))
    return routes


def test_every_call_the_browser_makes_is_a_route_the_api_declares(settings: Settings) -> None:
    """The one comparison no gate in this repo made: client call site against route."""

    declared = _routes(settings)
    unknown = sorted(_calls() - declared)

    assert not unknown, (
        "the browser asks for routes the API does not declare, so each one is a 404 "
        "whose screen shows a loading state or an empty list: "
        + ", ".join(f"{verb} {path}" for verb, path in unknown)
    )


def test_every_request_call_in_the_module_is_read_by_this_file() -> None:
    """The reader's own honesty: no call may be skipped and still leave the suite green."""

    source = API_MODULE.read_text(encoding="utf-8")
    spans = _call_arguments(source)

    assert len(spans) == source.count(CALL_MARKER), (
        f"api.ts contains {source.count(CALL_MARKER)} `request<` occurrences but this "
        f"file parsed {len(spans)}; a call the reader cannot see is a call it cannot check"
    )


def test_the_prefix_the_browser_prepends_is_the_prefix_the_app_mounts(settings: Settings) -> None:
    """A second string in the same seam: the client's base and the server's mount point.

    Changing ``API_V1_PREFIX`` renames every route in one line. The backend suite reads
    the routes through ``TestClient``, which follows the setting; the frontend prepends
    its own literal and would keep calling the old prefix, so every screen would 404
    while both suites stayed green. ``next.config.ts`` proxies ``/api/*`` wholesale, so
    nothing else names the version.
    """

    source = API_MODULE.read_text(encoding="utf-8")
    base = BASE_DEFAULT.search(source)

    assert base is not None, "api.ts no longer reads NEXT_PUBLIC_API_BASE with a fallback"
    assert base.group(1).rstrip("/") == settings.api_v1_prefix.rstrip("/"), (
        f"api.ts defaults to {base.group(1)!r} while the app mounts its routers at "
        f"{settings.api_v1_prefix!r}"
    )


def test_the_liveness_probe_calls_a_route_that_exists_outside_the_versioned_prefix(
    settings: Settings,
) -> None:
    """``probeHealth`` is the one call ``request`` does not make, so it is read separately."""

    source = API_MODULE.read_text(encoding="utf-8")
    direct = DIRECT_FETCH.findall(source)

    assert direct, "api.ts no longer calls fetch() directly; the probe has moved"
    declared = _routes(settings)
    for path in direct:
        assert ("GET", _shape(path)) in declared, (
            f"probeHealth calls {path!r}, which no route answers; the health indicator "
            f"would then report the API down while it is up"
        )
