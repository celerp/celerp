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

# Canonical goods-inventory account. Every goods movement - purchase/receive, bill, manufacturing,
# COGS relief, audit adjustment, landed-cost capitalisation - posts here so the asset account and its
# COGS relief reconcile against the SAME account. 1130 is the parent rollup; 1130-P is the postable
# leaf where goods actually live (receive/bill/mfg all debit it). The previous COGS-side "1300" was a
# placeholder that is not in the chart of accounts, so COGS credits were stranded and 1130-P was never
# relieved on sale.
_INVENTORY_ACCT = "1130-P"


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
        "inventory": _INVENTORY_ACCT,
        "expense": "6950",
        "asset": "1210",
    }.get(purchase_kind, _INVENTORY_ACCT)

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


# Landed-cost clearing accounts: capitalisable import charges park here at bill posting and
# capitalise into inventory on receipt (P3). Recoverable import VAT goes to 1150 instead (not a cost).
_LANDED_CLEARING_ACCT: dict[str, str] = {
    "freight": "1130-FRT",
    "insurance": "1130-INS",
    "duty": "1130-DTY",
    "import_vat": "1130-IVT",
}


async def landed_account_for_line(session, company_id, li: dict) -> str | None:
    """Return the clearing/receivable account for a landed-cost bill line, or None if not one.

    A line is a landed-cost component if it carries landed_cost_kind, or references a
    freight-typed item. Recoverable import VAT routes to 1150 (input VAT receivable, not capitalised);
    every other capitalisable kind routes to its clearing account.
    """
    kind = li.get("landed_cost_kind")
    recoverable = li.get("recoverable")
    if not kind:
        item_id = li.get("item_id") or li.get("entity_id")
        if not item_id:
            return None
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": str(item_id)})
        if not proj or (proj.state.get("inventory_type") or "stocked") != "freight":
            return None
        kind = proj.state.get("landed_cost_kind") or "freight"
        if recoverable is None:
            recoverable = proj.state.get("recoverable")
    if kind not in _LANDED_CLEARING_ACCT:
        return None
    if kind == "import_vat":
        # Fall back to the company default when recoverability is unspecified (worldwide: a company in a
        # recoverable-VAT jurisdiction sets import_vat_recoverable_default=True).
        if recoverable is None:
            from celerp.models.company import Company
            company = await session.get(Company, company_id)
            recoverable = bool((company.settings or {}).get("import_vat_recoverable_default")) if company else False
        if recoverable:
            return "1150"
    return _LANDED_CLEARING_ACCT[kind]


async def create_for_landed_capitalisation(
    session, *, company_id, user_id, doc_id: str, landed_by_kind: dict[str, float], receive_suffix: str,
    receive_date: str | None = None,
) -> None:
    """Capitalise received landed cost from the clearing accounts into goods inventory on receipt:
    Dr 1130-P (total) / Cr each kind's clearing account. Balances by construction.

    The bill posting (create_for_bill_conversion) parks freight/insurance/duty/non-recoverable-VAT in
    the clearing accounts; this draws the received portion down into 1130-P so that COGS, which relieves
    the item's full cost_total (base + landed) from 1130-P, reconciles against the same account.
    """
    total = round(sum(float(v or 0) for v in landed_by_kind.values()), 2)
    if total <= 0:
        return
    entries: list[dict] = [{"account": _INVENTORY_ACCT, "debit": total, "credit": 0.0}]
    for kind, amt in landed_by_kind.items():
        amt = round(float(amt or 0), 2)
        if amt:
            entries.append({"account": _LANDED_CLEARING_ACCT[kind], "debit": 0.0, "credit": amt})
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:landed-cap:{receive_suffix}",
        idem_create=je_idempotency_key(doc_id, f"landed.cap:{receive_suffix}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"landed.cap:{receive_suffix}", "p"),
        memo=f"Auto JE for {doc_id} landed-cost capitalisation",
        ts=receive_date,
        entries=entries,
        metadata_={"trigger": "doc.landed_capitalised", "doc_id": doc_id},
    )


