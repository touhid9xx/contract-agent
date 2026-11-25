"""The three places a count can enter SQL, checked as one family instead of one at a time.

``D10-149`` and ``D10-150`` fixed a defect three times before it stayed fixed. The defect is
that a negative ``LIMIT`` is not "no rows": SQLite and MySQL both read it as *unbounded*, so the
most cautious-looking invocation is the one that removes the cap. It was measured, not argued —
``outbox-prune --limit -1`` deleted all 29 settled rows, parked ones included — and it was reachable
from three different kinds of caller:

* a **CLI flag**, ``--limit`` on five subcommands, refused by ``_count_arg`` at parse time;
* an **HTTP query parameter**, ``limit`` on five routes, refused by ``Query(ge=1)``;
* a **setting**, the batch sizes and scan limits a worker passes down, bounded by
  ``Field(gt=0)``.

Each of those guards is one keyword argument in one place, and each was added by a separate
commit. That is the shape this file exists to defend: a keyword argument is invisible to every
other test in the suite, so the *sixth* route, the *next* subcommand and the *next* setting are
where the defect comes back, and nothing would notice. ``test_outbox_replay.py`` already pins the
CLI half across all five subcommands; this file pins the other two and the library defaults the
two rely on.

**Why the OpenAPI schema and not the source.** ``Query(ge=1)`` is a keyword on a keyword
argument, and reading it out of the AST would be a second copy of the parser's opinion. The
schema is generated *by FastAPI* from the declarations it will actually enforce, so a route that
declares ``le=200`` and forgets ``ge`` shows up as a parameter with no ``minimum`` — the same
thing a caller would experience. It also needs no authentication, because the contract is
declared before any request is served.

**Why the settings are read off the model.** ``Settings`` is the single source of truth for
every number a worker uses, and ``field.metadata`` holds the constraint Pydantic enforces at
instantiation. A hand-written list of settings would be a third copy of the same names.

**The fourth boundary reads bodies, not declarations.** A runtime count arrives at a repository
from a worker, not from a parser or a schema, and ``D10-151`` recorded the hole honestly: nothing
enumerated the functions that hand one to SQL, so a guard could be — and in ``claim_due`` was —
missing without any of the three checks above noticing. The last test here closes that by parsing
``app/`` and requiring that every function which passes its own ``limit`` into ``.limit()`` refuses
a non-positive value first. It is the one check in this file that would have failed on the tree it
was written against: fourteen of the twenty counts in the repository layer were unguarded.

**What this does not cover, stated rather than implied.** It does not prove a guard is
*sufficient* — a function could refuse ``limit <= 0`` and then hand SQL ``limit - 1`` — and it says
nothing about whether a limit's *value* is sensible: ``--limit 1000`` on a table of 1001 rows is
bounded and still a decision the operator made.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings

#: Names the repository layer uses for the page sizes it hands to SQL. Collected by suffix rather
#: than by list so a new ``DEFAULT_SOMETHING_LIMIT`` is covered the moment it is added, which is
#: the whole point of a drift guard.
LIMIT_CONSTANT_SUFFIXES = ("_LIMIT",)

#: Modules that own a `limit` default. Named explicitly because ``__all__`` re-exports would
#: otherwise have this test import the same constant several times and call that coverage.
LIMIT_CONSTANT_MODULES = (
    "app.db.repositories.agent",
    "app.db.repositories.contracts",
    "app.db.repositories.escalations",
    "app.db.repositories.fields",
    "app.db.repositories.notifications",
    "app.db.repositories.outbox",
)

#: How many ``limit`` query parameters the API declares today. Pinned so that deleting one cannot
#: make the route check below pass by finding nothing; if a route is genuinely removed, this number
#: moving is the reminder to confirm the guard moved with it.
EXPECTED_LIMIT_ROUTES = 5

#: How many numeric settings exist today, for the same anti-vacuity reason: the check is a loop over
#: whatever the model declares, so it would pass silently against a model that declared nothing.
EXPECTED_NUMERIC_SETTINGS = 50

#: How many functions hand a caller's count to SQL today — twenty of them, across six repository
#: modules. Pinned so that a rename that quietly removes one from the scan is a failure rather than
#: one fewer thing checked; the number moving is the reminder to confirm the guard moved with it.
EXPECTED_LIMIT_FORWARDERS = 20


def _limit_parameters(app: object) -> list[tuple[str, str, dict[str, object]]]:
    """Every ``limit`` query parameter the app declares, as ``(verb, path, schema)``."""
    schema = app.openapi()  # type: ignore[attr-defined]
    found: list[tuple[str, str, dict[str, object]]] = []
    for path, operations in sorted(schema["paths"].items()):
        for verb, operation in sorted(operations.items()):
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "limit" and parameter.get("in") == "query":
                    found.append((verb.upper(), path, parameter.get("schema") or {}))
    return found


def test_every_limit_query_parameter_declares_a_positive_minimum(client: TestClient) -> None:
    """A route that takes ``limit`` must refuse zero and below, in the schema it publishes.

    ``prune`` is the case that made this concrete: the delete path was the dangerous one, and the
    guard had to be written three times because it was reasoned about one function at a time. A
    route is the same argument with a different caller — the value arrives from a query string and
    is handed straight to ``.limit()`` by five different routers.
    """
    parameters = _limit_parameters(client.app)
    assert len(parameters) == EXPECTED_LIMIT_ROUTES, [(verb, path) for verb, path, _ in parameters]

    unbounded: list[tuple[str, str, dict[str, object]]] = []
    for verb, path, schema in parameters:
        minimum = schema.get("minimum")
        if isinstance(minimum, int) and minimum >= 1:
            continue
        unbounded.append((verb, path, schema))
    assert unbounded == [], "a limit query parameter without a positive minimum"

    for verb, path, schema in parameters:
        # A cap is what makes the page size a decision rather than an accident: without a maximum
        # the caller picks the size of the response, which is the other half of the same argument.
        assert isinstance(schema.get("maximum"), int), (verb, path, schema)


def test_every_numeric_setting_is_bounded_below() -> None:
    """Every number a worker reads from the environment must state a floor.

    Not all of them are limits. ``escalation_no_response_days`` decides which contracts are silent
    and ``quiet_hours_start`` decides when nobody is contacted, and a negative value for either is
    not a smaller number of anything — it inverts the window. The floor is what makes "set it
    lower" different from "set it backwards".
    """
    numeric = [
        (name, field)
        for name, field in Settings.model_fields.items()
        if field.annotation in (int, float)
    ]
    assert len(numeric) >= EXPECTED_NUMERIC_SETTINGS, len(numeric)

    unbounded = []
    for name, field in numeric:
        floors = [
            getattr(meta, "ge", None)
            if getattr(meta, "ge", None) is not None
            else getattr(meta, "gt", None)
            for meta in field.metadata
        ]
        floor = next((value for value in floors if value is not None), None)
        if floor is None or floor < 0:
            unbounded.append((name, floor))
    assert unbounded == [], "a numeric setting with no floor, or a floor below zero"


@pytest.mark.parametrize("module_name", LIMIT_CONSTANT_MODULES)
def test_every_limit_default_is_positive(module_name: str) -> None:
    """The library defaults are the fourth boundary, and they are not guarded.

    ``replay_dead_lettered`` and friends default their ``limit`` to a module constant, so the value
    a caller who passes nothing inherits is this number. ``D10-150``'s guards sit *inside* those
    functions, so a non-positive default would now return nothing rather than everything — a
    bounded failure instead of an unbounded one — but a default of zero still means a command that
    silently does nothing, which is the kind of success that wastes an operator's afternoon.
    """
    module = importlib.import_module(module_name)
    names = sorted(
        name
        for name in vars(module)
        if name.endswith(LIMIT_CONSTANT_SUFFIXES) and not name.startswith("_")
    )
    assert names != [], module_name

    defaults = {name: getattr(module, name) for name in names}
    assert all(isinstance(value, int) and value > 0 for value in defaults.values()), defaults


def _count_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names this function accepts that could carry a row count."""
    arguments = function.args
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args)}
    names |= {argument.arg for argument in arguments.kwonlyargs}
    return {name for name in names if name == "limit" or name.endswith("_limit")}


