"""Operator entry points (F5, F7, gap 20).

Run from ``backend/``::

    python -m app.cli outbox-replay --list
    python -m app.cli outbox-replay --dry-run
    python -m app.cli outbox-replay --ids 12,14
    python -m app.cli extract --contract-id 12 --text-file contract.txt
    python -m app.cli reseal-fields --dry-run
    python -m app.cli reseal-fields

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
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.crypto import (
    DecryptionError,
    MissingEncryptionKeyError,
    build_cipher,
    is_sealed,
)
from app.core.logging import configure_logging
from app.db.repositories.contracts import contract_for_operator
from app.db.repositories.fields import DEFAULT_RESEAL_LIMIT, fields_for_reseal
from app.db.repositories.outbox import (
    DEFAULT_REPLAY_LIMIT,
    DeadLetterReplay,
    dead_lettered,
    replay_dead_lettered,
)
from app.db.session import build_engine, build_session_factory, session_scope
from app.workers.extraction import ExtractionUnavailableError, extract_contract
from app.workers.extractors import build_extractor

log = structlog.get_logger(__name__)

EXIT_OK = 0
#: Explicit ids were given and at least one is not in the dead-letter state. A
#: wrong id is an operator mistake worth a non-zero code so a script notices.
EXIT_NO_MATCH = 1
#: Extraction stored nothing: the document yielded no usable field, or every
#: candidate was skipped or discarded. Distinct from ``EXIT_FAILED`` because the
#: run itself succeeded — the *result* is what needs a human (STATUS.md gap 11).
EXIT_NOTHING_EXTRACTED = 2
#: The run could not start or could not finish: missing key, unreadable document,
#: or a backend that did not answer.
EXIT_FAILED = 3
#: Every value that needed sealing was left alone because its stored token cannot
#: be read with the configured key. Non-zero because it almost always means the
#: operator ran with the wrong ``ENCRYPTION_KEY``, and silently "succeeding" while
#: a database stays half-migrated is the failure mode this command exists to end.
EXIT_UNREADABLE = 4

#: The two columns on :class:`~app.db.models.ExtractedField` that hold PII, and so
#: must be sealed. Named once, here, so "did we miss a column?" has one answer.
_RESEAL_COLUMNS = ("raw_value", "normalized_value")


def _ids_arg(value: str) -> list[int]:
    """Parse ``--ids 12,14`` into ``[12, 14]``."""
    try:
        return [int(part) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected a comma-separated list of ids, e.g. --ids 12,14"
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

    extract = commands.add_parser(
        "extract",
        help="extract field values from a contract document into reviewable rows",
        description=(
            "Read a contract document's text and write one ExtractedField per "
            "field the configured backend finds. Values are encrypted before "
            "they are stored, and nothing is ever written as 'approved'."
        ),
    )
    extract.add_argument(
        "--contract-id",
        type=int,
        required=True,
        metavar="N",
        help="id of the contract the document belongs to",
    )
    extract.add_argument(
        "--text-file",
        required=True,
        metavar="PATH",
        help=(
            "plain-text document to extract from (UTF-8). There is no upload or "
            "text storage yet, so the operator supplies it, which is also why "
            "the file may be a PDF's extracted text rather than a PDF"
        ),
    )
    extract.set_defaults(handler=_extract)

    reseal = commands.add_parser(
        "reseal-fields",
        help="encrypt field values that were stored as plaintext",
        description=(
            "Seal extracted-field values written before gap 20 closed: "
            "'raw_value' was always encrypted, 'normalized_value' was not. "
            "Values that are already sealed are left alone, and a value that "
            "cannot be read with the configured ENCRYPTION_KEY is reported "
            "rather than re-encrypted."
        ),
    )
    reseal.add_argument(
        "--ids",
        type=_ids_arg,
        default=None,
        metavar="N[,N...]",
        help="seal only these field ids (default: every row, up to --limit)",
    )
    reseal.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RESEAL_LIMIT,
        metavar="N",
        help=f"maximum rows to examine in one run (default: {DEFAULT_RESEAL_LIMIT})",
    )
    reseal.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be sealed without writing anything",
    )
    reseal.set_defaults(handler=_reseal_fields)
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


def _extract(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    contract_id = int(args.contract_id)
    document = Path(str(args.text_file))

    try:
        text = document.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("extract_document_unreadable", path=str(document), error=str(exc))
        return EXIT_FAILED

    if not text.strip():
        log.error("extract_document_empty", path=str(document))
        return EXIT_FAILED

    # Refuse rather than truncate. A truncated document produces missing fields,
    # and "missing" is indistinguishable from "the contract never stated it" — the
    # exact ambiguity the review queue exists to resolve, so it must not be
    # manufactured here.
    limit = settings.extraction_max_document_chars
    if len(text) > limit:
        log.error("extract_document_too_long", characters=len(text), limit=limit)
        return EXIT_FAILED

    try:
        cipher = build_cipher(settings)
    except MissingEncryptionKeyError as exc:
        log.error("extract_refused_without_encryption_key", error=str(exc))
        return EXIT_FAILED

    extractor = build_extractor(settings)

    with session_scope(session_factory) as session:
        contract = contract_for_operator(session, contract_id)
        if contract is None:
            log.error("extract_contract_not_found", contract_id=contract_id)
            return EXIT_NO_MATCH

        # The tenant comes from the contract row, never from an argument.
        tenant_id = contract.tenant_id
        try:
            outcome = extract_contract(
                session,
                contract,
                text,
                extractor=extractor,
                cipher=cipher,
                review_threshold=settings.extraction_review_threshold,
            )
        except ExtractionUnavailableError as exc:
            log.error(
                "extract_backend_unavailable",
                contract_id=contract_id,
                backend=settings.extraction_backend,
                error=str(exc),
            )
            return EXIT_FAILED

    log.info(
        "contract_extracted",
        contract_id=outcome.contract_id,
        tenant_id=tenant_id,
        backend=settings.extraction_backend,
        candidates=outcome.candidates,
        created=outcome.created,
        updated=outcome.updated,
        skipped_reviewed=outcome.skipped_reviewed,
        discarded=outcome.discarded,
        found=list(outcome.found),
    )

    if outcome.produced_nothing:
        # Not an exception: the run worked and the answer is "nothing usable".
        # Loud, and a distinct exit code, because a silent empty extraction is the
        # failure mode this milestone exists to remove.
        log.warning(
            "contract_extraction_empty",
            contract_id=outcome.contract_id,
            backend=settings.extraction_backend,
            candidates=outcome.candidates,
            discarded=outcome.discarded,
        )
        return EXIT_NOTHING_EXTRACTED

    return EXIT_OK


def _reseal_fields(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    """``reseal-fields``: encrypt field values that were stored as plaintext.

    Gap 20 was that ``normalized_value`` held the same PII as ``raw_value`` but in
    the clear, while the decision and dispatch policies read it. The write paths
    now seal it; this is the repair path for rows written before they did.

    Three outcomes per value, and the distinction matters:

    - **already sealed and readable** — left alone. Re-sealing would wrap a token
      in a second token and lose the value for good.
    - **already sealed but unreadable** — reported, never touched, and the run
      exits :data:`EXIT_UNREADABLE`. This is what a wrong ``ENCRYPTION_KEY`` looks
      like, and continuing past it would rewrite a correct database with garbage.
    - **plaintext** — sealed. Recognised by *shape* rather than by a failed
      decryption, for the reason :func:`~app.core.crypto.is_sealed` documents.

    Nothing here logs a value: a field's ``id`` and column name identify the row
    to the operator without putting PII in the log stream.
    """
    field_ids: list[int] | None = args.ids
    # Explicit ids replace the limit rather than being filtered by it, exactly as
    # in ``outbox-replay``: silently dropping some of the requested rows would
    # look like success.
    limit = len(field_ids) if field_ids is not None else int(args.limit)
    dry_run = bool(args.dry_run)

    try:
        cipher = build_cipher(settings)
    except MissingEncryptionKeyError as exc:
        # Without a key every plaintext row would be reported as "cannot seal" and
        # nothing would change, so refuse before reading anything.
        log.error("reseal_refused_without_encryption_key", error=str(exc))
        return EXIT_FAILED

    sealed = 0
    already_sealed = 0
    unreadable = 0
    changed: list[int] = []

    with session_scope(session_factory) as session:
        rows = fields_for_reseal(session, field_ids=field_ids, limit=limit)
        for field in rows:
            sealed_here = 0
            for column in _RESEAL_COLUMNS:
                value: Any = getattr(field, column)
                if value is None:
                    continue
                if is_sealed(str(value)):
                    try:
                        cipher.decrypt(str(value))
                    except DecryptionError:
                        unreadable += 1
                        log.warning(
                            "reseal_value_unreadable",
                            field_id=field.id,
                            column=column,
                        )
                        continue
                    already_sealed += 1
                    continue
                if not dry_run:
                    setattr(field, column, cipher.encrypt(str(value)))
                sealed += 1
                sealed_here += 1
            if sealed_here:
                changed.append(field.id)

    log.info(
        "fields_resealed",
        dry_run=dry_run,
        examined=len(rows),
        sealed=sealed,
        already_sealed=already_sealed,
        unreadable=unreadable,
        field_ids=changed,
    )

    if field_ids is not None:
        missing = sorted(set(field_ids) - {field.id for field in rows})
        if missing:
            # Same reasoning as ``outbox-replay``: a wrong id usually means the
            # operator read a different database, not that the row is fine.
            log.warning("reseal_ids_not_found", ids=missing)
            return EXIT_NO_MATCH

    if unreadable:
        log.error(
            "reseal_left_values_unreadable",
            unreadable=unreadable,
            hint="check that ENCRYPTION_KEY is the key these rows were sealed with",
        )
        return EXIT_UNREADABLE

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
