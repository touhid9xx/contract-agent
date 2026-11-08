"""Operator entry points (F5, F7, gap 20).

Run from ``backend/``::

    python -m app.cli outbox-replay --list
    python -m app.cli outbox-replay --dry-run
    python -m app.cli outbox-replay --ids 12,14
    python -m app.cli extract --contract-id 12 --text-file contract.txt
    python -m app.cli reseal-fields --dry-run
    python -m app.cli reseal-fields
    python -m app.cli consent-import --file consent.csv --dry-run
    python -m app.cli consent-import --file consent.csv
    python -m app.cli doctor
    python -m app.cli create-admin --tenant-slug acme --tenant-name Acme --email ops@acme.test

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
import csv
import getpass
import io
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.core.attention import PII_COLUMNS, attention_report
from app.core.config import Settings, get_settings
from app.core.crypto import (
    DecryptionError,
    FieldCipher,
    MissingEncryptionKeyError,
    build_cipher,
    is_sealed,
)
from app.core.document_text import DocumentTextError, extract_document_text
from app.core.documents import DocumentStoreError, build_document_store
from app.core.logging import configure_logging
from app.core.middleware import correlation_id_or_new
from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password
from app.db.enums import UserRole
from app.db.models import Contract, Tenant
from app.db.repositories.audit import (
    ACTION_CONSENT_UPDATED,
    ACTION_USER_CREATED,
    ACTOR_SYSTEM,
    AUDIT_ENTITY_CONTRACT,
    AUDIT_ENTITY_USER,
    AuditLogRepository,
)
from app.db.repositories.contracts import (
    CONSENT_FLAG_COLUMNS,
    apply_consent,
    consent_flags,
    consent_state,
    contract_for_operator,
)
from app.db.repositories.fields import DEFAULT_RESEAL_LIMIT, fields_for_reseal
from app.db.repositories.outbox import (
    DEFAULT_REPLAY_LIMIT,
    DeadLetterReplay,
    dead_lettered,
    replay_dead_lettered,
)
from app.db.repositories.users import (
    IdentityDirectory,
    UserRepository,
    is_plausible_email,
    normalize_email,
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
#: Something needs a human and nothing else was going to say so: an outbox row
#: parked while a transport was configured to deliver it, a notification that failed
#: or was suppressed, or a field value still stored in the clear. Non-zero so a cron
#: entry or a CI step fails loudly on it — a zero exit code would make the one state
#: that needs attention the one state that looks exactly like a clean database.
#:
#: A parked row under ``EVENT_TRANSPORT=none`` is deliberately *not* one of these
#: (``D10-39``): with the bus switched off the relay parks everything by design, and
#: a command that failed on that would fail every run of a default installation
#: until an operator learned to ignore it. Those parks are counted and named in the
#: log, and no exit code is spent on them.
EXIT_ATTENTION = 5
#: The account ``create-admin`` was asked for already exists, and nothing was
#: changed. Distinct from ``EXIT_FAILED`` because a provisioning script has to be
#: able to tell "this was already done" from "this was rejected" — only one of those
#: is worth stopping a deployment for. It is a refusal rather than a quiet success
#: on purpose: silently reusing the existing account would leave the caller believing
#: it had just set that account's password, which it did not do and must not.
EXIT_EXISTS = 6

#: The two columns on :class:`~app.db.models.ExtractedField` that hold PII, and so
#: must be sealed, are named once in :data:`app.core.attention.PII_COLUMNS` — not
#: here. The set moved when the attention check gained a second caller
#: (``AttentionWatch``); ``reseal-fields`` still seals it and ``doctor`` still
#: verifies it, so "did we miss a column?" still has exactly one answer.

#: The header a consent sheet must carry, in the order :data:`ConsentSheetRow`
#: documents it.
#:
#: Built from :data:`~app.db.repositories.contracts.CONSENT_FLAG_COLUMNS` rather
#: than retyped, so a fifth channel flag cannot be added to the sheet's writer
#: without appearing in its reader. Note there is no contact *value* column: the
#: address and phone number live in the encrypted fields, and a sheet that carried
#: them would be a new copy of the PII that F7 and gap 20 sealed.
_CONSENT_SHEET_COLUMNS = ("contract_id", *CONSENT_FLAG_COLUMNS)

#: Accepted spellings for a consent cell, compared after stripping and lowering.
#:
#: Deliberately a closed set rather than ``bool(cell)``: ``bool("false")`` is
#: ``True``, and a typo like ``ture`` would import as consent granted. Getting this
#: wrong is not a formatting nit — it is a customer being contacted who said no.
_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})

#: Longest slug and name the ``tenants`` columns accept, so an over-long argument
#: is refused here rather than as a driver error after the tenant row is flushed.
_MAX_SLUG_LENGTH = 64
_MAX_TENANT_NAME_LENGTH = 200

#: What a slug may contain, checked **after** the input is lower-cased.
#:
#: The slug is half of a login credential: ``POST /auth/login`` resolves it with
#: ``strip().lower()``, so a tenant stored with characters an operator cannot
#: reproduce at the login form is a tenant nobody can log into. Restricting the
#: shape at the one place tenants get created is cheaper than discovering that in
#: production. The first character is alphanumeric so a slug can never be read as a
#: flag, and case is normalised rather than rejected: ``--tenant-slug Acme`` stores
#: ``acme``, which is the value the login path will look up.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class ConsentSheetRow:
    """One validated line of a consent sheet.

    The field names are the CSV column names, so the parser, the reader and the
    writer all describe the same shape. ``line`` is kept because the only useful
    thing to tell an operator about a bad sheet is where in the file it went
    wrong, and the row itself has no idea it came from a file at all.
    """

    line: int
    contract_id: int
    consent_email: bool
    consent_sms: bool
    consent_voice: bool
    do_not_contact: bool

    def flags(self) -> dict[str, bool]:
        """The four consent booleans, keyed by column name.

        Used for the "does the sheet disagree with the row?" comparison, which
        has to be the *same* four columns
        :func:`~app.db.repositories.contracts.apply_consent` writes — hence the
        iteration over ``CONSENT_FLAG_COLUMNS`` rather than a literal list.
        """
        return {column: bool(getattr(self, column)) for column in CONSENT_FLAG_COLUMNS}


def _consent_bool(cell: str) -> bool | None:
    """Parse one consent cell, or ``None`` when it is not a recognisable boolean."""
    word = cell.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return None


def _read_consent_sheet(path: Path) -> tuple[list[ConsentSheetRow], list[str]]:
    """Parse a whole consent sheet, collecting every problem it has.

    Returns the usable rows *and* the problems, and the caller writes nothing at
    all while the problem list is non-empty. Validating the file before touching
    the database is the point: an import that stops at the first bad line has
    already applied the good ones, leaving the database holding half a sheet with
    nothing recording which half.

    ``utf-8-sig`` because these sheets come out of spreadsheet software, which
    writes a byte-order mark. Without it the first column arrives spelled
    ``\\ufeffcontract_id`` and the sheet is rejected for a header the operator can
    plainly see.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"could not read {path}: {exc}"]
    except UnicodeDecodeError as exc:
        return [], [f"{path} is not UTF-8 text: {exc}"]

    reader = csv.DictReader(io.StringIO(text))
    header = list(reader.fieldnames or [])
    missing_columns = [column for column in _CONSENT_SHEET_COLUMNS if column not in header]
    if missing_columns:
        return [], [
            f"missing column(s) {', '.join(missing_columns)}; "
            f"expected the header {','.join(_CONSENT_SHEET_COLUMNS)}"
        ]

    rows: list[ConsentSheetRow] = []
    problems: list[str] = []
    seen: dict[int, int] = {}

    for record in reader:
        where = f"line {reader.line_num}"
        if None in record or any(value is None for value in record.values()):
            # ``DictReader`` files extra cells under a ``None`` key and pads a
            # short row with ``None`` values; either means the sheet is ragged.
            problems.append(f"{where}: cell count does not match the header")
            continue

        raw_id = str(record["contract_id"] or "").strip()
        try:
            contract_id = int(raw_id)
        except ValueError:
            problems.append(f"{where}: contract_id {raw_id!r} is not a whole number")
            continue

        wanted: dict[str, bool] = {}
        for column in CONSENT_FLAG_COLUMNS:
            parsed = _consent_bool(str(record[column] or ""))
            if parsed is None:
                # A blank cell lands here too, deliberately: consent is stated, so
                # an empty cell is an unanswered question, not a "no". Treating it
                # as false would silently revoke a channel the sheet never
                # mentioned.
                problems.append(
                    f"{where}: {column} {record[column]!r} is not a boolean "
                    f"(expected {'/'.join(sorted(_TRUE_WORDS))} or "
                    f"{'/'.join(sorted(_FALSE_WORDS))})"
                )
            else:
                wanted[column] = parsed
        if len(wanted) != len(CONSENT_FLAG_COLUMNS):
            continue

        if contract_id in seen:
            # Two lines disagreeing about one contract has no defensible
            # resolution order, and picking the last one would make the import's
            # effect depend on row order.
            problems.append(
                f"{where}: contract_id {contract_id} already appears on line {seen[contract_id]}"
            )
            continue

        seen[contract_id] = reader.line_num
        rows.append(
            ConsentSheetRow(
                line=reader.line_num,
                contract_id=contract_id,
                consent_email=wanted["consent_email"],
                consent_sms=wanted["consent_sms"],
                consent_voice=wanted["consent_voice"],
                do_not_contact=wanted["do_not_contact"],
            )
        )

    return rows, problems


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
        metavar="PATH",
        help=(
            "plain-text document to extract from (UTF-8). Omit it to read the "
            "document the contract already has stored, which may be a PDF "
            "(gap 12); the two are alternatives because a contract has one "
            "authoritative document and a loose file is the operator overriding it"
        ),
    )
    extract.set_defaults(handler=_extract)

    reseal = commands.add_parser(
        "reseal-fields",
        help="seal plaintext field values, and finish a key rotation",
        description=(
            "Two jobs on the same rows. Seals extracted-field values written "
            "before gap 20 closed: 'raw_value' was always encrypted, "
            "'normalized_value' was not. And re-seals values still wrapped in a "
            "key listed in ENCRYPTION_KEY_PREVIOUS, which is the second half of "
            "rotating ENCRYPTION_KEY. Values sealed with the current key are left "
            "alone, and a value no configured key can read is reported rather "
            "than re-encrypted."
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

    doctor = commands.add_parser(
        "doctor",
        help="report rows that need a human and are otherwise silent",
        description=(
            "Read-only. Counts the states that need an operator and currently "
            "raise no alert: outbox events parked in the dead-letter state, "
            "notifications that failed or were suppressed, and extracted-field "
            "values still stored as plaintext. Exits non-zero when anything is "
            "found, so a scheduled run can act on it."
        ),
    )
    doctor.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RESEAL_LIMIT,
        metavar="N",
        help=(
            "maximum field rows to scan for unsealed values (default: "
            f"{DEFAULT_RESEAL_LIMIT}); a full page makes the count a floor"
        ),
    )
    doctor.set_defaults(handler=_doctor)

    consent = commands.add_parser(
        "consent-import",
        help="apply a consent sheet (CSV) to many contracts in one run",
        description=(
            "Record channel consent and opt-out state for many contracts from a "
            "CSV sheet. The header must be "
            f"{','.join(_CONSENT_SHEET_COLUMNS)}; each cell is true/false, "
            "yes/no or 1/0. The sheet is the complete state for every contract it "
            "names, so an all-false line revokes consent. Nothing is written "
            "unless every line parses."
        ),
    )
    consent.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help=(
            "consent sheet to read (UTF-8; a spreadsheet BOM is tolerated). A "
            "byte-order mark, CRLF endings and quoted cells are all handled"
        ),
    )
    consent.add_argument(
        "--dry-run",
        action="store_true",
        help="report which contracts would change without writing anything",
    )
    consent.set_defaults(handler=_consent_import)

    create = commands.add_parser(
        "create-admin",
        help="create the first admin of a tenant, so a fresh database can be logged into",
        description=(
            "Bootstrap an operator account. Without it a fresh database cannot be "
            "logged into at all: POST /auth/users needs an admin token, and an admin "
            "token needs a user (STATUS.md gap 10). Creates the tenant when its slug "
            "does not exist yet, then adds an ADMIN user to it. The password is read "
            "from a prompt (or from the first line of standard input with "
            "--password-stdin) and never from an argument, so it does not reach shell "
            "history, the process list or CI logs."
        ),
    )
    create.add_argument(
        "--tenant-slug",
        required=True,
        metavar="SLUG",
        help=(
            "tenant the account belongs to, as typed at the login form. Case is "
            "normalised to lower case; letters, digits and hyphens only"
        ),
    )
    create.add_argument(
        "--tenant-name",
        default=None,
        metavar="NAME",
        help=(
            "display name for the tenant. Required only when the slug does not exist "
            "yet; on an existing tenant it must match the stored name or the run is "
            "refused"
        ),
    )
    create.add_argument(
        "--email",
        required=True,
        metavar="ADDRESS",
        help="login address for the new account",
    )
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help=(
            "read the password from the first line of standard input instead of "
            "prompting, for scripted provisioning and tests"
        ),
    )
    create.set_defaults(handler=_create_admin)
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