async def create_for_bill_conversion(
    session,
    *,
    company_id,
    user_id,
    doc_id: str,
    doc: dict,
    base_currency: str = "USD",
    revert_count: int = 0,
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
            landed_acct = await landed_account_for_line(session, company_id, li)
            if li.get("account_code"):
                account = li["account_code"]
            elif receive_as == "expense":
                account = "6950"
            elif receive_as == "asset":
                account = "1210"
            elif landed_acct:
                # Landed-cost charge (freight/insurance/duty/import_vat): clearing or 1150.
                account = landed_acct
            else:
                account = _INVENTORY_ACCT if li.get("sku") else "6950"
            debit_entries.append({"account": account, "debit": _to_base(line_total, rate, base_currency), "credit": 0.0})
        if tax_total_d > 0:
            debit_entries.append({"account": "1150", "debit": _to_base(to_stored_float(tax_total_d), rate, base_currency), "credit": 0.0})

    # Doc-level shipping on a bill is inbound freight: debit the freight clearing account so the JE
    # balances (this closes the legacy gap where shipping inflated the AP credit with no debit).
    shipping_d = round_money(doc.get("shipping", 0) or 0, currency)
    if shipping_d > 0:
        debit_entries.append({"account": "1130-FRT", "debit": _to_base(to_stored_float(shipping_d), rate, base_currency), "credit": 0.0})

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
        idem_create=je_idempotency_key(doc_id, f"po.converted_to_bill:{revert_count}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"po.converted_to_bill:{revert_count}", "p"),
        memo=f"Auto JE for {doc_id} converted to bill",
        ts=doc.get("issue_date") or doc.get("finalized_at") or __import__("datetime").date.today().isoformat(),
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
                    account = _INVENTORY_ACCT if li.get("sku") else "6950"
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


async def create_for_doc_fulfilled(session, *, company_id, user_id, doc_id: str, total_cogs: float, cycle: int = 0) -> None:
    """Create COGS JE when a doc is fulfilled: Debit COGS (5100) / Credit Inventory (1130-P).

    cycle must be incremented each time a doc is re-fulfilled (e.g. use doc revert_count so that
    fulfill → revert → re-fulfill produces distinct JE idempotency keys and entity IDs).
    """
    if total_cogs <= 0:
        return
    cycle_tag = f"fulfill-{cycle}" if cycle else "fulfill"
    # Use cycle-scoped deterministic idempotency keys so retries are safe (same key → no-op)
    # while re-fulfill after revert produces a distinct JE (different cycle → different keys).
    # The original uuid4 approach was an overcorrection that broke retry idempotency.
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{cycle_tag}",
        idem_create=f"je:auto:{doc_id}:{cycle_tag}:create",
        idem_posted=f"je:auto:{doc_id}:{cycle_tag}:posted",
        memo=f"Auto JE for {doc_id} fulfilled (COGS)",
        entries=[
            {"account": "5100", "debit": float(total_cogs), "credit": 0.0},
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": float(total_cogs)},
        ],
        metadata_={"trigger": "doc.fulfilled", "doc_id": doc_id},
    )


async def void_for_doc_fulfilled(session, *, company_id, user_id, doc_id: str, cycle: int = 0) -> None:
    """Reverse the COGS JE created when a doc was fulfilled.

    cycle must match the value passed to create_for_doc_fulfilled for this fulfill cycle.
    """
    cycle_tag = f"fulfill-{cycle}" if cycle else "fulfill"
    je_id = f"je:auto:{doc_id}:{cycle_tag}"
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
            idempotency_key=f"je:auto:{doc_id}:{cycle_tag}:void",
            metadata_={"trigger": "doc.fulfillment_reversed", "doc_id": doc_id},
        )


async def create_for_return_received(session, *, company_id, user_id, cn_id: str, total_cogs: float, je_suffix: str) -> None:
    """Reversing COGS JE when goods are returned via credit note: Debit Inventory (1130-P) / Credit COGS (5100).

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
            {"account": _INVENTORY_ACCT, "debit": float(total_cogs), "credit": 0.0},
            {"account": "5100", "debit": 0.0, "credit": float(total_cogs)},
        ],
        metadata_={"trigger": "doc.return_received", "cn_id": cn_id},
    )


async def create_for_return_undone(session, *, company_id, user_id, cn_id: str, total_cogs: float, unique_suffix: str) -> None:
    """Reverse the COGS reversal JE when a receive-return is undone: Debit COGS (5100) / Credit Inventory (1130-P).

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
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": float(total_cogs)},
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
        "inventory": _INVENTORY_ACCT,
        "expense": "6950",
        "asset": "1210",
    }.get(purchase_kind, _INVENTORY_ACCT)
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
            {"account": _INVENTORY_ACCT, "debit": output_cost, "credit": 0.0},
            {"account": "5100", "debit": float(waste_cost), "credit": 0.0},
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": float(input_cost)},
        ],
        metadata_={"trigger": "mfg.order.completed", "order_id": order_id},
    )


