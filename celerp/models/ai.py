# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
    )  # "pending" | "running" | "completed" | "failed"
    total_files: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credits_consumed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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