def _hands_count_to_sql(
    function: ast.FunctionDef | ast.AsyncFunctionDef, parameters: set[str]
) -> set[str]:
    """Which of ``parameters`` this function passes into a ``.limit(...)``-shaped call."""
    handed: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        attribute = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        forwards = attribute in {"limit", "fetchmany", "slice"} or any(
            keyword.arg in {"limit", "count"}
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id in parameters
            for keyword in node.keywords
        )
        if not forwards:
            continue
        handed |= {
            argument.id
            for argument in node.args
            if isinstance(argument, ast.Name) and argument.id in parameters
        }
    return handed


def _refuses(function: ast.FunctionDef | ast.AsyncFunctionDef, parameter: str) -> bool:
    """Does this function branch on, or clamp, a non-positive ``parameter``?"""
    for node in ast.walk(function):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            test = node.test
            if isinstance(test.left, ast.Name) and test.left.id == parameter:
                for operator, right in zip(test.ops, test.comparators, strict=True):
                    if (
                        isinstance(right, ast.Constant)
                        and right.value == 0
                        and isinstance(operator, (ast.LtE, ast.Lt, ast.Eq))
                    ):
                        return True
        # ``max(0, limit)`` is the same refusal written as a clamp instead of a branch.
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if (
                name in {"max", "min"}
                and any(
                    isinstance(argument, ast.Name) and argument.id == parameter
                    for argument in node.args
                )
                and any(
                    isinstance(argument, ast.Constant) and argument.value in (0, 1)
                    for argument in node.args
                )
            ):
                return True
    return False