# Inventory-audit stock adjustment accounts (O1 default - overridable later via settings):
#   shrinkage (count < system): Dr 5100 Cost of Goods Sold / Cr 1130-P Inventory
#   overage   (count > system): Dr 1130-P Inventory          / Cr 4300 Other Income
_AUDIT_SHRINKAGE_ACCT = "5100"
_AUDIT_OVERAGE_ACCT = "4300"
_AUDIT_INVENTORY_ACCT = _INVENTORY_ACCT


def _audit_je_id(list_id: str, cycle: int) -> str:
    return f"je:auto:{list_id}:audit:{cycle}"


async def create_for_audit_adjustment(
    session, *, company_id, user_id, list_id: str, shrinkage_value: float, overage_value: float, cycle: int = 0,
) -> None:
    """Post the balanced inventory write-down/up JE for an audit's stock adjustment.

    shrinkage_value = total value lost (count below system); overage_value = total value gained.
    Cycle-aware so a re-adjust after an undo posts a fresh JE rather than colliding on idempotency.
    """
    entries: list[dict] = []
    if shrinkage_value > 1e-9:
        entries.append({"account": _AUDIT_SHRINKAGE_ACCT, "debit": round(float(shrinkage_value), 2), "credit": 0.0})
        entries.append({"account": _AUDIT_INVENTORY_ACCT, "debit": 0.0, "credit": round(float(shrinkage_value), 2)})
    if overage_value > 1e-9:
        entries.append({"account": _AUDIT_INVENTORY_ACCT, "debit": round(float(overage_value), 2), "credit": 0.0})
        entries.append({"account": _AUDIT_OVERAGE_ACCT, "debit": 0.0, "credit": round(float(overage_value), 2)})
    if not entries:
        return  # nothing to post (no value change)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=_audit_je_id(list_id, cycle),
        idem_create=je_idempotency_key(list_id, f"audit.adjusted:{cycle}", "c"),
        idem_posted=je_idempotency_key(list_id, f"audit.adjusted:{cycle}", "p"),
        memo=f"Inventory audit adjustment {list_id}",
        entries=entries,
        metadata_={"trigger": "audit.adjusted", "list_id": list_id},
    )


async def void_for_audit_adjustment(session, *, company_id, user_id, list_id: str, cycle: int = 0) -> None:
    """Void the audit-adjustment JE (undo). No-op if it was never posted (zero-value adjustment)."""
    je_id = _audit_je_id(list_id, cycle)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    if row is not None and row.state.get("status") == "posted":
        await emit_event(
            session,
            company_id=company_id,
            entity_id=je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data={"reason": f"Reversed: audit {list_id} stock adjustment undone"},
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=je_idempotency_key(list_id, f"audit.undo:{cycle}", "void"),
            metadata_={"trigger": "audit.undo", "list_id": list_id},
        )


