"""Operator entry points (F5).

Run from ``backend/``::

    python -m app.cli outbox-replay --list
    python -m app.cli outbox-replay --dry-run
    python -m app.cli outbox-replay --ids 12,14

Why a command rather than an HTTP endpoint: republishing parked events is a
privileged, rare operation. Over HTTP it would become a "republish any tenant's
parked events" verb reachable with one admin token, inside the process that
``docker-compose.yml`` deliberately does **not** give publishing rights to. An
operator with database access runs this next to the relay instead.

Why ``python -m``: ``app`` is not installed as a distribution (``pyproject.toml``
has no ``[project]`` table), so a console-script entry point would need
packaging. This works from ``backend/`` exactly like
``python -m app.workers.outbox_relay`` already does.

Output goes through structlog rather than ``print`` so an operator gets the same
redaction guarantees as the application (``app.core.logging``) — a provider
error string can echo a destination address, and these rows carry them.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.repositories.outbox import (
    DEFAULT_REPLAY_LIMIT,
    DeadLetterReplay,
    dead_lettered,
    replay_dead_lettered,
)
from app.db.session import build_engine, build_session_factory, session_scope

log = structlog.get_logger(__name__)

EXIT_OK = 0
#: Explicit ids were given and at least one is not in the dead-letter state. A
#: wrong id is an operator mistake worth a non-zero code so a script notices.
EXIT_NO_MATCH = 1


def _ids_arg(value: str) -> list[int]:
    """Parse ``--ids 12,14`` into ``[12, 14]``."""
    try:
        return [int(part) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected a comma-separated list of outbox ids, e.g. --ids 12,14"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Operator commands for the contract-expiry notification agent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser(
        "outbox-replay",
        help="inspect or requeue outbox rows parked in the dead-letter state",
        description=(
            "Requeue rows the relay parked after exhausting its retry budget. "
            "Only 'failed' rows are touched; replaying resets the attempt "
            "counter so the row gets a fresh budget."
        ),
    )
    replay.add_argument(
        "--list",
        action="store_true",
        help="print parked rows and change nothing",
    )
    replay.add_argument(
        "--ids",
        type=_ids_arg,
        default=None,
        metavar="N[,N...]",
        help="replay only these outbox ids (default: every parked row, up to --limit)",
    )
    replay.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_REPLAY_LIMIT,
        metavar="N",
        help=f"maximum rows to touch in one run (default: {DEFAULT_REPLAY_LIMIT})",
    )
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be requeued without writing anything",
    )
    replay.set_defaults(handler=_outbox_replay)
    return parser


def _describe(replays: Sequence[DeadLetterReplay]) -> list[dict[str, Any]]:
    """The pre-replay state, which the rows themselves no longer carry."""
    return [
        {
            "id": replay.id,
            "event_type": replay.event_type,
            "attempts": replay.attempts,
            "last_error": replay.last_error,
        }
        for replay in replays
    ]


def _outbox_replay(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    """``outbox-replay``: inspect or requeue parked rows."""
    ids: list[int] | None = args.ids
    # With explicit ids the limit is meaningless and would silently drop some of
    # the requested rows, so it is replaced by "exactly these".
    limit = len(ids) if ids is not None else int(args.limit)

    with session_scope(session_factory) as session:
        if args.list:
            parked = dead_lettered(session, limit=limit)
            log.info(
                "outbox_dead_letter",
                parked=len(parked),
                rows=[
                    {
                        "id": row.id,
                        "event_type": row.event_type,
                        "attempts": row.attempts,
                        "last_error": row.last_error,
                    }
                    for row in parked
                ],
            )
            return EXIT_OK

        replays = replay_dead_lettered(
            session,
            event_ids=ids,
            limit=limit,
            dry_run=bool(args.dry_run),
        )

    if not replays:
        # Steady state: nothing is parked. Not an error, so a periodic run does
        # not page anyone -- unless explicit ids were asked for, below.
        log.info("outbox_replay_nothing_parked", dry_run=bool(args.dry_run))
    else:
        log.info(
            "outbox_replay",
            dry_run=bool(args.dry_run),
            replayed=len(replays),
            lease_seconds=settings.outbox_lease_seconds,
            events=_describe(replays),
        )

    if ids is not None:
        wanted = set(ids)
        missing = sorted(wanted - {replay.id for replay in replays})
        if missing:
            # Naming an id that is not parked is not harmless: it usually means
            # the operator read a different database (or a stale export).
            log.warning("outbox_replay_ids_not_parked", ids=missing)
            return EXIT_NO_MATCH

    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """Entry point. ``settings``/``session_factory`` are injectable for tests.

    Mirrors :func:`app.main.create_app`: the production path builds its own
    engine from settings, and the test suite points it at an in-memory database
    instead of needing a MySQL container for a CLI assertion.
    """
    args = build_parser().parse_args(argv)

    app_settings = settings or get_settings()
    configure_logging(level=app_settings.log_level, json_logs=app_settings.json_logs)

    engine: Engine | None = None
    if session_factory is None:
        engine = build_engine(app_settings)
        factory = build_session_factory(engine)
    else:
        factory = session_factory

    try:
        handler: Any = args.handler
        return int(handler(args, factory, app_settings))
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