def test_every_function_that_hands_a_count_to_sql_refuses_a_non_positive_one() -> None:
    """The repository is the last place a count can be refused, so it has to refuse it.

    ``D10-150`` put guards inside three ``outbox`` functions and left ``claim_due`` alone, on the
    reasoning that its only caller passes a setting the config already bounds. That is true of the
    *wired* path and of nothing else: ``OutboxRelay`` takes ``batch_size`` as a constructor override,
    and ``expiry.py`` states the convention this violates — "Settings forbid a zero batch, but the
    constructor takes an override and the repository guards the case". By that rule every count a
    worker can hand to a repository needs a guard in the repository, because the constructor is the
    boundary and it does not check.

    The failure is not uniform, which is why the count is worth pinning: on ``claim_due`` a negative
    limit leases every due row in one statement, which the relay then publishes against leases that
    expire underneath it; on ``claim_silent_contracts`` it locks and escalates every silent contract
    in one transaction; on ``contracts_for_contact`` it uncaps the ``MAX_OPT_OUT_CONTRACTS`` ceiling
    that exists to make an unauthenticated webhook bounded.
    """
    root = Path(__file__).resolve().parents[1]
    checked: list[str] = []
    unguarded: list[str] = []

    for path in sorted((root / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = _count_parameters(node)
            if not parameters:
                continue
            for parameter in sorted(_hands_count_to_sql(node, parameters)):
                where = (
                    f"{path.relative_to(root).as_posix()}:{node.lineno} {node.name}({parameter})"
                )
                checked.append(where)
                if not _refuses(node, parameter):
                    unguarded.append(where)

    assert unguarded == [], "a function hands a count to SQL without refusing a non-positive one"
    # Pinned so that deleting a function cannot make this pass by finding nothing to check.
    assert len(checked) == EXPECTED_LIMIT_FORWARDERS, checked