def _text_file_contents(document: Path) -> str | None:
    """The text in an operator-supplied file, or ``None`` once the reason is logged."""
    try:
        return document.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("extract_document_unreadable", path=str(document), error=str(exc))
        return None


def _stored_document_text(
    contract: Contract, settings: Settings, cipher: FieldCipher
) -> str | None:
    """The contract's own stored document, loaded and parsed (gap 12).

    This is the content reader ``storage_path`` never had: until now the operator
    had to extract a PDF's text by hand and pass it in, which meant the bytes the
    system had carefully sealed were only ever read by ``has_document``.

    Four refusals rather than one, because the fix differs: no document on the row
    (nothing to parse), bytes the store does not hold (a volume or path problem),
    bytes that will not decrypt (a key-ring problem — ``DecryptionError`` is the
    cipher's, not the store's), and bytes this build cannot parse (a format
    problem). Each is logged with the field *names* that identify it and no part of
    the document, which is PII.
    """
    if contract.storage_path is None:
        log.error("extract_no_document_stored", contract_id=contract.id)
        return None
    store = build_document_store(settings, cipher=cipher)
    try:
        payload = store.load(contract.storage_path)
    except (DocumentStoreError, DecryptionError) as exc:
        log.error(
            "extract_stored_document_unreadable",
            contract_id=contract.id,
            error=str(exc),
        )
        return None
    try:
        extracted = extract_document_text(payload)
    except DocumentTextError as exc:
        log.error("extract_document_unsupported", contract_id=contract.id, error=str(exc))
        return None
    log.info(
        "extract_document_parsed",
        contract_id=contract.id,
        kind=extracted.kind,
        pages=extracted.pages,
        characters=len(extracted.text),
    )
    return extracted.text


