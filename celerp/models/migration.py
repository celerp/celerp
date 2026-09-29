# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Durable migration runs and their source-to-Celerp entity maps.

A run imports one source into one staged company. The row is the authoritative
state: status, current phase and per-phase progress survive restarts, and the
entity map makes every batch safe to re-run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from celerp.compat import StrEnum
from celerp.models.base import Base


class MigrationStatus(StrEnum):
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
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_artifact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    cif_version: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=MigrationStatus.READY.value)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase_state: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    coverage: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    mapping_decisions: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    source_summary: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    reconciliation: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    error_summary: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
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
    meta: Mapped[dict] = mapped_column("metadata", sa.JSON, nullable=False, default=dict)
