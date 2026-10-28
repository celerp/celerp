# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Durable migration runs and their source-to-Celerp entity maps.

A run imports one source into one staged company. It is committed as
``preparing`` before its source files leave the scan store, so a start that dies
part way is finished by a retry or by startup recovery. The row is the authoritative
state: status, current phase and per-phase progress survive restarts, and the
entity map makes every batch safe to re-run.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celerp.compat import StrEnum
from celerp.models.base import Base


class MigrationStatus(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    RECONCILING = "reconciling"
    READY_TO_FINALIZE = "ready_to_finalize"
    COMPLETED = "completed"


class MigrationPhase(StrEnum):
    """Import phases, in execution order."""
    COMPANY_SETTINGS = "company_settings"
    CURRENCIES_TAXES_ACCOUNTS = "currencies_taxes_accounts"
    CONTACTS_LOCATIONS = "contacts_locations"
    INVENTORY_MASTERS = "inventory_masters"
    OPERATIONAL_DOCUMENTS = "operational_documents"
    SETTLEMENTS = "settlements"
    INVENTORY_OPENING_ADJUSTMENTS = "inventory_opening_adjustments"
    RESIDUAL_JOURNALS_OR_CUTOVER_OPENING = "residual_journals_or_cutover_opening"
    ATTACHMENTS = "attachments"
    RECONCILIATION = "reconciliation"
    READY_TO_FINALIZE = "ready_to_finalize"


PHASE_ORDER: tuple[MigrationPhase, ...] = tuple(MigrationPhase)

_S = MigrationStatus
LEGAL_TRANSITIONS: dict[MigrationStatus, frozenset[MigrationStatus]] = {
    _S.PREPARING: frozenset({_S.READY, _S.FAILED}),
    _S.READY: frozenset({_S.RUNNING}),
    _S.RUNNING: frozenset({_S.CANCEL_REQUESTED, _S.INTERRUPTED, _S.FAILED, _S.RECONCILING}),
    _S.CANCEL_REQUESTED: frozenset({_S.CANCELLED, _S.INTERRUPTED, _S.FAILED}),
    _S.CANCELLED: frozenset({_S.RUNNING}),
    _S.INTERRUPTED: frozenset({_S.RUNNING}),
    _S.FAILED: frozenset({_S.RUNNING}),
    _S.RECONCILING: frozenset({_S.READY_TO_FINALIZE, _S.FAILED, _S.INTERRUPTED}),
    _S.READY_TO_FINALIZE: frozenset({_S.RUNNING, _S.COMPLETED}),
    _S.COMPLETED: frozenset(),
}


def can_transition(current: MigrationStatus, new: MigrationStatus) -> bool:
    return new in LEGAL_TRANSITIONS[current]


class MigrationRun(Base):
    __tablename__ = "migration_runs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("companies.id"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # SHA-256 of the scan token the run was started from: the durable, non-secret claim
    # that lets a repeated or recovered start find this run instead of creating another.
    scan_claim_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_artifact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The source's accounting lock date, read when the run is created. A resume must read the
    # same date from the source; finishing installs it as the company's period lock.
    source_lock_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    cif_version: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=MigrationStatus.READY.value)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase_state: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    coverage: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    mapping_decisions: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    source_summary: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    reconciliation: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    error_summary: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict, server_default="{}")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MigrationEntityMap(Base):
    __tablename__ = "migration_entity_maps"
    __table_args__ = (
        UniqueConstraint(
            "migration_run_id", "source_type", "source_external_id", name="uq_migration_entity_map_source"
        ),
        Index("idx_migration_entity_map_run_status", "migration_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    migration_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("migration_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # `metadata` is reserved on declarative classes; the column keeps the planned name.
    meta: Mapped[dict] = mapped_column("metadata", sa.JSON, nullable=False, default=dict, server_default="{}")


class MigrationCleanupTask(Base):
    """Files still to delete, retried at startup until they are gone.

    A discard task names a discarded staged company, and a company reset task names a
    reset company: its run sources and all its attachment files, written in the same
    transaction that deletes the company. An attachment task names one
    file a migration batch is about to store: committed before the file is written and
    deleted in the batch transaction that links it, so a task that outlives its batch
    marks a file no committed record links. No foreign keys: the company and runs a task
    names may no longer exist, and paths are derived from the ids, never stored."""
    __tablename__ = "migration_cleanup_tasks"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    run_ids: Mapped[list] = mapped_column(sa.JSON, nullable=False, default=list)
    # An attachment task's file: {"file_id", "mime", "idempotency_key"}; None on a discard task.
    attachment: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class MigrationAttachmentFetch(Base):
    """The files of a Xero migration's documents, fetched from Xero after the import.

    One per run: fetching again starts it over under a new operation, and the files already
    attached are recognised by their Xero id, never attached twice. ``progress`` holds
    the listing cursor (``kind`` and ``page``), the files found and attached so far, and
    the ``unmatched`` rows: documents not imported and files that could not be attached,
    each with its reason."""
    __tablename__ = "migration_attachment_fetches"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    migration_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("migration_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    started_by_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    cloud_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # signing_in, fetching, waiting (a Xero limit), done, stopped or failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    organisation: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # The Xero organisation (its stable tenant id) the current sign-in chose, and the one the
    # owner confirmed and files were fetched from; the binding outlives each fetch.
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bound_tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bound_organisation: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    progress: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class MigrationTemplate(Base):
    """The posting accounts a person chose finishing their last migration from one
    source, offered on their next client from that source. Belongs to the person, not
    a company: ``answers`` maps each role to the account chosen, ``{code, name,
    account_type}``, so a later client's chart is matched by account, never by code alone."""
    __tablename__ = "migration_templates"
    __table_args__ = (UniqueConstraint("user_id", "source_system", name="uq_migration_templates_user_source"),)

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    answers: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class MigrationBatchFile(Base):
    """One file of a person's Move several companies upload. A readable file holds its
    own scan, known here only by its claim, as a run is; the run started from it is
    found by that claim. A file that could not be read keeps the reason instead."""
    __tablename__ = "migration_batch_files"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prepared_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    source_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_claim_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