def _extract(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    contract_id = int(args.contract_id)
    #: ``None`` means "use the document the contract already has stored". The two
    #: sources are alternatives, not layers: a contract has one authoritative
    #: document, and a loose file is the operator overriding it deliberately.
    text_file = Path(str(args.text_file)) if args.text_file is not None else None

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

        # One source, then one set of checks over it. The file path and the stored
        # path differ only in where the bytes come from; what counts as an
        # unreadable, empty or oversized document must not depend on that.
        if text_file is None:
            source = "stored document"
            text = _stored_document_text(contract, settings, cipher)
        else:
            source = str(text_file)
            text = _text_file_contents(text_file)
        if text is None:
            return EXIT_FAILED

        if not text.strip():
            log.error("extract_document_empty", contract_id=contract_id, source=source)
            return EXIT_FAILED

        # Refuse rather than truncate. A truncated document produces missing fields,
        # and "missing" is indistinguishable from "the contract never stated it" — the
        # exact ambiguity the review queue exists to resolve, so it must not be
        # manufactured here.
        limit = settings.extraction_max_document_chars
        if len(text) > limit:
            log.error(
                "extract_document_too_long",
                characters=len(text),
                limit=limit,
                source=source,
            )
            return EXIT_FAILED

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

    Four outcomes per value, and the distinction matters:

    - **already sealed with the current key** — left alone. Re-sealing would wrap a
      token in a second token and lose the value for good.
    - **sealed with a retired key** — re-sealed under the current one. This is the
      second half of a rotation: ``ENCRYPTION_KEY_PREVIOUS`` keeps those rows
      *readable*, and this makes them stop needing it, which is what lets the
      operator clear the variable afterwards.
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
    rewrapped = 0
    already_sealed = 0
    unreadable = 0
    changed: list[int] = []

    with session_scope(session_factory) as session:
        rows = fields_for_reseal(session, field_ids=field_ids, limit=limit)
        for field in rows:
            sealed_here = 0
            for column in PII_COLUMNS:
                value: Any = getattr(field, column)
                if value is None:
                    continue
                if is_sealed(str(value)):
                    try:
                        replacement = cipher.rewrap(str(value))
                    except DecryptionError:
                        unreadable += 1
                        log.warning(
                            "reseal_value_unreadable",
                            field_id=field.id,
                            column=column,
                        )
                        continue
                    if replacement is None:
                        already_sealed += 1
                        continue
                    if not dry_run:
                        setattr(field, column, replacement)
                    rewrapped += 1
                    sealed_here += 1
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
        # Rotation progress: this reaching 0 is the signal that
        # ENCRYPTION_KEY_PREVIOUS can be cleared.
        rewrapped=rewrapped,
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
            hint=(
                "check that ENCRYPTION_KEY is the key these rows were sealed with, "
                "and that ENCRYPTION_KEY_PREVIOUS still carries any older one"
            ),
        )
        return EXIT_UNREADABLE

    return EXIT_OK