async def upsert_opening_inventory_je(
    session,
    *,
    company_id,
    user_id,
) -> None:
    """Auto-post (or update) the opening inventory JE for pre-system stock.

    Computes gap = catalog_cost_total (stocked, non-consignment, non-archived)
    minus the sum of all JE-backed balances on 1130 / 1130-P (excluding the OB
    JE itself).  If gap > ฿0.01, emits/updates je:auto:opening-inventory:{company_id}
    (debit 1130-OB, credit 3200).  If gap is negligible, voids the OB JE.

    When the gap changes (more stock added), voids the old JE and posts a fresh
    one so the amount stays current.  Idempotent: safe to call on every render.
    """
    from sqlalchemy import select as _sel

    # --- Catalog cost: sum total_cost for stocked, non-consignment, non-archived items ---
    item_rows = (
        await session.execute(
            _sel(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()

    # Inactive statuses: items no longer owned. Must match get_valuation()'s filter.
    _INACTIVE = frozenset({"archived", "deleted", "void", "sold", "fulfilled", "merged", "expired"})

    catalog_total = _Dec("0")
    for row in item_rows:
        s = row.state
        status = str(s.get("status") or "").lower()
        if status in _INACTIVE:
            continue
        if s.get("consignment_flag") == "in" or row.consignment_flag == "in":
            continue
        if (s.get("inventory_type") or "stocked") != "stocked":
            continue
        cost_total = float(s.get("cost_total") or 0)
        if cost_total:
            catalog_total += _Dec(str(cost_total))
        else:
            cost = s.get("cost_price") or s.get("cost price")
            qty = s.get("quantity") or 0
            if cost is not None:
                catalog_total += _Dec(str(cost)) * _Dec(str(qty))

    # --- JE-backed inventory: sum 1130 / 1130-P / 1130-OB across all posted JEs ---
    je_rows = (
        await session.execute(
            _sel(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()

    # Every 1130* account is inventory asset value: goods (1130-P), opening balance (1130-OB), and the
    # landed-cost clearing sub-accounts (1130-FRT/INS/DTY/IVT). catalog cost_total now includes
    # capitalised landed cost, so the JE-backed sum must cover the clearing accounts too or the landed
    # amount would show up as a spurious opening-balance gap.
    ob_je_id = f"je:auto:opening-inventory:{company_id}"
    je_backed = _Dec("0")
    ob_proj = None
    for row in je_rows:
        if row.entity_id == ob_je_id:
            ob_proj = row
            continue  # exclude OB JE itself from gap calculation
        s = row.state
        if s.get("status") != "posted":
            continue
        for entry in s.get("entries", []):
            if str(entry.get("account") or "").startswith("1130"):
                je_backed += _Dec(str(entry.get("debit") or 0))
                je_backed -= _Dec(str(entry.get("credit") or 0))

    gap = catalog_total - je_backed
    from celerp.models.company import Company as _Company
    company_obj = await session.get(_Company, company_id)
    base_currency = (company_obj.settings or {}).get("currency", "USD") if company_obj else "USD"
    needed = float(round_money(gap, base_currency)) if gap >= _Dec("0.01") else 0.0

    # Current OB JE amount (0 if not posted)
    current_amount = 0.0
    if ob_proj and ob_proj.state.get("status") == "posted":
        for entry in ob_proj.state.get("entries", []):
            if entry.get("account") == "1130-OB":
                current_amount = float(entry.get("debit") or 0)
                break

    if abs(needed - current_amount) < 0.01:
        # Amount is correct - but also void+repost if the JE is missing a ts (dateless legacy)
        if not (ob_proj and ob_proj.state.get("status") == "posted" and not ob_proj.state.get("ts")):
            return  # already correct and has a date, nothing to do

    # Void the existing OB JE if posted (amount changed or gap closed)
    if ob_proj and ob_proj.state.get("status") == "posted":
        from celerp.events.engine import emit_event as _emit
        await _emit(
            session,
            company_id=company_id,
            entity_id=ob_je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data={"reason": "opening inventory amount updated"},
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=f"opening-inv:{company_id}:void:{current_amount}",
            metadata_={"trigger": "opening_inventory.auto"},
        )

    if needed < 0.01:
        return  # gap closed, no new JE needed

    from datetime import date as _date
    today = str(_date.today())
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=ob_je_id,
        idem_create=f"opening-inv:{company_id}:c:{needed}:{today}",
        idem_posted=f"opening-inv:{company_id}:p:{needed}:{today}",
        memo="Opening inventory balance (pre-system stock)",
        entries=[
            {"account": "1130-OB", "debit": needed, "credit": 0.0},
            {"account": "3200",    "debit": 0.0,    "credit": needed},
        ],
        metadata_={"trigger": "opening_inventory.auto"},
        ts=today,
    )


def _category_inventory_account(category: str) -> str:
    """All categories map to the canonical goods-inventory account until category-level CoA mapping
    is built. (Was a 1300 placeholder that is not in the chart of accounts.)"""
    return _INVENTORY_ACCT


async def create_for_item_transform(
    session, *, company_id, user_id, parent_entity_id: str,
    parent_cost_total: float, parent_category: str, child_category: str,
) -> None:
    """Inventory reclassification JE for transform. Moves parent_cost_total exactly.
    Currently a no-op (both categories -> 1300). Scaffolded for future category-CoA mapping.
    """
    dr_account = _category_inventory_account(child_category)
    cr_account = _category_inventory_account(parent_category)
    if dr_account == cr_account:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:transform:{parent_entity_id}",
        idem_create=je_idempotency_key(parent_entity_id, "item.transform", "c"),
        idem_posted=je_idempotency_key(parent_entity_id, "item.transform", "p"),
        memo=f"Inventory transform: {parent_category} -> {child_category}",
        entries=[
            {"account": dr_account, "debit": parent_cost_total, "credit": 0.0},
            {"account": cr_account, "debit": 0.0, "credit": parent_cost_total},
        ],
        metadata_={"trigger": "item.transform", "parent_entity_id": parent_entity_id},
    )
