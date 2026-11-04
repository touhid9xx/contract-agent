"""Contract aggregate: the contract row itself and its extracted fields."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base, TimestampMixin
from app.db.enums import (
    Channel,
    ContractSource,
    ContractStatus,
    FieldName,
    FieldStatus,
    RiskLevel,
    enum_column,
)
from app.db.types import GUID

#: Decimal places of ``confidence``, matching ``NUMERIC(4, 3)``. Named so the
#: model can enforce the scale SQLite does not (see
#: :meth:`ExtractedField._quantise_confidence`).
CONFIDENCE_SCALE = 3


class Contract(Base, TimestampMixin):
    """A customer contract whose end date drives every reminder.

    ``version`` is the optimistic-lock column: two operators editing the same
    contract must not silently overwrite each other, so a stale write raises
    ``StaleDataError`` which the API turns into a 409 with the current state.
    """

    __tablename__ = "contracts"
    __table_args__ = (
        # A PDF is de-duplicated per tenant, not globally: the same file uploaded
        # by two tenants is two independent contracts.
        UniqueConstraint("tenant_id", "sha256", name="uq_contracts_tenant_sha256"),
        # Drives the daily scan. MySQL has no partial indexes, so ``status``
        # leads and ``end_date`` follows — still sargable for
        # ``status = 'active' AND end_date BETWEEN ? AND ?``.
        Index("ix_contracts_scan", "status", "end_date"),
        Index("ix_contracts_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Pseudonymous label shown in lists ("A. Rahman"). Full identity lives in
    #: the encrypted fields, not here.
    customer_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ContractStatus] = mapped_column(
        enum_column(ContractStatus, length=20), nullable=False, default=ContractStatus.ACTIVE
    )
    source: Mapped[ContractSource] = mapped_column(
        enum_column(ContractSource, length=10), nullable=False, default=ContractSource.PDF
    )
    risk: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel, length=10), nullable=False, default=RiskLevel.LOW
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- ingestion provenance -------------------------------------------
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- contact policy inputs ------------------------------------------
    #: Resolved per contract from the tenant/customer jurisdiction. Drives the
    #: quiet-hours window checked deterministically before any dispatch.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    #: Standing instruction to make no contact at all, whatever consent exists.
    #: Separate from the per-channel flags below because it is a veto, not a
    #: choice of route: an opt-out must survive someone re-granting consent by
    #: mistake, so the decision policy checks it first and refuses outright.
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Per-channel consent, one column each rather than a JSON blob: the column
    #: set is closed (``Channel`` has three members) and a boolean is queryable,
    #: index-friendly and impossible to typo into a channel that does not exist.
    #: All three default to ``False`` — consent is opt-in, so a contract ingested
    #: but never consented to cannot be contacted by any route (fail closed).
    consent_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: When consent was last recorded or revoked, so "was this in force at the
    #: time?" is answerable without replaying the audit log.
    consent_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    fields: Mapped[list[ExtractedField]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )

    #: Declarative directive, not shared state: SQLAlchemy reads this dict off the
    #: class body and copies it, so it is a per-class constant, not a mutable default.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    @property
    def consent_channels(self) -> set[Channel]:
        """The channels this customer has granted consent for, ignoring opt-out.

        Spelled out one flag at a time rather than looked up in a mapping: the
        column set is closed, and an explicit list is what a mypy run can check
        against the columns that actually exist.
        """
        granted: set[Channel] = set()
        if self.consent_email:
            granted.add(Channel.EMAIL)
        if self.consent_sms:
            granted.add(Channel.SMS)
        if self.consent_voice:
            granted.add(Channel.VOICE)
        return granted

    @property
    def may_contact(self) -> bool:
        """Whether any channel at all is permitted.

        Opt-out wins over consent: a ``do_not_contact`` contract is not
        contactable even if every channel flag is set, because the flag is a
        standing instruction rather than an expired permission. The decision
        policy refuses outright instead of selecting a channel and relying on the
        sender to suppress it later.
        """
        return not self.do_not_contact and bool(self.consent_channels)


class ExtractedField(Base, TimestampMixin):
    """One field extracted from one contract, with its provenance and review state.

    ``raw_value`` holds the value exactly as the extractor produced it and is
    the column that F7 encrypts. ``normalized_value`` holds the canonical form
    used for comparison (a normalized date, a lowercased email) and is what
    pattern-matching equality is done against.
    """

    __tablename__ = "extracted_fields"
    __table_args__ = (
        # One row per field per contract: re-extraction updates in place instead
        # of accumulating duplicates that a reader could pick the wrong one from.
        UniqueConstraint("contract_id", "field_name", name="uq_extracted_fields_contract_field"),
        Index("ix_extracted_fields_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    field_name: Mapped[FieldName] = mapped_column(enum_column(FieldName, length=20), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``NUMERIC(4,3)`` = 0.000-1.000. Calibrated per field, never one global score.
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0.0)
    status: Mapped[FieldStatus] = mapped_column(
        enum_column(FieldStatus, length=20), nullable=False, default=FieldStatus.PENDING_REVIEW
    )

    # --- provenance ------------------------------------------------------
    source_document: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_line: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- review ----------------------------------------------------------
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    contract: Mapped[Contract] = relationship(back_populates="fields")

    #: See :class:`Contract` — a declarative directive, not shared mutable state.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    @property
    def blocks_pipeline(self) -> bool:
        """Whether this field is still holding back dispatch.

        The notification worker must call this, not re-implement the rule: a
        pending or conflicting field is a hard stop (spec §CUSTOMER CONTACT
        POLICY), and having one definition prevents the two from drifting.
        """
        return self.status in {FieldStatus.PENDING_REVIEW, FieldStatus.REJECTED}

    @validates("confidence")
    def _quantise_confidence(self, _key: str, value: float) -> float:
        """Keep ``confidence`` at the precision its column declares.

        ``NUMERIC(4, 3)`` is enforced by MySQL, which stores exactly three
        decimals, but **not** by SQLite, which stores the float it is handed. Any
        value with more precision than that — ``0.1 * 3`` is
        ``0.30000000000000004`` — is therefore stored in full and read back
        rounded, so a page boundary in the review queue compares a rounded key
        against an unrounded row and serves that row twice. Quantising on write
        makes the declared scale true on both dialects, for the same reason
        :func:`~app.db.base.truncate_to_second` floors timestamps (D2-06).
        """
        return round(float(value), CONFIDENCE_SCALE)
