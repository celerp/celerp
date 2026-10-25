# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""AI data models - conversations, messages, and batch jobs.

These are persisted via SQLAlchemy alongside the core ERP models.
All tables are company-scoped (company_id FK) and user-scoped where relevant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from celerp.models.base import Base


class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True,
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    messages: Mapped[list[AIMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AIMessage.created_at",
    )


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tools_called: Mapped[list | None] = mapped_column(sa.JSON, nullable=True)
    file_ids: Mapped[list | None] = mapped_column(sa.JSON, nullable=True)
    credits_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    conversation: Mapped[AIConversation] = relationship(back_populates="messages")


class AIBatchJob(Base):
    __tablename__ = "ai_batch_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False,
    )  # "pending" | "running" | "completed" | "failed" | "cancelled"
    kind: Mapped[str] = mapped_column(String(16), default="documents", nullable=False)  # "documents" | "question"
    mode: Mapped[str] = mapped_column(String(16), default="background", nullable=False)  # "interactive" | "background"
    question_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    file_ids: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    results: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class AIJobStep(Base):
    """One step of a job, and the identity the AI service answers it by: a file of a
    reading job, or a model turn or tool call of a question. The steps are the job's
    progress; a resumed job rebuilds from them and sends a step again only under the
    identity it was sent under."""

    __tablename__ = "ai_job_steps"
    __table_args__ = (sa.UniqueConstraint("job_id", "key", name="uq_ai_job_steps_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_batch_jobs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(String(160), nullable=False)  # file id, "model:{turn}" or "tool:{call_id}"
    operation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False,
    )  # "pending" | "running" | "done" | "failed" | "unknown" | "expired"
    result: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    charge_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class AIUsage(Base):
    """One question the AI service was asked, named by its ``operation_id`` (the user
    message, one file of a reading job, or the one-off request), with what the service
    charged for it. The row is written before the question leaves, and stays
    ``pending`` until the service has named the whole charge (in a reply, or in the
    operation's status read by celerp.ai.usage.reconcile_usage, once ``reconcile_at``
    has passed)."""

    __tablename__ = "ai_usage"
    __table_args__ = (
        sa.UniqueConstraint("company_id", "user_id", "operation_id", name="uq_ai_usage_operation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id"), nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False,
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Written before every send under the id: after it the service has ended and
    # settled that send (celerp.ai.deadlines.reconcile_delay_s).
    reconcile_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
