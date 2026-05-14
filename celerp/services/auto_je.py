# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Auto journal entry creation for document lifecycle events.

Uses doc-scoped idempotency keys so the same doc can never produce
duplicate JEs regardless of trigger source (API, import, doctor repair).
"""

from __future__ import annotations

import uuid
from decimal import Decimal as _Dec

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.je_keys import je_idempotency_key
from celerp.services.money import round_money, to_decimal, to_stored_float
from sqlalchemy import select as _select


def _to_base(amount: float, rate: _Dec, base_cur: str) -> float:
    """Convert a doc-currency amount to base currency for JE entry.

    When rate is 1 (base currency doc), result is identical to input.
    """
    return to_stored_float(round_money(to_decimal(amount) * rate, base_cur))


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


async def create_for_doc_finalized(session, *, company_id, user_id, doc_id: str, doc: dict, base_currency: str = "USD") -> None:
    currency = doc.get("currency", "USD")
    rate = _Dec(str(doc.get("conversion_rate") or 1))
    total_d = round_money(doc.get("total", 0), currency)
    tax_d = round_money(doc.get("tax", 0), currency)
    revenue_d = round_money(total_d - tax_d, currency)
    total = _to_base(to_stored_float(total_d), rate, base_currency)
    tax = _to_base(to_stored_float(tax_d), rate, base_currency)
    revenue = _to_base(to_stored_float(revenue_d), rate, base_currency)
    # Use a cycle-aware suffix so re-finalize after revert creates a fresh JE entity
    # rather than hitting the dedup guard on the voided JE from the previous cycle.
    cycle = int(doc.get("revert_count", 0))
    cycle_suffix = f"fin:{cycle}" if cycle else "fin"
    je_type_key = f"invoice.finalized:{cycle}" if cycle else "invoice.finalized"
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{cycle_suffix}",
        idem_create=je_idempotency_key(doc_id, je_type_key, "c"),
        idem_posted=je_idempotency_key(doc_id, je_type_key, "p"),
        memo=f"Auto JE for {doc_id} finalized",
        ts=doc.get("finalized_at") or doc.get("issue_date"),
        entries=[
            {"account": "1120", "debit": total, "credit": 0.0},
            {"account": "4100", "debit": 0.0, "credit": revenue},
            {"account": "2120", "debit": 0.0, "credit": tax},
        ],
        metadata_={"trigger": "doc.finalized", "doc_id": doc_id},
    )


async def create_for_doc_payment(session, *, company_id, user_id, doc_id: str, amount: float, payment_index: int, bank_account_code: str, doc_type: str = "invoice", payment_date: str, base_currency: str = "USD", conversion_rate: float = 1.0) -> None:
    """Create JE for a payment.

    bank_account_code: chart account to debit. Required - no default. Always pass the
        specific bank sub-account (e.g. "1111"). Omitting raises TypeError at call time.
    doc_type: 'invoice' debits bank/credits AR; 'bill' debits AP/credits bank.
    payment_date: ISO date string (YYYY-MM-DD). Always required.
    payment_index: position of this payment in the payments list (0-based). Used as the
        idempotency key suffix so voiding and re-paying at the same amount never collides.
    base_currency: company base currency for JE conversion.
    conversion_rate: doc-to-base-currency rate (1.0 for base currency docs).
    """
    rate = _Dec(str(conversion_rate or 1))
    base_amount = _to_base(float(amount), rate, base_currency)
    paid_key = str(payment_index)
    if doc_type in ("bill", "purchase_order"):
        entries = [
            {"account": "2110", "debit": base_amount, "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": base_amount},
        ]
    else:
        entries = [
            {"account": bank_account_code, "debit": base_amount, "credit": 0.0},
            {"account": "1120", "debit": 0.0, "credit": base_amount},
        ]
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:pay:{paid_key}",
        idem_create=je_idempotency_key(doc_id, f"invoice.paid:{paid_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"invoice.paid:{paid_key}", "p"),
        memo=f"Auto JE for {doc_id} payment",
        ts=payment_date,
        entries=entries,
        metadata_={"trigger": "doc.payment.received", "doc_id": doc_id, "payment_index": payment_index},
    )


async def void_for_doc_payment(session, *, company_id, user_id, doc_id: str, payment_index: int, amount: float, bank_account_code: str, doc_type: str = "invoice", refund_date: str | None = None, base_currency: str = "USD", conversion_rate: float = 1.0) -> None:
    """Reverse a payment JE by creating a counter-entry.

    refund_date: ISO date for the reversal JE (defaults to today if None). Used when
        void is actually a refund - the date affects bank ledger position.
    base_currency: company base currency for JE conversion.
    conversion_rate: doc-to-base-currency rate (1.0 for base currency docs).
    """
    rate = _Dec(str(conversion_rate or 1))
    base_amount = _to_base(float(amount), rate, base_currency)
    void_key = f"void_{payment_index}"
    if doc_type in ("bill", "purchase_order"):
        entries = [
            {"account": bank_account_code, "debit": base_amount, "credit": 0.0},
            {"account": "2110", "debit": 0.0, "credit": base_amount},
        ]
    else:
        entries = [
            {"account": "1120", "debit": base_amount, "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": base_amount},
        ]
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:payvoid:{void_key}",
        idem_create=je_idempotency_key(doc_id, f"payment.voided:{void_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"payment.voided:{void_key}", "p"),
        memo=f"Auto JE for {doc_id} payment void (index {payment_index})",
        ts=refund_date,
        entries=entries,
        metadata_={"trigger": "doc.payment.voided", "doc_id": doc_id, "payment_index": payment_index},
    )


async def create_for_cn_application(session, *, company_id, user_id, doc_id: str, cn_id: str, amount: float, payment_index: int = 0, base_currency: str = "USD", conversion_rate: float = 1.0) -> None:
    """Create JE for credit note application: AR-to-AR transfer.

    payment_index disambiguates repeated applications (void + re-apply) to the same CN-invoice pair.
    base_currency: company base currency for JE conversion.
    conversion_rate: doc-to-base-currency rate (1.0 for base currency docs).
    """
    rate = _Dec(str(conversion_rate or 1))
    base_amount = _to_base(float(amount), rate, base_currency)
    app_key = f"cn_apply_{cn_id}:{payment_index}"
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:cnapply:{cn_id}",
        idem_create=je_idempotency_key(doc_id, f"cn.applied:{app_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"cn.applied:{app_key}", "p"),
        memo=f"Auto JE for credit note {cn_id} applied to {doc_id}",
        entries=[
            {"account": "1120", "debit": 0.0, "credit": base_amount},
            {"account": "1120", "debit": base_amount, "credit": 0.0},
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
    unique_suffix: str | None = None,
    base_currency: str = "USD",
    receive_date: str | None = None,
) -> None:
    purchase_kind = str((doc or {}).get("purchase_kind") or "inventory").strip().lower()
    debit_account = {
        "inventory": "1130",
        "expense": "6950",
        "asset": "1210",
    }.get(purchase_kind, "1130")

    rate = _Dec(str((doc or {}).get("conversion_rate") or 1))
    base_total = _to_base(float(total), rate, base_currency)

    # suffix=None means "first receive" - use fixed key for backward compat with doctor/duplicate checks
    # suffix provided means "re-receive after revert" - use unique key to avoid idempotency collision
    suffix = unique_suffix if unique_suffix is not None else "0"
    idem_suffix = f":{suffix}" if unique_suffix is not None else ""
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{po_id}:rcv{idem_suffix}",
        idem_create=je_idempotency_key(po_id, f"po.received{idem_suffix}", "c"),
        idem_posted=je_idempotency_key(po_id, f"po.received{idem_suffix}", "p"),
        memo=f"Auto JE for {po_id} received",
        ts=receive_date,
        entries=[
            {"account": debit_account, "debit": base_total, "credit": 0.0},
            {"account": "2110", "debit": 0.0, "credit": base_total},
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
    base_currency: str = "USD",
) -> None:
    """Create JE when a bill is finalized (direct bill) or when a PO is converted to a bill.

    Debit per-line expense/inventory accounts, credit AP (2110).
    Line-level account_code takes priority; otherwise defaults to 1130 (inventory)
    for lines with SKU, 6950 (misc expense) for lines without.
    """
    total = float(doc.get("total", 0) or 0)
    currency = doc.get("currency", "USD")
    rate = _Dec(str(doc.get("conversion_rate") or 1))
    line_items = doc.get("line_items", [])
    debit_entries: list[dict] = []
    tax_total_d = _Dec(0)

    if line_items:
        for li in line_items:
            line_total = to_stored_float(round_money(
                to_decimal(li.get("line_total") or 0) or
                to_decimal(li.get("quantity", 0)) * to_decimal(li.get("unit_price", 0)),
                currency,
            ))
            if line_total <= 0:
                continue
            tax_rate = to_decimal(li.get("tax_rate", 0) or 0)
            line_tax_d = round_money(to_decimal(line_total) * tax_rate / 100, currency)
            tax_total_d += line_tax_d
            # receive_as overrides SKU-based account selection for bills.
            receive_as = (li.get("receive_as") or "").strip().lower()
            if li.get("account_code"):
                account = li["account_code"]
            elif receive_as == "expense":
                account = "6950"
            elif receive_as == "asset":
                account = "1210"
            else:
                account = "1130" if li.get("sku") else "6950"
            debit_entries.append({"account": account, "debit": _to_base(line_total, rate, base_currency), "credit": 0.0})
        if tax_total_d > 0:
            debit_entries.append({"account": "1150", "debit": _to_base(to_stored_float(tax_total_d), rate, base_currency), "credit": 0.0})

    base_total = _to_base(total, rate, base_currency)
    if not debit_entries:
        if total <= 0:
            return
        debit_entries.append({"account": "6950", "debit": base_total, "credit": 0.0})

    entries = debit_entries + [{"account": "2110", "debit": 0.0, "credit": base_total}]

    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:bill",
        idem_create=je_idempotency_key(doc_id, "po.converted_to_bill", "c"),
        idem_posted=je_idempotency_key(doc_id, "po.converted_to_bill", "p"),
        memo=f"Auto JE for {doc_id} converted to bill",
        ts=doc.get("issue_date") or doc.get("finalized_at"),
        entries=entries,
        metadata_={"trigger": "doc.converted_to_bill", "doc_id": doc_id},
    )


async def void_for_doc_finalized(session, *, company_id, user_id, doc_id: str, revert_count: int = 0) -> None:
    """Void the auto-JE that was created when a doc was finalized (invoice or bill).

    revert_count: the current revert_count from doc state (before this revert increments it).
    Used to derive the correct JE entity id for cycle-aware invoice JEs.
    """
    # Cycle-aware invoice JE id (matches create_for_doc_finalized logic).
    cycle = revert_count
    fin_suffix = f"fin:{cycle}" if cycle else "fin"
    for je_suffix in (fin_suffix, "bill"):
        je_id = f"je:auto:{doc_id}:{je_suffix}"
        row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
        if row is not None and row.state.get("status") == "posted":
            # Void idempotency key is cycle-aware to allow multiple revert cycles.
            void_cycle_key = f"revert_to_draft:{revert_count}"
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
                idempotency_key=je_idempotency_key(doc_id, void_cycle_key, "void"),
                metadata_={"trigger": "doc.reverted_to_draft", "doc_id": doc_id},
            )
            return  # void the first found; at most one exists per doc


async def create_for_doc_unvoided(session, *, company_id, user_id, doc_id: str, doc: dict, base_currency: str = "USD") -> None:
    """Re-apply the finalize JE when a doc is unvoided.

    Uses 'unvoid' suffix in idempotency keys to avoid colliding with original JEs
    (which may still exist if voiding didn't delete them).
    """
    doc_type = doc.get("doc_type", "")
    currency = doc.get("currency", "USD")
    rate = _Dec(str(doc.get("conversion_rate") or 1))
    total = float(doc.get("total", 0) or 0)
    if total <= 0:
        return

    if doc_type == "invoice":
        tax_d = round_money(doc.get("tax", 0), currency)
        total_d = round_money(total, currency)
        base_total = _to_base(to_stored_float(total_d), rate, base_currency)
        base_tax = _to_base(to_stored_float(tax_d), rate, base_currency)
        base_revenue = _to_base(to_stored_float(round_money(total_d - tax_d, currency)), rate, base_currency)
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:fin:unvoid",
            idem_create=je_idempotency_key(doc_id, "invoice.finalized.unvoid", "c"),
            idem_posted=je_idempotency_key(doc_id, "invoice.finalized.unvoid", "p"),
            memo=f"Auto JE for {doc_id} unvoided (restore finalize)",
            entries=[
                {"account": "1120", "debit": base_total, "credit": 0.0},
                {"account": "4100", "debit": 0.0, "credit": base_revenue},
                {"account": "2120", "debit": 0.0, "credit": base_tax},
            ],
            metadata_={"trigger": "doc.unvoided", "doc_id": doc_id},
        )
    elif doc_type in ("bill", "purchase_order"):
        line_items = doc.get("line_items", [])
        debit_entries: list[dict] = []
        tax_total_d = _Dec(0)
        if line_items:
            for li in line_items:
                line_total = to_stored_float(round_money(
                    to_decimal(li.get("line_total") or 0) or
                    to_decimal(li.get("quantity", 0)) * to_decimal(li.get("unit_price", 0)),
                    currency,
                ))
                if line_total <= 0:
                    continue
                tax_rate = to_decimal(li.get("tax_rate", 0) or 0)
                line_tax_d = round_money(to_decimal(line_total) * tax_rate / 100, currency)
                tax_total_d += line_tax_d
                receive_as = (li.get("receive_as") or "").strip().lower()
                if li.get("account_code"):
                    account = li["account_code"]
                elif receive_as == "expense":
                    account = "6950"
                elif receive_as == "asset":
                    account = "1210"
                else:
                    account = "1130" if li.get("sku") else "6950"
                debit_entries.append({"account": account, "debit": _to_base(line_total, rate, base_currency), "credit": 0.0})
            if tax_total_d > 0:
                debit_entries.append({"account": "1150", "debit": _to_base(to_stored_float(tax_total_d), rate, base_currency), "credit": 0.0})
        base_total = _to_base(total, rate, base_currency)
        if not debit_entries:
            if total <= 0:
                return
            debit_entries.append({"account": "6950", "debit": base_total, "credit": 0.0})
        entries = debit_entries + [{"account": "2110", "debit": 0.0, "credit": base_total}]
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:bill:unvoid",
            idem_create=je_idempotency_key(doc_id, "po.converted_to_bill.unvoid", "c"),
            idem_posted=je_idempotency_key(doc_id, "po.converted_to_bill.unvoid", "p"),
            memo=f"Auto JE for {doc_id} unvoided (restore bill conversion)",
            ts=doc.get("issue_date") or doc.get("finalized_at"),
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


async def create_for_return_received(session, *, company_id, user_id, cn_id: str, total_cogs: float, je_suffix: str) -> None:
    """Reversing COGS JE when goods are returned via credit note: Debit Inventory (1300) / Credit COGS (5100).

    je_suffix must be unique per receive-return call (e.g. first item_id) to avoid idempotency key collisions
    when receive-return is called multiple times on the same CN.
    """
    if total_cogs <= 0:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{cn_id}:return:{je_suffix}",
        idem_create=je_idempotency_key(cn_id, f"return:{je_suffix}", "c"),
        idem_posted=je_idempotency_key(cn_id, f"return:{je_suffix}", "p"),
        memo=f"Auto JE for {cn_id} return received (COGS reversal)",
        entries=[
            {"account": "1300", "debit": float(total_cogs), "credit": 0.0},
            {"account": "5100", "debit": 0.0, "credit": float(total_cogs)},
        ],
        metadata_={"trigger": "doc.return_received", "cn_id": cn_id},
    )


async def create_for_return_undone(session, *, company_id, user_id, cn_id: str, total_cogs: float, unique_suffix: str) -> None:
    """Reverse the COGS reversal JE when a receive-return is undone: Debit COGS (5100) / Credit Inventory (1300).

    unique_suffix must be unique per call (e.g. a UUID) so repeated undo attempts each get their own JE.
    """
    if total_cogs <= 0:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{cn_id}:return:undo:{unique_suffix}",
        idem_create=je_idempotency_key(cn_id, f"return.undo.{unique_suffix}", "c"),
        idem_posted=je_idempotency_key(cn_id, f"return.undo.{unique_suffix}", "p"),
        memo=f"Auto JE for {cn_id} return undone (COGS re-reversal)",
        entries=[
            {"account": "5100", "debit": float(total_cogs), "credit": 0.0},
            {"account": "1300", "debit": 0.0, "credit": float(total_cogs)},
        ],
        metadata_={"trigger": "doc.return_undone", "cn_id": cn_id},
    )


async def create_for_receive_undone(
    session,
    *,
    company_id,
    user_id,
    bill_id: str,
    doc: dict | None = None,
    total_cost: float,
    unique_suffix: str,
) -> None:
    """Reverse the PO-received JE when goods-received is undone: Debit AP (2110) / Credit Inventory account.

    unique_suffix must be unique per call so repeated undo attempts each get their own JE.
    """
    if total_cost <= 0:
        return
    purchase_kind = str((doc or {}).get("purchase_kind") or "inventory").strip().lower()
    credit_account = {
        "inventory": "1130",
        "expense": "6950",
        "asset": "1210",
    }.get(purchase_kind, "1130")
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{bill_id}:rcv:undo:{unique_suffix}",
        idem_create=je_idempotency_key(bill_id, f"receive.undo.{unique_suffix}", "c"),
        idem_posted=je_idempotency_key(bill_id, f"receive.undo.{unique_suffix}", "p"),
        memo=f"Auto JE for {bill_id} goods-received undone",
        entries=[
            {"account": "2110", "debit": float(total_cost), "credit": 0.0},
            {"account": credit_account, "debit": 0.0, "credit": float(total_cost)},
        ],
        metadata_={"trigger": "doc.receive_undone", "doc_id": bill_id, "purchase_kind": purchase_kind},
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