def _doctor(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    """``doctor``: report the states that need a human and raise no alert.

    The signed GATE 2 record names two of these as conditions of the sign-off
    rather than footnotes: a notification that parks ``failed`` or is
    ``suppressed`` raises no alert (gap 22's residue), and a field value written
    before gap 20 stays plaintext with nothing pointing at it. They are one defect
    wearing two hats — a state that needs a person and is invisible until somebody
    goes looking — so one read-only check answers both.

    What this **is**: the machine-readable half. A cron entry or a CI step runs it
    and acts on a non-zero exit code, which is the hook an external alerter needs
    and this stack did not have.

    What it is **not**: delivery. Nothing here pages anyone, and a command nobody
    schedules is worth little more than the log line it replaces. The condition is
    narrowed — the state is now observable and machine-checkable instead of
    requiring a hand-written query — not closed.

    The counting moved to :func:`~app.core.attention.attention_report` when
    :class:`~app.workers.attention_watch.AttentionWatch` became a second caller;
    what is *this command's* and stays here is the part that needs an exit code.
    Since ``D10-39`` the reading also distinguishes a bus that is switched off from
    one that is broken: an explained park is logged and **not** failed, because on
    ``EVENT_TRANSPORT=none`` every event parks by design and an alarm that fires on
    a default install is one nobody reads for long.

    Read-only, and deliberately usable **without** an ``ENCRYPTION_KEY``:
    :func:`~app.core.crypto.is_sealed` recognises a token by its shape, so a run
    with the wrong key still reports a plaintext database as plaintext. A check
    that needed the key would go quiet at exactly the moment the key was wrong.
    """
    limit = int(args.limit)

    with session_scope(session_factory) as session:
        report = attention_report(session, limit=limit, settings=settings)

    if report.field_scan_truncated:
        log.warning(
            "doctor_field_scan_truncated",
            scanned=report.fields_scanned,
            limit=limit,
            hint="the unsealed count is a floor; raise --limit above the field-table size",
        )

    if report.parks_explained:
        # Deliberately ``warning`` and not part of the exit code. With
        # EVENT_TRANSPORT=none the relay parks every event it claims, so this is the
        # designed shape of a default installation and not something to act on —
        # but the count is real and refusing to drain is real, so it is said out
        # loud rather than dropped. Whether these parks should exist at all is gap
        # 25's open question, which is a decision and not a patch (D10-39).
        log.warning(
            "doctor_bus_is_switched_off",
            event_transport=report.event_transport,
            outbox_parked=report.outbox_parked,
            effect="these events will not drain until a transport is configured",
        )

    if report.needs_attention:
        log.error("doctor_needs_attention", **report.as_dict())
        return EXIT_ATTENTION

    log.info("doctor_clean", **report.as_dict())
    return EXIT_OK


def _consent_import(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    """``consent-import``: apply a consent sheet to many contracts in one run.

    The bulk half of gap 18. The other half — a screen where an operator ticks the
    same four boxes for one contract — is still unbuilt, and this command is not a
    substitute for it: what it replaces is a hand-written ``UPDATE`` against the
    contracts table, which is what a consent sheet produces today. That UPDATE
    leaves no audit row and cannot tell a revocation from a re-grant.

    Four rules, each of which is a way the naive version goes wrong:

    - **The whole sheet is validated before anything is written.** A half-applied
      import leaves some customers consented and some not, with nothing pointing
      at the boundary. A sheet with one bad cell writes nothing and says which
      line.
    - **A row that already agrees writes nothing.** No version bump, no audit
      row, no ``consent_updated_at`` move — the same no-op rule as
      ``PUT /contracts/{id}/consent``, so re-running an export is not an incident.
      This is also what makes the command safe to re-run after a partial failure.
    - **A contract id that does not exist is reported, not skipped.** The rest of
      the sheet still applies — those rows are independent — but the run exits
      :data:`EXIT_NO_MATCH`, because "we recorded consent for 40 of 41 customers"
      must not look like success.
    - **The audit row says ``system``, not a person.** An operator with database
      access is not a principal, so there is no ``actor_id`` to record, and
      borrowing one would fabricate the fact the audit log exists to carry.

    Needs no ``ENCRYPTION_KEY``: consent is four booleans, and the contact values
    they permit live in the sealed fields, which this command neither reads nor
    writes.

    The ``contract_id`` is the table's primary key, so the sheet can name any
    tenant's contract — the same reach :func:`_extract` has, and the reason both
    are CLI commands rather than HTTP routes. The tenant each *write* lands in is
    taken from the contract row, never from the sheet.
    """
    sheet = Path(str(args.file))
    rows, problems = _read_consent_sheet(sheet)

    if problems:
        log.error(
            "consent_import_sheet_invalid",
            path=str(sheet),
            problems=problems,
            hint="nothing was written; fix the sheet and run it again",
        )
        return EXIT_FAILED

    if not rows:
        # A header with no data rows is not an import of zero consent updates: it
        # is almost always an export that produced nothing. Exiting 0 would let a
        # scheduled import report success every night for a year while no consent
        # was ever recorded.
        log.error(
            "consent_import_sheet_empty",
            path=str(sheet),
            hint=f"the sheet needs the header {','.join(_CONSENT_SHEET_COLUMNS)} and at least one row",
        )
        return EXIT_FAILED

    dry_run = bool(args.dry_run)
    # One correlation id for the whole run, so "what did this import change?" is a
    # single query over audit_log rather than a time range to guess at.
    correlation_id = correlation_id_or_new()
    updated: list[int] = []
    unchanged: list[int] = []
    missing: list[int] = []

    with session_scope(session_factory) as session:
        try:
            for row in rows:
                contract = contract_for_operator(session, row.contract_id)
                if contract is None:
                    missing.append(row.contract_id)
                    continue

                wanted = row.flags()
                if consent_flags(contract) == wanted:
                    unchanged.append(contract.id)
                    continue

                # Counted before the dry-run exit as well: a dry run answers "what
                # would change", and a sheet that would change nothing has to look
                # different from one that would.
                updated.append(contract.id)
                before = consent_state(contract)
                if dry_run:
                    continue

                apply_consent(
                    contract,
                    consent_email=row.consent_email,
                    consent_sms=row.consent_sms,
                    consent_voice=row.consent_voice,
                    do_not_contact=row.do_not_contact,
                )
                # Flush per row so a concurrent writer's committed change surfaces
                # here as a version conflict with the row that hit it, rather than
                # as an opaque failure at the end of the run.
                session.flush()
                AuditLogRepository(session, contract.tenant_id).record(
                    correlation_id=correlation_id,
                    actor_type=ACTOR_SYSTEM,
                    actor_id=None,
                    action=ACTION_CONSENT_UPDATED,
                    entity=AUDIT_ENTITY_CONTRACT,
                    entity_id=str(contract.id),
                    before=before,
                    after=consent_state(contract),
                    # The sheet's *name*, not its full path: enough to identify the
                    # import, without recording an operator's directory layout.
                    reason=f"consent imported from {sheet.name} line {row.line}",
                )
        except StaleDataError:
            # Another writer committed between the read and the flush. Undo the
            # whole run rather than reporting a partly-applied sheet, and leave the
            # retry to the operator, who can see what changed.
            session.rollback()
            log.error(
                "consent_import_conflict",
                path=str(sheet),
                hint="consent changed while the import ran; nothing was written, run it again",
            )
            return EXIT_FAILED

    log.info(
        "consent_import",
        path=str(sheet),
        dry_run=dry_run,
        rows=len(rows),
        updated=len(updated),
        unchanged=len(unchanged),
        missing=missing,
        contract_ids=updated,
    )

    if missing:
        log.error(
            "consent_import_contracts_not_found",
            contract_ids=missing,
            hint=(
                "no row was written for these ids; check the sheet came from this "
                "database and not from another environment's export"
            ),
        )
        return EXIT_NO_MATCH

    return EXIT_OK


def _password_from_stdin() -> tuple[str, str | None]:
    """Read the password from the first line of standard input.

    ``rstrip("\\r\\n")`` rather than ``strip()``: the newline a shell adds is not
    part of the password, but a trailing space the operator typed is. Trimming that
    would store a hash for a password the operator cannot reproduce at the login
    form, and the failure would look like a wrong password rather than a mangled one.
    """
    line = sys.stdin.readline()
    if not line:
        return "", "no password arrived on standard input"
    return line.rstrip("\r\n"), None


def _password_from_prompt() -> tuple[str, str | None]:
    """Ask for the password twice, so a typo cannot become the stored credential.

    There is no way to read back an Argon2id hash, so a mistyped bootstrap password
    is only discoverable by failing to log in afterwards — with a fresh database and
    one account, that reads as "authentication is broken".
    """
    try:
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Repeat password: ")
    except (EOFError, KeyboardInterrupt):
        return "", "no password was entered"
    if first != second:
        return "", "the two passwords did not match"
    return first, None


def _create_admin(
    args: argparse.Namespace,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    """``create-admin``: bootstrap the first account of a tenant.

    Gap 10. Nothing else in the system can create a user without already having one:
    ``POST /auth/users`` is admin-only, and login needs a user that does not exist
    yet. The documented workaround was a hand-written snippet in STATUS.md that
    built a ``Tenant`` and a ``User`` in a REPL — which means the first account of
    every deployment was made by someone pasting Python, and the snippet could drift
    from the models without anything noticing.

    Four rules:

    - **The password is never an argument.** It arrives on stdin or from a prompt,
      so it stays out of shell history, out of the process list (``ps`` shows argv
      to every user on the box) and out of CI logs, which is where a recycled
      bootstrap password would end up otherwise. The same bounds as
      ``POST /auth/users`` are imported rather than retyped, so the CLI cannot drift
      from the API's policy.
    - **The slug is stored the way login will look it up.** ``POST /auth/login``
      resolves the tenant with ``strip().lower()``, so ``--tenant-slug Acme`` stores
      ``acme``. A tenant stored with a casing the login form cannot reproduce is a
      tenant nobody can log into, and that is not a state this command will create.
    - **An existing account is a refusal, not a no-op.** Re-running with the same
      address exits :data:`EXIT_EXISTS` and changes nothing. Reporting success would
      leave the operator believing the password had just been set, and resetting it
      would make this command a way to take over an admin account without knowing
      its password. Creating a *different* address in an existing tenant still works,
      because "we lost the only admin" is a real and recoverable state.
    - **The creation is audited as ``system``.** This is the only account in the
      system created with no principal behind it, so the audit row is the only
      evidence of who did it. It carries the role and nothing about the person: no
      address, no hash.

    Every refusal happens before the first write. ``session_scope`` commits whatever
    it finds on exit, so a ``return`` after a flush would persist a half-built
    bootstrap — a tenant with no admin in it, which is exactly the state this command
    exists to get out of.
    """
    slug = str(args.tenant_slug).strip().lower()
    email = normalize_email(str(args.email))
    tenant_name = None if args.tenant_name is None else str(args.tenant_name).strip()

    problems: list[str] = []
    if not slug:
        problems.append("--tenant-slug cannot be blank")
    elif len(slug) > _MAX_SLUG_LENGTH:
        problems.append(f"--tenant-slug is longer than {_MAX_SLUG_LENGTH} characters")
    elif not _SLUG_RE.match(slug):
        problems.append(
            "--tenant-slug must be letters, digits and hyphens, and start with a letter or digit"
        )
    if not is_plausible_email(email):
        problems.append("--email is not a usable address")
    if tenant_name is not None:
        if not tenant_name:
            problems.append("--tenant-name cannot be blank")
        elif len(tenant_name) > _MAX_TENANT_NAME_LENGTH:
            problems.append(f"--tenant-name is longer than {_MAX_TENANT_NAME_LENGTH} characters")
    if problems:
        log.error("create_admin_invalid_arguments", problems=problems)
        return EXIT_FAILED

    password, problem = (
        _password_from_stdin() if bool(args.password_stdin) else _password_from_prompt()
    )
    if problem is not None:
        log.error("create_admin_password_rejected", tenant_slug=slug, reason=problem)
        return EXIT_FAILED
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        log.error(
            "create_admin_password_rejected",
            tenant_slug=slug,
            reason="length",
            min_length=MIN_PASSWORD_LENGTH,
            max_length=MAX_PASSWORD_LENGTH,
        )
        return EXIT_FAILED

    with session_scope(session_factory) as session:
        existing = IdentityDirectory(session).tenant_by_slug(slug)

        if existing is not None:
            # Refusals first: still nothing written, so any return here leaves the
            # database untouched.
            if tenant_name is not None and tenant_name != existing.name:
                log.error(
                    "create_admin_tenant_exists",
                    tenant_slug=slug,
                    hint=(
                        "--tenant-name is only used when the tenant is being created; "
                        "drop it to add an admin to the existing tenant"
                    ),
                )
                return EXIT_FAILED
            if not existing.is_active:
                log.error(
                    "create_admin_tenant_inactive",
                    tenant_slug=slug,
                    hint="a user in a deactivated tenant cannot log in; reactivate it first",
                )
                return EXIT_FAILED
            repository = UserRepository(session, existing.id)
            if repository.by_email(email) is not None:
                log.error(
                    "create_admin_account_exists",
                    tenant_slug=slug,
                    hint=(
                        "that address already has an account here and the run changed "
                        "nothing; passwords are never reset by this command"
                    ),
                )
                return EXIT_EXISTS
            tenant = existing
            tenant_created = False
        else:
            if tenant_name is None:
                log.error(
                    "create_admin_tenant_missing",
                    tenant_slug=slug,
                    hint="no tenant has that slug; pass --tenant-name to create it",
                )
                return EXIT_FAILED
            tenant = Tenant(name=tenant_name, slug=slug)
            session.add(tenant)
            session.flush()
            repository = UserRepository(session, tenant.id)
            tenant_created = True

        user = repository.create(
            email=email,
            password_hash=hash_password(password, settings),
            role=UserRole.ADMIN,
        )
        session.flush()
        AuditLogRepository(session, tenant.id).record(
            correlation_id=correlation_id_or_new(),
            actor_type=ACTOR_SYSTEM,
            actor_id=None,
            action=ACTION_USER_CREATED,
            entity=AUDIT_ENTITY_USER,
            entity_id=str(user.id),
            after={"role": user.role.value, "tenant_created": tenant_created},
            reason="bootstrap admin created by `python -m app.cli create-admin`",
        )
        user_id = user.id
        tenant_slug = tenant.slug

    log.info(
        "admin_created",
        user_id=str(user_id),
        tenant_slug=tenant_slug,
        role=UserRole.ADMIN.value,
        tenant_created=tenant_created,
    )
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
