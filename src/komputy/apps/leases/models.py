"""IFRS 16 lease accounting tables.

Accounting facts are append-only: approved versions, posted journal lines and locked periods are
never updated by the service layer (enforced there and covered by tests). Every row is tenant-scoped.
Lease terms/assessment/rate are stored in the same camelCase JSON shape the web app and the engine use.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from komputy.db import Base

MONEY = Numeric(20, 2)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LeaseEntity(Base):
    """One reporting entity + IFRS 16 book per workspace (multi-entity comes later)."""

    __tablename__ = "lease_entities"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    fy_start_month: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    book_name: Mapped[str] = mapped_column(Text, nullable=False)
    framework: Mapped[str] = mapped_column(String(16), nullable=False, server_default="IFRS16")
    standard_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="IFRS 16 (2016, as amended)")
    # per-tenant sequences (locked with SELECT … FOR UPDATE when allocating)
    lease_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    batch_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    import_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    import_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LeaseSetupVersion(Base):
    """Versioned policy / IBR curve / GL mapping. kind ∈ policies | curves | gl; data holds the kind's fields."""

    __tablename__ = "lease_setup_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "version"),
        Index(
            "lease_setup_one_pending",
            "tenant_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'in_review'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # in_review | approved
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = _ts()
    approved_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeasePeriod(Base):
    __tablename__ = "lease_periods"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")  # open | closing | locked
    locked_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopen_log: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # append-only [{by, at, reason}]
    created_at: Mapped[datetime] = _ts()


class LeaseCounterparty(Base):
    __tablename__ = "lease_counterparties"
    __table_args__ = (Index("lease_cp_tenant_name_uq", "tenant_id", func.lower(text("name")), unique=True),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code"),
        Index("leases_tenant_updated_idx", "tenant_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String(26), nullable=False, server_default="")
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(26), nullable=False)
    cost_centre: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    contract_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    signature_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    wizard_step: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rev: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")  # optimistic lock
    import_batch_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    verification_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LeaseVersion(Base):
    __tablename__ = "lease_versions"
    __table_args__ = (
        UniqueConstraint("lease_id", "n"),
        # at most one pending (draft / in review / returned) version per lease
        Index(
            "lease_versions_one_pending",
            "lease_id",
            unique=True,
            postgresql_where=text("status IN ('draft','in_review','returned')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    lease_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    terms: Mapped[dict] = mapped_column(JSON, nullable=False)
    assessment: Mapped[dict] = mapped_column(JSON, nullable=False)
    rate: Mapped[dict] = mapped_column(JSON, nullable=False)
    scope_decrease: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    termination_penalty: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    comments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # append-only
    created_by: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = _ts()
    submitted_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approver_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    calc_liability: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    calc_rou: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    calc_checks_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    calc_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class LeaseBooks(Base):
    """Computed books for a lease's approved chain (schedule rows, journals, segment summary).

    Rewritten on every approval; reports, batches and balances read from here instead of recomputing.
    """

    __tablename__ = "lease_books"
    lease_id: Mapped[str] = mapped_column(String(26), ForeignKey("leases.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    chain_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    exempt: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rows: Mapped[list] = mapped_column(JSON, nullable=False)
    journals: Mapped[list] = mapped_column(JSON, nullable=False)
    segments: Mapped[list] = mapped_column(JSON, nullable=False)
    computed_at: Mapped[datetime] = _ts()


class LeaseDocument(Base):
    __tablename__ = "lease_documents"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    lease_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_n: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(26), nullable=False)
    uploaded_at: Mapped[datetime] = _ts()


class LeaseCloseRun(Base):
    __tablename__ = "lease_close_runs"
    __table_args__ = (UniqueConstraint("tenant_id", "period"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    steps: Mapped[list] = mapped_column(JSON, nullable=False)  # [{id, status, doneBy, doneAt, note}]
    batch_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    started_by: Mapped[str] = mapped_column(String(26), nullable=False)
    started_at: Mapped[datetime] = _ts()


class LeaseActualPayment(Base):
    __tablename__ = "lease_actual_payments"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    close_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("lease_close_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    lease_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    expected: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    match: Mapped[str] = mapped_column(String(16), nullable=False)  # matched | late | different | unmatched
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(26), nullable=True)


class LeaseJournalBatch(Base):
    __tablename__ = "lease_journal_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code"),
        UniqueConstraint("idempotency_key"),
        Index(
            "lease_batches_erp_ref_uq",
            "tenant_id",
            "erp_ref",
            unique=True,
            postgresql_where=text("erp_ref IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # period | reversal
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # draft | approved | exported | posted | reversed
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = _ts()
    approved_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erp_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversal_of: Mapped[str | None] = mapped_column(String(26), nullable=True)
    reversed_by: Mapped[str | None] = mapped_column(String(26), nullable=True)


class LeaseJournalLine(Base):
    __tablename__ = "lease_journal_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False)
    batch_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("lease_journal_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lease_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    account: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    debit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    credit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    catch_up: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class LeasePostedBalance(Base):
    """Signed (Dr − Cr) cumulative per lease/account through the last locked period."""

    __tablename__ = "lease_posted_balances"
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    lease_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    account: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class LeaseImportBatch(Base):
    __tablename__ = "lease_import_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code"),
        Index(
            "lease_imports_committed_sha_uq",
            "tenant_id",
            "sha256",
            unique=True,
            postgresql_where=text("status = 'committed'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retry_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    headers: Mapped[list] = mapped_column(JSON, nullable=False)
    mapping: Mapped[dict] = mapped_column(JSON, nullable=False)
    warnings_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    warnings_accepted_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    template_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(26), nullable=False)
    created_at: Mapped[datetime] = _ts()
    committed_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeaseImportRow(Base):
    __tablename__ = "lease_import_rows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("lease_import_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    values: Mapped[dict] = mapped_column(JSON, nullable=False)
    issues: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    action: Mapped[str] = mapped_column(String(16), nullable=False, server_default="create")
    lease_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class LeaseSavedView(Base):
    __tablename__ = "lease_saved_views"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()


class LeaseAlertSent(Base):
    """Dedupe for daily alerts: one notification per (lease, alert key)."""

    __tablename__ = "lease_alerts_sent"
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False)
    lease_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    sent_at: Mapped[datetime] = _ts()
