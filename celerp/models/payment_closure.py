# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from celerp.models.base import Base


class PaymentClosure(Base):
    """A request to Celerp Cloud to close one company's online payments that has not
    been settled yet.

    Saved in its own transaction before Cloud is asked to prepare the closing, so it
    survives a crash at any later point. Once the reset that asked for it has
    committed or rolled back, the company is looked up: gone means Cloud is told to
    close the payments for good, still there means Cloud is told to reopen them. The
    row is removed once Cloud answers either for good. Until then Cloud refuses new
    payment pages for the company. Not company data: it must outlive the company it
    names, so the company is a plain value, not a reference."""

    __tablename__ = "payment_closures"

    operation_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True)
    target_company: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    # The installation's payment generation when the request was made (PaymentRecovery).
    generation: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


class PaymentRecovery(Base):
    """A System Recovery restore of this installation, as Celerp Cloud must learn of it.

    Written in the restore's own transaction, naming every company the restore brought
    back. Celerp Cloud answers with the installation's new payment generation: closing
    requests from before the restore can no longer close payments for good, and the
    restored companies take payments again. Until Cloud has answered (``generation``
    is None) no company's payments can be closed. Rows restored from an older backup
    keep their generations; the latest generation is the current one. Cloud
    delivers again every payment the installation ever recorded, so none is lost
    with the restore, and confirms the restore once they are recorded again."""

    __tablename__ = "payment_recoveries"

    recovery_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), primary_key=True)
    company_ids: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    generation: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


class UnmatchedPayment(Base):
    """An online payment Celerp Cloud delivered that could not be recorded on its
    invoice: the company or the invoice no longer exists, or the invoice refused it.

    Kept so the money received is never lost from view; recorded before Cloud is told
    the payment arrived. Not company data: it must outlive the company it names, so
    the company and invoice are plain values, not references."""

    __tablename__ = "unmatched_payments"

    reference: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    former_company: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    document: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    # When the customer paid, as the payment provider reported it; None when not reported.
    paid_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # When the payment stopped being linked to Stripe (Stripe was disconnected); applied
    # with the payment once it is recorded on its invoice. None while it is linked.
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # The company and invoice a person recorded it on from the unmatched payments
    # (``payments.record_unmatched_payment``); every later delivery of it, its refunds
    # and its release goes there. None while it is unmatched.
    recorded_company: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    recorded_document: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class UnmatchedRefund(Base):
    """A change to a Stripe refund of an online payment, delivered by Celerp Cloud, that
    could not be applied to the payment yet: the payment is among the unmatched
    payments or has not arrived, its company or invoice no longer exists, or the
    payment cannot take it. Kept with the unmatched payments under the payment's
    reference, with the books the payment was recorded on, and applied in the order
    Stripe reported, as soon as its payment is on its invoice.

    ``transition`` is "applied" (the refund succeeded) or "reversed" (an applied refund
    later failed or was canceled). ``cycle`` counts the times Stripe put the refund
    through: a refund undone and put through again is applied again in its next cycle,
    and a reversal undoes the refund applied in its own cycle."""

    __tablename__ = "unmatched_refunds"

    refund_id: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    cycle: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    transition: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    reference: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    former_company: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    document: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # When Stripe reported the change; None when not reported.
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    context: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
