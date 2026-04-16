# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Auto journal entry creation for document lifecycle events.

Uses doc-scoped idempotency keys so the same doc can never produce
duplicate JEs regardless of trigger source (API, import, doctor repair).
"""

from __future__ import annotations

import uuid

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.je_keys import je_idempotency_key
from sqlalchemy import select as _select


async def _emit_auto_posted_je(
    session,
    *,
    company_id,
    user_id,
    je_id: str,
    idem_create: str,
    idem_posted: str,
    memo: str,
    entries: list[dict],
    metadata_: dict,
    ts: str | None = None,
) -> None:
    payload = {"memo": memo, "entries": entries}
    if ts:
        payload["ts"] = ts

    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data=payload,
        actor_id=user_id,
        location_id=None,
        source="auto_je",
        idempotency_key=idem_create,
        metadata_=metadata_,
    )
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.posted",
        data={},
        actor_id=user_id,
        location_id=None,
        source="auto_je",
        idempotency_key=idem_posted,
        metadata_=metadata_,
    )


async def create_for_doc_finalized(session, *, company_id, user_id, doc_id: str, doc: dict) -> None:
    tax = float(doc.get("tax", 0) or 0)
    total = float(doc.get("total", 0) or 0)
    revenue = total - tax  # net revenue (after discount, before tax)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:fin",
        idem_create=je_idempotency_key(doc_id, "invoice.finalized", "c"),
        idem_posted=je_idempotency_key(doc_id, "invoice.finalized", "p"),
        memo=f"Auto JE for {doc_id} finalized",
        ts=doc.get("finalized_at") or doc.get("issue_date"),
        entries=[
            {"account": "1120", "debit": total, "credit": 0.0},
            {"account": "4100", "debit": 0.0, "credit": revenue},
            {"account": "2120", "debit": 0.0, "credit": tax},
        ],
        metadata_={"trigger": "doc.finalized", "doc_id": doc_id},
    )


async def create_for_doc_payment(session, *, company_id, user_id, doc_id: str, amount: float, cumulative_paid: float | None = None, bank_account_code: str = "1110", doc_type: str = "invoice") -> None:
    """Create JE for a payment.

    bank_account_code: chart account to debit (defaults to "1110" generic cash).
    Pass the specific bank sub-account (e.g. "1111") when the user selects a bank.
    doc_type: 'invoice' debits bank/credits AR; 'bill' debits AP/credits bank.
    """
    paid_key = str(int(round((cumulative_paid or amount) * 100)))  # cents, avoids float key issues
    if doc_type in ("bill", "purchase_order"):
        entries = [
            {"account": "2110", "debit": float(amount), "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": float(amount)},
        ]
    else:
        entries = [
            {"account": bank_account_code, "debit": float(amount), "credit": 0.0},
            {"account": "1120", "debit": 0.0, "credit": float(amount)},
        ]
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:pay:{paid_key}",
        idem_create=je_idempotency_key(doc_id, f"invoice.paid:{paid_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"invoice.paid:{paid_key}", "p"),
        memo=f"Auto JE for {doc_id} payment",
        entries=entries,
        metadata_={"trigger": "doc.payment.received", "doc_id": doc_id, "cumulative_paid": cumulative_paid},
    )


async def void_for_doc_payment(session, *, company_id, user_id, doc_id: str, payment_index: int, amount: float, bank_account_code: str = "1110", doc_type: str = "invoice") -> None:
    """Reverse a payment JE by creating a counter-entry."""
    void_key = f"void_{payment_index}"
    if doc_type in ("bill", "purchase_order"):
        entries = [
            {"account": bank_account_code, "debit": float(amount), "credit": 0.0},
            {"account": "2110", "debit": 0.0, "credit": float(amount)},
        ]
    else:
        entries = [
            {"account": "1120", "debit": float(amount), "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": float(amount)},
        ]
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:payvoid:{void_key}",
        idem_create=je_idempotency_key(doc_id, f"payment.voided:{void_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"payment.voided:{void_key}", "p"),
        memo=f"Auto JE for {doc_id} payment void (index {payment_index})",
        entries=entries,
        metadata_={"trigger": "doc.payment.voided", "doc_id": doc_id, "payment_index": payment_index},
    )


async def create_for_cn_application(session, *, company_id, user_id, doc_id: str, cn_id: str, amount: float) -> None:
    """Create JE for credit note application: AR-to-AR transfer."""
    app_key = f"cn_apply_{cn_id}"
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:cnapply:{cn_id}",
        idem_create=je_idempotency_key(doc_id, f"cn.applied:{app_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"cn.applied:{app_key}", "p"),
        memo=f"Auto JE for credit note {cn_id} applied to {doc_id}",
        entries=[
            {"account": "1120", "debit": 0.0, "credit": float(amount)},
            {"account": "1120", "debit": float(amount), "credit": 0.0},
        ],
        metadata_={"trigger": "cn.applied", "doc_id": doc_id, "cn_id": cn_id},
    )


async def create_for_po_received(
    session,
    *,
    company_id,
    user_id,
    po_id: str,
    total: float,
    doc: dict | None = None,
) -> None:
    purchase_kind = str((doc or {}).get("purchase_kind") or "inventory").strip().lower()
    debit_account = {
        "inventory": "1130",
        "expense": "6950",
        "asset": "1210",
    }.get(purchase_kind, "1130")

    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{po_id}:rcv",
        idem_create=je_idempotency_key(po_id, "po.received", "c"),
        idem_posted=je_idempotency_key(po_id, "po.received", "p"),
        memo=f"Auto JE for {po_id} received",
        entries=[
            {"account": debit_account, "debit": float(total), "credit": 0.0},
            {"account": "2110", "debit": 0.0, "credit": float(total)},
        ],
        metadata_={"trigger": "doc.received", "doc_id": po_id, "purchase_kind": purchase_kind},
    )


async def create_for_bill_conversion(
    session,
    *,
    company_id,
    user_id,
    doc_id: str,
    doc: dict,
) -> None:
    """Create JE when a PO is converted to a bill.

    Debit per-line expense/inventory accounts, credit AP (2110).
    Line-level account_code takes priority; otherwise defaults to 1130 (inventory)
    for lines with SKU, 6950 (misc expense) for lines without.
    """
    total = float(doc.get("total", 0) or 0)
    line_items = doc.get("line_items", [])
    entries: list[dict] = []

    if line_items:
        for li in line_items:
            line_total = float(li.get("line_total", 0) or 0) or (
                float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)
            )
            if line_total <= 0:
                continue
            account = li.get("account_code") or ("1130" if li.get("sku") else "6950")
            entries.append({"account": account, "debit": line_total, "credit": 0.0})
    else:
        # No line items - single debit to misc expense
        entries.append({"account": "6950", "debit": total, "credit": 0.0})

    if total > 0:
        entries.append({"account": "2110", "debit": 0.0, "credit": total})

    if not entries:
        return

    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:bill",
        idem_create=je_idempotency_key(doc_id, "po.converted_to_bill", "c"),
        idem_posted=je_idempotency_key(doc_id, "po.converted_to_bill", "p"),
        memo=f"Auto JE for {doc_id} converted to bill",
        entries=entries,
        metadata_={"trigger": "doc.converted_to_bill", "doc_id": doc_id},
    )


async def void_for_doc_finalized(session, *, company_id, user_id, doc_id: str) -> None:
    """Void the auto-JE that was created when a doc was finalized (invoice or bill)."""
    for je_suffix in ("fin", "bill"):
        je_id = f"je:auto:{doc_id}:{je_suffix}"
        row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
        if row is not None and row.state.get("status") == "posted":
            await emit_event(
                session,
                company_id=company_id,
                entity_id=je_id,
                entity_type="journal_entry",
                event_type="acc.journal_entry.voided",
                data={"reason": f"Reversed: {doc_id} reverted to draft"},
                actor_id=user_id,
                location_id=None,
                source="auto_je",
                idempotency_key=je_idempotency_key(doc_id, "revert_to_draft", "void"),
                metadata_={"trigger": "doc.reverted_to_draft", "doc_id": doc_id},
            )
            return  # void the first found; at most one exists per doc


async def create_for_doc_unvoided(session, *, company_id, user_id, doc_id: str, doc: dict) -> None:
    """Re-apply the finalize JE when a doc is unvoided.

    Uses 'unvoid' suffix in idempotency keys to avoid colliding with original JEs
    (which may still exist if voiding didn't delete them).
    """
    doc_type = doc.get("doc_type", "")
    total = float(doc.get("total", 0) or 0)
    if total <= 0:
        return

    if doc_type == "invoice":
        tax = float(doc.get("tax", 0) or 0)
        revenue = total - tax
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:fin:unvoid",
            idem_create=je_idempotency_key(doc_id, "invoice.finalized.unvoid", "c"),
            idem_posted=je_idempotency_key(doc_id, "invoice.finalized.unvoid", "p"),
            memo=f"Auto JE for {doc_id} unvoided (restore finalize)",
            entries=[
                {"account": "1120", "debit": total, "credit": 0.0},
                {"account": "4100", "debit": 0.0, "credit": revenue},
                {"account": "2120", "debit": 0.0, "credit": tax},
            ],
            metadata_={"trigger": "doc.unvoided", "doc_id": doc_id},
        )
    elif doc_type in ("bill", "purchase_order"):
        line_items = doc.get("line_items", [])
        entries: list[dict] = []
        if line_items:
            for li in line_items:
                line_total = float(li.get("line_total", 0) or 0) or (
                    float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0)
                )
                if line_total <= 0:
                    continue
                account = li.get("account_code") or ("1130" if li.get("sku") else "6950")
                entries.append({"account": account, "debit": line_total, "credit": 0.0})
        else:
            entries.append({"account": "6950", "debit": total, "credit": 0.0})
        entries.append({"account": "2110", "debit": 0.0, "credit": total})
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:bill:unvoid",
            idem_create=je_idempotency_key(doc_id, "po.converted_to_bill.unvoid", "c"),
            idem_posted=je_idempotency_key(doc_id, "po.converted_to_bill.unvoid", "p"),
            memo=f"Auto JE for {doc_id} unvoided (restore bill conversion)",
            entries=entries,
            metadata_={"trigger": "doc.unvoided", "doc_id": doc_id},
        )


async def create_for_doc_fulfilled(session, *, company_id, user_id, doc_id: str, total_cogs: float) -> None:
    """Create COGS JE when a doc is fulfilled: Debit COGS (5100) / Credit Inventory (1300)."""
    if total_cogs <= 0:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:fulfill",
        idem_create=je_idempotency_key(doc_id, "fulfill", "c"),
        idem_posted=je_idempotency_key(doc_id, "fulfill", "p"),
        memo=f"Auto JE for {doc_id} fulfilled (COGS)",
        entries=[
            {"account": "5100", "debit": float(total_cogs), "credit": 0.0},
            {"account": "1300", "debit": 0.0, "credit": float(total_cogs)},
        ],
        metadata_={"trigger": "doc.fulfilled", "doc_id": doc_id},
    )


async def void_for_doc_fulfilled(session, *, company_id, user_id, doc_id: str) -> None:
    """Reverse the COGS JE created when a doc was fulfilled."""
    je_id = f"je:auto:{doc_id}:fulfill"
    row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    if row is not None and row.state.get("status") == "posted":
        await emit_event(
            session,
            company_id=company_id,
            entity_id=je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data={"reason": f"Reversed: {doc_id} fulfillment reversed"},
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=je_idempotency_key(doc_id, "fulfill-void", "void"),
            metadata_={"trigger": "doc.fulfillment_reversed", "doc_id": doc_id},
        )


async def create_for_mfg_completed(session, *, company_id, user_id, order_id: str, input_cost: float, waste_cost: float) -> None:
    output_cost = max(0.0, float(input_cost) - float(waste_cost))
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{order_id}:mfg",
        idem_create=je_idempotency_key(order_id, "mfg.completed", "c"),
        idem_posted=je_idempotency_key(order_id, "mfg.completed", "p"),
        memo=f"Auto JE for {order_id} completion",
        entries=[
            {"account": "1130", "debit": output_cost, "credit": 0.0},
            {"account": "5100", "debit": float(waste_cost), "credit": 0.0},
            {"account": "1130", "debit": 0.0, "credit": float(input_cost)},
        ],
        metadata_={"trigger": "mfg.order.completed", "order_id": order_id},
    )
