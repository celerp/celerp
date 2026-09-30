# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Auto journal entry creation for document lifecycle events.

Uses doc-scoped idempotency keys so the same doc can never produce
duplicate JEs regardless of trigger source (API, import, doctor repair).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal as _Dec

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.je_keys import je_idempotency_key, je_void_data
from celerp.services.line_measures import splitting_allowed
from celerp.services.money import allocate_pro_rata, checked_exchange_rate, require_doc_rate, round_money, to_base, to_decimal, to_stored_float
from celerp.services.pick import doc_bound_lots, plan_lot_draws, resolve_pick_method
from celerp.services.units import is_non_stock_line
from sqlalchemy import or_
from sqlalchemy import select as _select

# Canonical goods-inventory account. Every goods movement - purchase/receive, bill, manufacturing,
# COGS relief, audit adjustment, landed-cost capitalisation - posts here so the asset account and its
# COGS relief reconcile against the SAME account. 1130 is the parent rollup; 1130-P is the postable
# leaf where goods actually live (receive/bill/mfg all debit it). The previous COGS-side "1300" was a
# placeholder that is not in the chart of accounts, so COGS credits were stranded and 1130-P was never
# relieved on sale.
_INVENTORY_ACCT = "1130-P"

# Where a settlement exchange difference lands. Seeded in the default chart and
# backfilled for every company holding the 6000 parent, so it is always there to
# post to.
_FX_DIFFERENCE_ACCT = "6960"


# Ledger metadata key set on a doc.created written by a raw snapshot import. It is
# the only doc.created that may carry an issued document and post its entry, so
# the Doctor reads this record rather than the payload's status.
IMPORTED_SNAPSHOT = "imported_snapshot"


def import_auto_je_kind(data: dict) -> str | None:
    """Accounting operation an imported snapshot would post, or None.

    Shared by the import endpoints (which post it) and the Doctor (which only
    repairs an entry the document's own history says should exist)."""
    status = str(data.get("status") or "draft")
    total = float(data.get("total", 0) or 0)
    if status in ("void", "draft", "converted", "expired") or total <= 0:
        return None
    doc_type = str(data.get("doc_type") or "")
    if doc_type == "invoice" and status in ("sent", "final", "partial", "paid", "awaiting_payment"):
        return "invoice"
    if doc_type == "purchase_order" and status in ("received", "partially_received", "final"):
        return "purchase_order"
    if doc_type == "bill" and status in ("awaiting_payment", "partial", "paid", "final"):
        return "bill"
    return None


def _balanced_with_fx_difference(entries: list[dict]) -> list[dict]:
    """The entry, plus the exchange difference line that makes it balance.

    A receivable or payable can only be cleared at the rate it was raised at, and
    cash can only move at the rate it actually converted at. When a document is
    settled at a different rate from the one it was issued at, those two amounts
    differ and the entry is short on one side by exactly that difference. It is a
    realised exchange gain or loss, and this is the line an accountant writes by
    hand for it.

    The side is not decided here, it is read off the entry: whichever side is
    short takes the line. One rule covers a receipt and a payment, a gain and a
    loss, without a sign convention to get backwards.

    Returned untouched when the two rates agree, which is every document in the
    company's own currency. A difference of zero is not a difference, and a line
    for it would put an account with no movement on the statement of every
    document ever settled.
    """
    gap = (sum(to_decimal(e.get("debit")) for e in entries)
           - sum(to_decimal(e.get("credit")) for e in entries))
    if gap == 0:
        return entries
    short_side = "credit" if gap > 0 else "debit"
    return entries + [{
        "account": _FX_DIFFERENCE_ACCT,
        "debit": 0.0,
        "credit": 0.0,
        short_side: to_stored_float(abs(gap)),
    }]


class UnbalancedJournalEntry(ValueError):
    """An automatic journal entry whose lines do not balance in the company currency."""


async def company_currency(session, company_id) -> str:
    """The currency the company keeps its books in."""
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    return str((company.settings or {}).get("currency") or "USD").upper() if company else "USD"


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
    """Post an automatic JE. The one place its amounts become money: every line is rounded
    to the company currency, and an entry that does not balance after rounding is refused,
    so producers build their lines to balance once rounded."""
    currency = await company_currency(session, company_id)
    entries = [
        {**e,
         "debit": to_stored_float(round_money(e.get("debit") or 0, currency)),
         "credit": to_stored_float(round_money(e.get("credit") or 0, currency))}
        for e in entries
    ]
    debits = sum((to_decimal(e["debit"]) for e in entries), _Dec(0))
    credits = sum((to_decimal(e["credit"]) for e in entries), _Dec(0))
    if debits != credits:
        raise UnbalancedJournalEntry(
            f"{memo}: debits {debits} and credits {credits} {currency} do not balance"
        )
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
        data={"ts": ts} if ts else {},
        actor_id=user_id,
        location_id=None,
        source="auto_je",
        idempotency_key=idem_posted,
        metadata_=metadata_,
    )


@dataclass
class CogsResult:
    """COGS for a document with its per-line lot allocation.

    ``allocations`` is keyed by the line's index in doc["line_items"] as a
    string (JSON metadata round-trips string keys). Each entry carries the lots
    the line prices at (lot_entity_id, qty, unit_cost), the provisional_qty no
    lot could cover (priced at the bound lot's unit cost), and the line's total
    amount. ``ambiguous`` is True when at least one splittable line exceeds its
    bound lot, so bound-lot-only pricing is a guess rather than an exact cost.
    """
    total: float = 0.0
    allocations: dict[str, dict] = field(default_factory=dict)
    ambiguous: bool = False


def lot_unit_cost(state: dict) -> float:
    """A lot's per-unit cost: cost_total spread over its own quantity when that
    quantity is positive; otherwise the unit cost a zero-quantity lot keeps
    (cost_price) plus its per-unit landed cost, which is what each unit that
    arrives will be costed at."""
    cost_total = state.get("cost_total")
    qty = float(state.get("quantity") or 0)
    if cost_total is not None and qty > 0:
        return float(cost_total) / qty
    landed = sum(float(v or 0) for v in (state.get("landed_contributions") or {}).values())
    return float(state.get("cost_price") or 0) + landed


async def _span_line_lots(
    session, company_id, primary_proj, needed: float, doc_id: str | None, exclude: set[str],
) -> tuple[list[dict], float, float]:
    """Resolve a spanning line's draws across the SKU's sibling lots.

    Eligible siblings: same company, item entity, same SKU, positive quantity,
    not in exclude (lots bound to or already drawn by the document's other lines),
    and either available or reserved by doc_id (this document's own hold).
    Draw order is the bound lot first, then the effective pick method. Returns
    (lots, provisional_qty, amount); the shortfall no lot covers is priced
    provisionally at the bound lot's unit cost.
    """
    from celerp.models.company import Company

    sku = str(primary_proj.state.get("sku") or "").strip()
    company = await session.get(Company, company_id)
    method = resolve_pick_method(primary_proj.state, (company.settings or {}) if company else {})

    def _lot(entity_id: str, created_at, state: dict) -> dict:
        return {
            "entity_id": entity_id,
            "quantity": float(state.get("quantity") or 0),
            "created_at": created_at.isoformat() if created_at else "",
            "expires_at": state.get("expires_at"),
            "unit_cost": lot_unit_cost(state),
        }

    rows = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item"))).scalars().all()
    siblings: list[dict] = []
    for r in rows:
        if r.entity_id == primary_proj.entity_id or r.entity_id in exclude:
            continue
        s = r.state or {}
        if str(s.get("sku") or "").strip() != sku:
            continue
        if float(s.get("quantity") or 0) <= 1e-9:
            continue
        status = s.get("status") or "available"
        if not (status == "available"
                or (status == "reserved" and doc_id is not None and s.get("status_doc_id") == doc_id)):
            continue
        siblings.append(_lot(r.entity_id, r.created_at, s))

    primary = _lot(primary_proj.entity_id, primary_proj.created_at, primary_proj.state)
    draws, short_qty = plan_lot_draws(primary, needed, siblings, method)
    lots = [{"lot_entity_id": lot["entity_id"], "qty": take, "unit_cost": lot["unit_cost"]}
            for lot, take, _is_full in draws]
    amount = sum(take * lot["unit_cost"] for lot, take, _is_full in draws)
    if short_qty > 1e-9:
        amount += short_qty * primary["unit_cost"]
    return lots, short_qty, amount


async def compute_doc_cogs(
    session, company_id, doc: dict, *, span_lots: bool = False, doc_id: str | None = None,
) -> CogsResult:
    """Cost a document's stock lines at posting time, lot by lot.

    Each line prices at the specific lots it draws (specific identification by
    lot). A splittable line whose quantity exceeds its bound lot is flagged
    ambiguous: the remainder physically comes from sibling lots at their own
    costs, so extrapolating the bound lot's unit cost is a guess.

    span_lots=False prices the whole line at the bound lot's unit cost (the
    historical extrapolation) and leaves the ambiguity to the caller - the
    backfill refuses to post a guess. span_lots=True (live finalize) resolves
    the remainder across the SKU's other lots - available ones plus lots
    reserved by doc_id - in the effective pick order; whatever no lot covers
    stays priced at the bound lot's cost as provisional_qty.

    Lines are allocated together in document order, the way fulfillment draws
    them: a lot bound to another line, or already drawn by an earlier line's span,
    is never a sibling.

    Non-stock lines (service, freight) hold no goods and contribute nothing.
    Per-line amounts are clamped at zero so one mis-costed lot cannot cancel
    correctly costed siblings.
    """
    result = CogsResult()
    line_items = doc.get("line_items", [])
    bound = doc_bound_lots(line_items)
    span_consumed: set[str] = set()
    for index, li in enumerate(line_items):
        line_qty = float(li.get("quantity") or 0)
        if line_qty <= 0:
            continue
        item_id = li.get("item_id") or li.get("entity_id")
        if not item_id:
            continue
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": str(item_id)})
        if not proj:
            continue
        state = proj.state
        if is_non_stock_line(state.get("inventory_type"), state.get("sell_by")):
            continue
        unit_cost = lot_unit_cost(state)
        bound_qty = float(state.get("quantity") or 0)
        spans = line_qty > bound_qty + 1e-9 and splitting_allowed(state)
        if spans:
            result.ambiguous = True
        if spans and span_lots:
            lots, provisional_qty, amount = await _span_line_lots(
                session, company_id, proj, line_qty, doc_id,
                exclude=(bound - {str(item_id)}) | span_consumed)
            span_consumed.update(lot["lot_entity_id"] for lot in lots)
        else:
            lots = [{"lot_entity_id": str(item_id), "qty": line_qty, "unit_cost": unit_cost}]
            provisional_qty = 0.0
            amount = unit_cost * line_qty
        amount = max(0.0, amount)
        result.allocations[str(index)] = {
            "lots": lots, "provisional_qty": provisional_qty, "amount": amount}
        result.total += amount
    return result


def _recognition_metadata(trigger: str, doc_id: str, allocations: dict | None) -> dict:
    """Metadata of a recognition entry: its trigger and document, and the per-line COGS
    allocation snapshot when it recognizes any, which fulfillment, returns and cost
    corrections true up against (see reconcile_doc_cogs)."""
    metadata_ = {"trigger": trigger, "doc_id": doc_id}
    if allocations:
        metadata_["cogs_allocations"] = allocations
    return metadata_


async def create_for_doc_finalized(session, *, company_id, user_id, doc_id: str, doc: dict, base_currency: str = "USD", span_lots: bool = False) -> None:
    currency = doc.get("currency", "USD")
    rate = require_doc_rate(doc, base_currency)
    total_d = round_money(doc.get("total", 0), currency)
    tax_d = round_money(doc.get("tax", 0), currency)
    total = to_base(to_stored_float(total_d), rate, base_currency)
    tax = to_base(to_stored_float(tax_d), rate, base_currency)
    # Revenue is what the receivable leaves after tax, so converting each side separately
    # cannot leave the entry a unit of rounding apart.
    revenue = to_stored_float(to_decimal(total) - to_decimal(tax))
    # Use a cycle-aware suffix so re-finalize after revert creates a fresh JE entity
    # rather than hitting the dedup guard on the voided JE from the previous cycle.
    cycle = int(doc.get("revert_count", 0))
    cycle_suffix = f"fin:{cycle}" if cycle else "fin"
    je_type_key = f"invoice.finalized:{cycle}" if cycle else "invoice.finalized"
    entries = [
        {"account": "1120", "debit": total, "credit": 0.0},
        {"account": "4100", "debit": 0.0, "credit": revenue},
        {"account": "2120", "debit": 0.0, "credit": tax},
    ]
    # Recognize COGS with revenue: cost of the goods sold posts on the same JE, dated
    # the invoice date, so the P&L matches even when the invoice is never fulfilled.
    # span_lots is set only by the live finalize route, where a line exceeding its
    # bound lot recognizes at the sibling lots that will actually be drawn.
    cogs_result = await compute_doc_cogs(session, company_id, doc, span_lots=span_lots, doc_id=doc_id)
    cogs = cogs_result.total
    if cogs > 0:
        entries.append({"account": "5100", "debit": cogs, "credit": 0.0})
        entries.append({"account": _INVENTORY_ACCT, "debit": 0.0, "credit": cogs})
    metadata_ = _recognition_metadata("doc.finalized", doc_id, cogs_result.allocations)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{cycle_suffix}",
        idem_create=je_idempotency_key(doc_id, je_type_key, "c"),
        idem_posted=je_idempotency_key(doc_id, je_type_key, "p"),
        memo=f"Auto JE for {doc_id} finalized",
        ts=doc.get("finalized_at") or doc.get("issue_date"),
        entries=entries,
        metadata_=metadata_,
    )


async def create_for_doc_payment(session, *, company_id, user_id, doc_id: str, amount: float, payment_index: int, bank_account_code: str, doc_type: str = "invoice", payment_date: str, base_currency: str = "USD", doc_rate: float, settlement_rate: float) -> None:
    """Create JE for a payment.

    bank_account_code: chart account to debit. Required - no default. Always pass the
        specific bank sub-account (e.g. "1111"). Omitting raises TypeError at call time.
    doc_type: 'invoice' debits bank/credits AR; 'bill' debits AP/credits bank.
    payment_date: ISO date string (YYYY-MM-DD). Always required.
    payment_index: position of this payment in the payments list (0-based). Used as the
        idempotency key suffix so voiding and re-paying at the same amount never collides.
    base_currency: company base currency for JE conversion.
    doc_rate: the rate the document raised the receivable or payable at. That balance
        can only be cleared at the rate it was raised at, so this converts the AR/AP side.
    settlement_rate: the rate the cash actually converted at, which converts the bank
        side. Equal to doc_rate unless the payer recorded a rate of their own.

    Both rates are required - no default. A rate silently defaulting to 1 next to a real
    one would post a fabricated exchange difference, so a caller that omits either raises
    TypeError at call time instead.
    """
    ledger_amount = to_base(float(amount), checked_exchange_rate(doc_rate), base_currency)
    bank_amount = to_base(float(amount), checked_exchange_rate(settlement_rate), base_currency)
    paid_key = str(payment_index)
    if doc_type in ("bill", "purchase_order"):
        entries = [
            {"account": "2110", "debit": ledger_amount, "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": bank_amount},
        ]
    elif doc_type == "credit_note":
        # Cash refund of a credit note: money LEAVES the bank and the credit
        # balance the note held against AR is cleared.
        entries = [
            {"account": "1120", "debit": ledger_amount, "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": bank_amount},
        ]
    else:
        entries = [
            {"account": bank_account_code, "debit": bank_amount, "credit": 0.0},
            {"account": "1120", "debit": 0.0, "credit": ledger_amount},
        ]
    entries = _balanced_with_fx_difference(entries)
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


async def void_for_doc_payment(session, *, company_id, user_id, doc_id: str, payment_index: int, amount: float, bank_account_code: str, doc_type: str = "invoice", refund_date: str | None = None, base_currency: str = "USD", doc_rate: float, settlement_rate: float, refund_number: int | None = None, already_given_back: float = 0.0) -> None:
    """Reverse a payment JE, or the refunded share of it, by creating a counter-entry.

    refund_date: ISO date for the reversal JE (defaults to today if None). Used when
        void is actually a refund - the date affects bank ledger position.
    base_currency: company base currency for JE conversion.
    doc_rate, settlement_rate: the same two rates the payment posted at, so the counter
        entry is the mirror of it line for line, exchange difference included. Reversing
        at any other rate would leave the difference behind in the accounts the payment
        touched, on a document that is back to unpaid.
    refund_number: set for a refund of part or all of the payment; each refund of the
        payment is its own entry, reversing `amount` of it at the payment's rates.
    already_given_back: how much of the payment earlier refunds reversed. Each piece
        reverses what the payment's total so far converts to, less what the earlier pieces
        did, so the pieces add up to exactly what the payment posted.
    """
    def _piece(rate: float) -> float:
        rate = checked_exchange_rate(rate)
        before = to_decimal(to_base(already_given_back, rate, base_currency))
        return to_stored_float(to_decimal(to_base(to_decimal(already_given_back) + to_decimal(amount), rate, base_currency)) - before)

    ledger_amount = _piece(doc_rate)
    bank_amount = _piece(settlement_rate)
    if refund_number is None:
        kind, key, trigger = "payvoid", f"void_{payment_index}", "doc.payment.voided"
        memo = f"Auto JE for {doc_id} payment void (index {payment_index})"
    else:
        kind, key, trigger = "payrefund", f"refund_{payment_index}_{refund_number}", "doc.payment.refunded"
        memo = f"Auto JE for {doc_id} payment refund (index {payment_index})"
    if doc_type in ("bill", "purchase_order"):
        entries = [
            {"account": bank_account_code, "debit": bank_amount, "credit": 0.0},
            {"account": "2110", "debit": 0.0, "credit": ledger_amount},
        ]
    elif doc_type == "credit_note":
        # Reverse of the refund's outflow: the money comes back into the bank
        # and the credit balance is restored against AR.
        entries = [
            {"account": bank_account_code, "debit": bank_amount, "credit": 0.0},
            {"account": "1120", "debit": 0.0, "credit": ledger_amount},
        ]
    else:
        entries = [
            {"account": "1120", "debit": ledger_amount, "credit": 0.0},
            {"account": bank_account_code, "debit": 0.0, "credit": bank_amount},
        ]
    entries = _balanced_with_fx_difference(entries)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{kind}:{key}",
        idem_create=je_idempotency_key(doc_id, f"{trigger.removeprefix('doc.')}:{key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"{trigger.removeprefix('doc.')}:{key}", "p"),
        memo=memo,
        ts=refund_date,
        entries=entries,
        metadata_={"trigger": trigger, "doc_id": doc_id, "payment_index": payment_index},
    )


async def create_for_cn_application(session, *, company_id, user_id, doc_id: str, cn_id: str, amount: float, payment_index: int = 0, payment_date: str | None = None, base_currency: str = "USD", conversion_rate: float) -> None:
    """Create JE for credit note application: AR-to-AR transfer.

    payment_index disambiguates repeated applications (void + re-apply) to the same CN-invoice pair.
    base_currency: company base currency for JE conversion.
    conversion_rate: doc-to-base-currency rate (1.0 for base currency docs).
    """
    rate = checked_exchange_rate(conversion_rate)
    base_amount = to_base(float(amount), rate, base_currency)
    app_key = f"cn_apply_{cn_id}:{payment_index}"
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        # Entity id carries the application's index: the same credit note can
        # be applied to the same invoice more than once, and each application
        # must be a distinct entry so voiding one never erases another.
        je_id=f"je:auto:{doc_id}:cnapply:{cn_id}:{payment_index}",
        idem_create=je_idempotency_key(doc_id, f"cn.applied:{app_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"cn.applied:{app_key}", "p"),
        memo=f"Auto JE for credit note {cn_id} applied to {doc_id}",
        ts=payment_date,
        entries=[
            {"account": "1120", "debit": 0.0, "credit": base_amount},
            {"account": "1120", "debit": base_amount, "credit": 0.0},
        ],
        metadata_={"trigger": "cn.applied", "doc_id": doc_id, "cn_id": cn_id},
    )


def bill_line_kind(line: dict) -> str:
    """What a bill line brings in: stock, an expense or an asset. A line naming no item
    or SKU, and no kind, is an expense."""
    kind = str(line.get("receive_as") or "").strip().lower()
    return kind or ("stock" if line.get("sku") or line.get("item_id") else "expense")


def po_receipt_account(doc: dict, receive_as: str = "stock") -> str:
    """The account a purchase order receipt debits: stock by the order's purchase kind."""
    if receive_as in ("expense", "asset"):
        return {"expense": "6950", "asset": "1210"}[receive_as]
    purchase_kind = str(doc.get("purchase_kind") or "inventory").strip().lower()
    return {"expense": "6950", "asset": "1210"}.get(purchase_kind, _INVENTORY_ACCT)


async def _post_po_receipt(session, *, company_id, user_id, po_id: str, receipt_key: str | None,
                           debits: dict[str, float], receive_date: str | None) -> None:
    """Dr each receipt account / Cr AP (2110) for the sum of the rounded debits."""
    currency = await company_currency(session, company_id)
    rounded = {acct: round_money(amount, currency) for acct, amount in debits.items()}
    total = sum(rounded.values(), _Dec(0))
    if total <= 0:
        return
    entries = [{"account": acct, "debit": to_stored_float(amt), "credit": 0.0} for acct, amt in rounded.items() if amt]
    entries.append({"account": "2110", "debit": 0.0, "credit": to_stored_float(total)})
    suffix = f":{receipt_key}" if receipt_key else ""
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{po_id}:rcv{suffix}",
        idem_create=je_idempotency_key(po_id, f"po.received{suffix}", "c"),
        idem_posted=je_idempotency_key(po_id, f"po.received{suffix}", "p"),
        memo=f"Auto JE for {po_id} received",
        ts=receive_date,
        entries=entries,
        metadata_={"trigger": "doc.received", "doc_id": po_id},
    )


# Party control account and JE id suffix of an imported document's own entry.
_IMPORTED_DOC_ENTRY = {
    "invoice": ("1120", "fin"), "credit_note": ("1120", "fin"), "bill": ("2110", "bill"), "debit_note": ("2110", "dn"),
}


async def create_for_imported_document(
    session, *, company_id, user_id, doc_id: str, doc_type: str, contact_id: str | None,
    entries: list[dict], ts: str | None, suffix: str | None = None, cogs_allocations: dict | None = None,
) -> None:
    """Post the entry an imported document carries in its source books.

    `entries` are the document's own line postings on the accounts the source used,
    already in base currency; the customer or supplier control account takes the
    balancing line, named for the contact. The entry takes the id and keys of the
    document's normal recognition entry (`:fin` for the sales side, `:bill` for the
    purchase side), so a document can carry exactly one of the two. No cost of sales
    is computed: the source's own postings are the whole effect, and
    `cogs_allocations`, the per-line snapshot of the cost of sales those postings
    book, lets later deliveries, returns and cost corrections true it up as on an
    invoice finalized in Celerp. A debit note has
    no Celerp document: it posts on the bill it notes, `doc_id`, under its own
    `suffix`.
    """
    party_account, kind = _IMPORTED_DOC_ENTRY[doc_type]
    suffix = suffix or kind
    gap = sum(to_decimal(e.get("debit")) for e in entries) - sum(to_decimal(e.get("credit")) for e in entries)
    party = {"account": party_account, "debit": to_stored_float(max(-gap, _Dec(0))),
             "credit": to_stored_float(max(gap, _Dec(0)))}
    if contact_id:
        party["contact"] = contact_id
    je_type = {"fin": "invoice.finalized", "bill": "po.converted_to_bill:0"}.get(suffix, f"imported.{suffix}")
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{suffix}",
        idem_create=je_idempotency_key(doc_id, je_type, "c"),
        idem_posted=je_idempotency_key(doc_id, je_type, "p"),
        memo=f"Imported entry for {doc_id}",
        ts=ts,
        entries=[*entries, party],
        metadata_=_recognition_metadata("doc.imported", doc_id, cogs_allocations),
    )


async def create_for_po_received(
    session,
    *,
    company_id,
    user_id,
    po_id: str,
    total: float,
    doc: dict | None = None,
    base_currency: str = "USD",
    receive_date: str | None = None,
) -> None:
    """Receipt entry for a purchase order imported as already received: its whole total."""
    rate = require_doc_rate(doc or {}, base_currency)
    await _post_po_receipt(
        session, company_id=company_id, user_id=user_id, po_id=po_id, receipt_key=None,
        debits={po_receipt_account(doc or {}): to_base(float(total), rate, base_currency)},
        receive_date=receive_date,
    )


async def create_for_po_receipt(
    session, *, company_id, user_id, po_id: str, receipt_key: str, debits: dict[str, float],
    receive_date: str | None = None,
) -> None:
    """Receipt entry for one batch of goods received on a purchase order.

    debits are what the received goods cost per account, in the books' currency:
    the same amounts the receipt adds to the lots' cost."""
    await _post_po_receipt(
        session, company_id=company_id, user_id=user_id, po_id=po_id, receipt_key=receipt_key,
        debits=debits, receive_date=receive_date,
    )


async def _doc_receipt_booked(session, company_id, doc_id: str) -> dict[str, _Dec]:
    """Net debit per account of the posted receipt entries of a document."""
    prefix = f"je:auto:{doc_id}:rcv"
    rows = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix, autoescape=True),
    ))).scalars().all()
    net: dict[str, _Dec] = {}
    for row in rows:
        if row.state.get("status") != "posted" or not (row.entity_id == prefix or row.entity_id.startswith(f"{prefix}:")):
            continue
        for e in row.state.get("entries") or []:
            net[e["account"]] = net.get(e["account"], _Dec(0)) + to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
    return net


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
    currency = await company_currency(session, company_id)
    credits = {kind: round_money(amt or 0, currency) for kind, amt in landed_by_kind.items()}
    total = sum(credits.values(), _Dec(0))  # the debit is the sum of the rounded credits
    if total <= 0:
        return
    entries: list[dict] = [{"account": _INVENTORY_ACCT, "debit": to_stored_float(total), "credit": 0.0}]
    for kind, amt in credits.items():
        if amt:
            entries.append({"account": _LANDED_CLEARING_ACCT[kind], "debit": 0.0, "credit": to_stored_float(amt)})
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


async def create_for_supplier_return(
    session, *, company_id, user_id, doc_id: str, return_key: str, goods_account: str,
    goods: float, landed_by_kind: dict[str, float], return_date: str | None = None,
) -> None:
    """Goods sent back to the supplier leave the books at what they carried.

    Dr AP (2110) / Cr goods_account for the goods. Each kind of landed cost they carried
    goes back to its clearing account (Dr clearing / Cr inventory) in an entry of its own,
    the reverse of the receipt's capitalisation, so undoing the receipt returns only the
    landed cost still on the shelf."""
    currency = await company_currency(session, company_id)
    goods_d = round_money(goods or 0, currency)
    if goods_d > 0:
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:rtn:{return_key}",
            idem_create=je_idempotency_key(doc_id, f"items.returned:{return_key}", "c"),
            idem_posted=je_idempotency_key(doc_id, f"items.returned:{return_key}", "p"),
            memo=f"Auto JE for {doc_id} goods returned to supplier",
            ts=return_date,
            entries=[{"account": "2110", "debit": to_stored_float(goods_d), "credit": 0.0},
                     {"account": goods_account, "debit": 0.0, "credit": to_stored_float(goods_d)}],
            metadata_={"trigger": "doc.items_returned", "doc_id": doc_id},
        )
    landed = {kind: round_money(amt or 0, currency) for kind, amt in landed_by_kind.items()}
    landed_total = sum(landed.values(), _Dec(0))
    if landed_total > 0:
        entries = [{"account": _LANDED_CLEARING_ACCT[kind], "debit": to_stored_float(amt), "credit": 0.0}
                   for kind, amt in landed.items() if amt]
        entries.append({"account": _INVENTORY_ACCT, "debit": 0.0, "credit": to_stored_float(landed_total)})
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:landed-rtn:{return_key}",
            idem_create=je_idempotency_key(doc_id, f"landed.returned:{return_key}", "c"),
            idem_posted=je_idempotency_key(doc_id, f"landed.returned:{return_key}", "p"),
            memo=f"Auto JE for {doc_id} landed cost returned with goods",
            ts=return_date,
            entries=entries,
            metadata_={"trigger": "doc.items_returned", "doc_id": doc_id},
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

    Debit per-line expense/inventory accounts, credit AP (2110), less what the
    document's purchase order receipts already booked.
    Line-level account_code takes priority; otherwise defaults to 1130 (inventory)
    for lines with SKU, 6950 (misc expense) for lines without.
    """
    currency = doc.get("currency", "USD")
    rate = require_doc_rate(doc, base_currency)
    total_d = round_money(doc.get("total", 0) or 0, currency)
    line_items = doc.get("line_items", [])
    lines: list[tuple[str, _Dec]] = []  # (account, amount in the document currency)
    tax_total_d = _Dec(0)

    if line_items:
        for li in line_items:
            line_total = round_money(
                to_decimal(li.get("line_total") or 0) or
                to_decimal(li.get("quantity", 0)) * to_decimal(li.get("unit_price", 0)),
                currency,
            )
            if line_total <= 0:
                continue
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
                account = _INVENTORY_ACCT if bill_line_kind(li) == "stock" else "6950"
            lines.append((account, line_total))
        # Input VAT: debit the EFFECTIVE tax that create_doc rolled into `total` (line `taxes[].amount`
        # + doc_taxes), not a per-line `tax_rate` the structured-tax create path never sets.
        tax_total_d = round_money(to_decimal(doc.get("tax", 0) or 0), currency)

    # Doc-level shipping on a bill is inbound freight: debit the freight clearing account.
    shipping_d = round_money(doc.get("shipping", 0) or 0, currency)
    if total_d <= 0:
        return
    # A bill total below its lines, tax and shipping is a discount on those lines: each line's
    # cost is reduced by its share, so the debits sum to what the bill says is owed. Any other
    # gap between the parts and the total is refused rather than posted unbalanced.
    goods_d = sum((a for _, a in lines), _Dec(0))
    discount_d = goods_d + tax_total_d + shipping_d - total_d
    if 0 < discount_d <= goods_d:
        shares = allocate_pro_rata(discount_d, [a for _, a in lines], currency)
        lines = [(acct, a - share) for (acct, a), share in zip(lines, shares)]
    if tax_total_d > 0:
        lines.append(("1150", tax_total_d))
    if shipping_d > 0:
        lines.append(("1130-FRT", shipping_d))
    if not lines:
        lines.append(("6950", total_d))
    if sum((a for _, a in lines), _Dec(0)) != total_d:
        raise UnbalancedJournalEntry(
            f"Bill {doc_id}: its lines, tax and shipping do not add up to its total of {total_d} {currency}"
        )

    # AP is the bill total in base; the debits are converted line by line, and the unit
    # of rounding that conversion can leave goes to the largest debit so the entry balances.
    base_total = to_base(to_stored_float(total_d), rate, base_currency)
    debits = [to_decimal(to_base(to_stored_float(a), rate, base_currency)) for _, a in lines]
    largest = max(range(len(debits)), key=lambda i: debits[i])
    debits[largest] += to_decimal(base_total) - sum(debits, _Dec(0))
    entries = [{"account": acct, "debit": to_stored_float(d), "credit": 0.0} for (acct, _), d in zip(lines, debits)]
    entries.append({"account": "2110", "debit": 0.0, "credit": base_total})
    # What the document's purchase order receipts already booked is not booked again, so
    # receiving before or after finalizing ends in the same books.
    booked = await _doc_receipt_booked(session, company_id, doc_id)
    if booked:
        net: dict[str, _Dec] = {}
        for e in entries:
            net[e["account"]] = net.get(e["account"], _Dec(0)) + to_decimal(e["debit"]) - to_decimal(e["credit"])
        for acct, amount in booked.items():
            net[acct] = net.get(acct, _Dec(0)) - amount
        entries = [{"account": acct, "debit": to_stored_float(max(v, _Dec(0))), "credit": to_stored_float(max(-v, _Dec(0)))}
                   for acct, v in net.items() if v]
        if not entries:
            return

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


async def _void_je_if_posted(session, *, company_id, user_id, doc_id: str, je_id: str, idem_key: str, reason: str, trigger: str) -> bool:
    """Void a single auto-JE when it is currently posted. Returns True if it voided one.

    Voiding an entry already in 'void' status is a no-op (the status guard skips it),
    so callers may safely sweep a set of candidate JEs without tracking which are live.
    """
    row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    if row is None or row.state.get("status") != "posted":
        return False
    await emit_event(
        session,
        company_id=company_id,
        entity_id=je_id,
        entity_type="journal_entry",
        event_type="acc.journal_entry.voided",
        data=je_void_data(reason, row.state),
        actor_id=user_id,
        location_id=None,
        source="auto_je",
        idempotency_key=idem_key,
        metadata_={"trigger": trigger, "doc_id": doc_id},
    )
    return True


# The JE families that carry a doc's recognized economics: finalize (fin, and
# fin:{cycle} after reverts), bill conversion, the one-time COGS backfill, and
# the fulfillment COGS adjustment (cogs-adj:{cycle_tag}). These are what a doc
# void reverses and an unvoid restores. Settlement and stock-movement JEs
# (payments, credit-note applications, fulfillment, receiving, landed cost,
# returns) are not recognition: they reverse through their own flows.
_RECOGNITION_FAMILIES = ("fin", "bill", "cogs-backfill", "cogs-adj")


def _recognition_root(suffix: str) -> str | None:
    """The recognition root of a JE id suffix, or None for non-recognition JEs.

    The root is the suffix with any unvoid-restore generations stripped, so a
    restore shares its original's root: fin, fin:2, fin:unvoid, fin:2:unvoid:1
    all root to their cycle id; cogs-adj:fulfill-0:l0:unvoid:1 roots to
    cogs-adj:fulfill-0:l0. A payment (pay:0) or fulfillment (fulfill-1) suffix
    returns None.
    """
    root = re.sub(r"(?::unvoid(?::\d+)?)+$", "", suffix)
    for family in _RECOGNITION_FAMILIES:
        if root == family or root.startswith(f"{family}:"):
            return root
    return None


async def _doc_recognition_jes(session, company_id, doc_id: str) -> dict[str, Projection]:
    """suffix -> JE projection for every recognition-family auto-JE of the doc."""
    prefix = f"je:auto:{doc_id}:"
    rows = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
    ))).scalars().all()
    jes: dict[str, Projection] = {}
    for row in rows:
        if not row.entity_id.startswith(prefix):
            continue
        suffix = row.entity_id[len(prefix):]
        if _recognition_root(suffix) is not None:
            jes[suffix] = row
    return jes


async def _doc_goods_movement_jes(session, company_id, doc_id: str) -> list[str]:
    """JE id suffixes of the doc's receipt (rcv) and supplier-return (rtn) entries."""
    prefix = f"je:auto:{doc_id}:"
    rows = (await session.execute(_select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix, autoescape=True),
    ))).scalars().all()
    return [eid[len(prefix):] for eid in rows if eid[len(prefix):].split(":")[0] in ("rcv", "rtn")]


async def _doc_void_events(session, company_id, doc_id: str) -> list:
    """Every acc.journal_entry.voided ledger event on the doc's auto-JEs, oldest
    first (ledger id order)."""
    from celerp.models.ledger import LedgerEntry

    prefix = f"je:auto:{doc_id}:"
    rows = (await session.execute(
        _select(LedgerEntry)
        .where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.event_type == "acc.journal_entry.voided",
        )
        .order_by(LedgerEntry.id)
    )).scalars().all()
    return [r for r in rows if r.entity_id.startswith(prefix)]


async def void_for_doc_finalized(session, *, company_id, user_id, doc_id: str, revert_count: int = 0) -> None:
    """Void every posted recognition-family auto-JE when a doc reverts to draft
    (invoice or bill).

    The candidates are enumerated from the doc's actual JE projections, so any
    recognition JE the doc has accumulated - finalize cycles, unvoid restores,
    the COGS backfill, fulfillment COGS adjustments - is reversed without a
    fixed list to fall out of date. _void_je_if_posted skips anything already
    void, so the sweep only ever reverses what is live.

    A document reverts only once no received goods remain on it, so the entries
    that booked its receipts and its returns to the supplier reverse with it.

    revert_count: the current revert_count from doc state (before this revert
    increments it), scoping the void idempotency keys per revert cycle.
    """
    for suffix in [*await _doc_recognition_jes(session, company_id, doc_id),
                   *await _doc_goods_movement_jes(session, company_id, doc_id)]:
        await _void_je_if_posted(
            session,
            company_id=company_id,
            user_id=user_id,
            doc_id=doc_id,
            je_id=f"je:auto:{doc_id}:{suffix}",
            # Cycle- and suffix-aware so multiple revert cycles and multiple
            # live JEs in one revert each get their own void event.
            idem_key=je_idempotency_key(doc_id, f"revert_to_draft:{revert_count}:{suffix}", "void"),
            reason=f"Reversed: {doc_id} reverted to draft",
            trigger="doc.reverted_to_draft",
        )


async def void_for_doc_voided(session, *, company_id, user_id, doc_id: str) -> None:
    """Reverse every posted recognition-family auto-JE when a finalized doc is
    voided, stamping each void event with this void's batch number.

    Symmetric with create_for_doc_unvoided, which restores exactly the JEs the
    most recent batch voided. The candidates come from the doc's actual JE
    projections (finalize cycles, unvoid restores, COGS backfill, COGS
    adjustments), so nothing live is left behind and nothing settled (payments,
    fulfillment stock moves) is touched. The batch number in both the metadata
    and the idempotency key keeps every void/unvoid cycle's events distinct:
    a second cycle's voids can never dedup against the first's.
    """
    void_events = await _doc_void_events(session, company_id, doc_id)
    batch = 1 + max(
        (int((e.metadata_ or {}).get("void_batch") or 0) for e in void_events),
        default=0,
    )
    for suffix, row in (await _doc_recognition_jes(session, company_id, doc_id)).items():
        je_id = f"je:auto:{doc_id}:{suffix}"
        if (row.state or {}).get("status") != "posted":
            continue
        await emit_event(
            session,
            company_id=company_id,
            entity_id=je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data=je_void_data(f"Reversed: {doc_id} voided", row.state),
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=je_idempotency_key(doc_id, f"voided:{batch}:{suffix}", "void"),
            metadata_={"trigger": "doc.voided", "doc_id": doc_id, "void_batch": batch},
        )


async def create_for_doc_cogs_backfill(session, *, company_id, user_id, doc_id: str, cogs: float, ts: str | None) -> None:
    """Post the one-time COGS JE for a finalized invoice that predates
    COGS-at-finalize and never received its COGS at fulfillment.

    ts carries the doc's finalize-family JE date so the expense lands in the
    period that recognized the revenue; a dateless doc stays dateless.
    """
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:cogs-backfill",
        idem_create=je_idempotency_key(doc_id, "invoice.cogs_backfill", "c"),
        idem_posted=je_idempotency_key(doc_id, "invoice.cogs_backfill", "p"),
        memo=f"Auto JE for {doc_id} COGS backfill",
        ts=ts,
        entries=[
            {"account": "5100", "debit": float(cogs), "credit": 0.0},
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": float(cogs)},
        ],
        metadata_={"trigger": "doc.cogs_backfill", "doc_id": doc_id},
    )


@dataclass
class RecognizedCogs:
    """The per-line COGS a doc's live finalize-family JE recognized.

    cycle is the recognition root of that JE (fin, fin:2, ...), shared by its
    unvoid restores; allocations is the snapshot keyed by line index."""

    cycle: str
    allocations: dict


def _finalize_root(doc_id: str, je_id: str) -> str | None:
    """The finalize cycle root of a doc's JE id (fin, fin:2), or None for any other JE."""
    prefix = f"je:auto:{doc_id}:"
    if not je_id.startswith(prefix):
        return None
    root = _recognition_root(je_id[len(prefix):])
    return root if root is not None and (root == "fin" or root.startswith("fin:")) else None


async def recognized_cogs(session, company_id, doc_id: str) -> RecognizedCogs | None:
    """The COGS the doc's currently posted finalize-family JE recognized.

    Reads the allocation snapshot off the creation event of the currently posted
    finalize-family JE (fin, a re-finalize cycle, or an unvoid restore of one).
    None when no posted finalize-family JE carries a snapshot - which is every doc
    finalized before snapshots existed, where there is no recognized basis to
    true up against and no adjustment may be posted.
    """
    from celerp.models.ledger import LedgerEntry

    live = None
    for suffix, row in (await _doc_recognition_jes(session, company_id, doc_id)).items():
        if _finalize_root(doc_id, row.entity_id) is None or (row.state or {}).get("status") != "posted":
            continue
        if live is None or (
            row.created_at is not None
            and (live.created_at is None or row.created_at > live.created_at)
        ):
            live = row
    if live is None:
        return None
    created = (await session.execute(
        _select(LedgerEntry)
        .where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == live.entity_id,
            LedgerEntry.event_type == "acc.journal_entry.created",
        )
        .order_by(LedgerEntry.id.desc())
        .limit(1)
    )).scalars().first()
    allocations = ((created.metadata_ or {}) if created is not None else {}).get("cogs_allocations")
    if not allocations:
        return None
    return RecognizedCogs(cycle=_finalize_root(doc_id, live.entity_id), allocations=allocations)


def lot_cost_of_sale(state: dict) -> float:
    """What a lot costs when it leaves on a sale: its cost_total, or its unit
    cost_price times its quantity when it carries no total."""
    cost_total = state.get("cost_total")
    if cost_total is not None:
        return float(cost_total)
    return float(state.get("cost_price") or 0) * float(state.get("quantity") or 0)


async def doc_line_of_lot(session, company_id, doc_id: str, doc_state: dict, lot_id: str, lot_state: dict) -> int | None:
    """The index of the doc line a lot belongs to: the line naming the lot, else the
    line its latest fulfillment for this doc recorded, else the only line of its SKU."""
    from celerp.models.ledger import LedgerEntry

    line_items = doc_state.get("line_items", [])
    for idx, line in enumerate(line_items):
        if (line.get("entity_id") or line.get("item_id")) == lot_id:
            return idx
    rows = (await session.execute(
        _select(LedgerEntry).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == lot_id,
            LedgerEntry.event_type == "item.fulfilled",
        ).order_by(LedgerEntry.id.desc())
    )).scalars().all()
    for event in rows:
        if (event.data or {}).get("source_doc_id") != doc_id:
            continue
        idx = (event.metadata_ or {}).get("line_index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            return idx
        break
    sku = str(lot_state.get("sku") or "").strip()
    matches = [idx for idx, line in enumerate(line_items) if str(line.get("sku") or "").strip() == sku]
    return matches[0] if len(matches) == 1 else None


async def allocations_naming_lot(session, company_id, lot_id: str) -> dict[tuple[str, str, int], float]:
    """{(doc_id, finalize cycle, line index): quantity} for every current finalize
    allocation that prices a line at this lot.

    Current means the doc's latest finalize cycle, whether that cycle's JE is
    posted or voided, so a correction reaches a voided invoice when it is restored.
    A doc reverted to draft has no current cycle until it is finalized again."""
    from sqlalchemy import Text, cast

    from celerp.models.ledger import LedgerEntry

    rows = (await session.execute(
        _select(LedgerEntry.entity_id, LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.event_type == "acc.journal_entry.created",
            cast(LedgerEntry.metadata_, Text).like(f"%{lot_id}%"),
        )
    )).all()
    found: dict[tuple[str, str, int], float] = {}
    for je_id, metadata_ in rows:
        doc_id = (metadata_ or {}).get("doc_id")
        allocations = (metadata_ or {}).get("cogs_allocations") or {}
        cycle = _finalize_root(str(doc_id), je_id) if doc_id else None
        if cycle is None or not allocations:
            continue
        doc = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
        if doc is None:
            continue
        revert_count = int((doc.state or {}).get("revert_count") or 0)
        if cycle != (f"fin:{revert_count}" if revert_count else "fin"):
            continue
        for idx, alloc in allocations.items():
            qty = sum(float(lot.get("qty") or 0) for lot in alloc.get("lots", [])
                      if lot.get("lot_entity_id") == lot_id)
            if qty:
                found[(doc_id, cycle, int(idx))] = qty
    return found


async def _recorded_repricings(session, company_id, doc_id: str, cycle: str) -> dict[int, float]:
    """line index -> total cost correction recorded against the doc's finalize cycle."""
    from sqlalchemy import Text, cast

    from celerp.models.ledger import LedgerEntry

    rows = (await session.execute(
        _select(LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "item",
            cast(LedgerEntry.metadata_, Text).like("%cogs_repriced%"),
        )
    )).scalars().all()
    by_line: dict[int, float] = {}
    for metadata_ in rows:
        for rec in (metadata_ or {}).get("cogs_repriced") or []:
            if rec.get("doc_id") == doc_id and rec.get("cycle") == cycle:
                line = int(rec["line"])
                by_line[line] = by_line.get(line, 0.0) + float(rec["amount"])
    return by_line


async def _lots_out_on_doc(session, company_id, doc_id: str) -> list[Projection]:
    """The lots whose latest fulfillment event for this doc ships them (not reversed)."""
    from celerp.models.ledger import LedgerEntry

    rows = (await session.execute(
        _select(LedgerEntry.entity_id, LedgerEntry.event_type).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "item",
            LedgerEntry.event_type.in_(("item.fulfilled", "item.fulfillment_reversed")),
            LedgerEntry.data["source_doc_id"].as_string() == doc_id,
        ).order_by(LedgerEntry.id)
    )).all()
    last: dict[str, str] = {}
    for entity_id, event_type in rows:
        last[entity_id] = event_type
    lots = []
    for entity_id, event_type in sorted(last.items()):
        if event_type != "item.fulfilled":
            continue
        row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if row is not None:
            lots.append(row)
    return lots


async def reconcile_doc_cogs(
    session, *, company_id, user_id, doc_id: str, cycle_tag: str, ts: str | None,
    trigger: str, memo: str | None = None, context: dict | None = None,
) -> None:
    """Bring an invoice's booked COGS to what it recognizes today, in one entry.

    A shipped line recognizes the actual cost of the lots it shipped, plus its
    allocation's share for any quantity it has not shipped (an imported invoice can
    deliver part of a line). A line not shipped recognizes its finalize allocation plus
    every cost correction since recorded against that allocation. The difference from
    the cost of sales the invoice's live entries already book, measured as their net
    relief of inventory, whichever account carries the expense, is rounded once, for
    the whole invoice, and posted
    through create_for_doc_cogs_adjustment. An invoice with no recognized
    allocation on record posts nothing. Raises ValueError when a shipped lot
    cannot be matched to one of the invoice's lines.
    """
    recognized = await recognized_cogs(session, company_id, doc_id)
    if recognized is None:
        return
    doc = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    doc_state = (doc.state or {}) if doc is not None else {}
    shipped: dict[int, float] = {}
    shipped_qty: dict[int, float] = {}
    for lot in await _lots_out_on_doc(session, company_id, doc_id):
        idx = await doc_line_of_lot(session, company_id, doc_id, doc_state, lot.entity_id, lot.state or {})
        if idx is None:
            raise ValueError("cannot safely identify the invoice line of every shipped lot")
        shipped[idx] = shipped.get(idx, 0.0) + lot_cost_of_sale(lot.state or {})
        shipped_qty[idx] = shipped_qty.get(idx, 0.0) + float((lot.state or {}).get("quantity") or 0)
    repriced = await _recorded_repricings(session, company_id, doc_id, recognized.cycle)
    truth = sum(shipped.values())
    for idx, alloc in recognized.allocations.items():
        amount = float(alloc.get("amount") or 0)
        if int(idx) not in shipped:
            truth += amount + repriced.get(int(idx), 0.0)
            continue
        allocated = sum(float(lot.get("qty") or 0) for lot in alloc.get("lots", [])) + float(
            alloc.get("provisional_qty") or 0)
        unshipped = allocated - shipped_qty[int(idx)]
        if allocated > 0 and unshipped > 1e-9:
            truth += amount * unshipped / allocated
    booked = sum(
        float(e.get("credit") or 0) - float(e.get("debit") or 0)
        for row in (await _doc_recognition_jes(session, company_id, doc_id)).values()
        if (row.state or {}).get("status") == "posted"
        for e in (row.state or {}).get("entries", []) if e.get("account") == _INVENTORY_ACCT
    )
    await create_for_doc_cogs_adjustment(
        session, company_id=company_id, user_id=user_id, doc_id=doc_id, delta=truth - booked,
        cycle_tag=cycle_tag, doc_number=doc_state.get("doc_number") or doc_state.get("ref_id") or doc_id,
        ts=ts, trigger=trigger, memo=memo, context=context,
    )


async def create_for_doc_cogs_adjustment(
    session, *, company_id, user_id, doc_id: str, delta: float, cycle_tag: str, doc_number: str,
    ts: str | None = None, trigger: str = "doc.fulfilled", memo: str | None = None,
    context: dict | None = None,
) -> None:
    """Post one COGS true-up JE for a document (see reconcile_doc_cogs).

    A positive delta debits 5100 and relieves inventory; a negative one reverses
    that. The delta is rounded once to the company currency and nothing posts when
    it rounds to zero. cycle_tag scopes the JE id and its idempotency keys to the
    event that triggered it (fulfill-0:l0-1, reverse-1:l2, restate-<id>), so
    replaying that event is a no-op while each distinct event trues up on its
    own JE.
    """
    currency = await company_currency(session, company_id)
    rounded = round_money(delta, currency)  # one amount, used on both sides
    amount = to_stored_float(abs(rounded))
    if amount <= 0:
        return
    if rounded > 0:
        entries = [
            {"account": "5100", "debit": amount, "credit": 0.0},
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": amount},
        ]
    else:
        entries = [
            {"account": _INVENTORY_ACCT, "debit": amount, "credit": 0.0},
            {"account": "5100", "debit": 0.0, "credit": amount},
        ]
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:cogs-adj:{cycle_tag}",
        idem_create=je_idempotency_key(doc_id, f"cogs_adjustment:{cycle_tag}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"cogs_adjustment:{cycle_tag}", "p"),
        memo=memo or f"COGS adjustment for {doc_number}",
        ts=ts,
        entries=entries,
        metadata_={
            "trigger": trigger, "doc_id": doc_id, "cogs_delta": to_stored_float(rounded), **(context or {}),
        },
    )


async def create_for_doc_unvoided(session, *, company_id, user_id, doc_id: str) -> None:
    """Restore exactly what the immediately preceding void removed.

    Finds the recognition JEs the most recent void batch reversed and re-posts
    each as a new JE copying its memo, entries, and date verbatim - never
    recomputing: costs may have moved since the doc was voided, and an unvoid
    must put back the numbers the void took out, not today's. Each restore
    lives at je:auto:{doc_id}:{root}:unvoid:{n}, so repeated void/unvoid cycles
    keep every generation distinct, and a family that already holds a posted JE
    is skipped (the restore already happened). One copy mechanism covers every
    doc type and family: invoice finalize, bill conversion, COGS backfill, and
    COGS adjustments alike.

    Docs voided before batch stamping existed have per-JE void events with no
    batch number; for those, each recognition family whose latest void event
    came from a doc void (not a revert to draft) restores the JE that event
    reversed.
    """
    from celerp.models.ledger import LedgerEntry

    prefix = f"je:auto:{doc_id}:"
    jes = await _doc_recognition_jes(session, company_id, doc_id)
    void_events = await _doc_void_events(session, company_id, doc_id)

    batched = [e for e in void_events if (e.metadata_ or {}).get("void_batch")]
    if batched:
        last_batch = max(int(e.metadata_["void_batch"]) for e in batched)
        to_restore = [e.entity_id for e in batched
                      if int(e.metadata_["void_batch"]) == last_batch]
    else:
        last_void_by_root: dict[str, object] = {}
        for event in void_events:  # oldest first, so the latest event wins
            root = _recognition_root(event.entity_id[len(prefix):])
            if root is not None:
                last_void_by_root[root] = event
        to_restore = [e.entity_id for e in last_void_by_root.values()
                      if (e.metadata_ or {}).get("trigger") == "doc.voided"]

    for voided_je_id in to_restore:
        suffix = voided_je_id[len(prefix):]
        root = _recognition_root(suffix)
        source = jes.get(suffix)
        if root is None or source is None:
            continue
        family = {s: r for s, r in jes.items() if _recognition_root(s) == root}
        if any((r.state or {}).get("status") == "posted" for r in family.values()):
            continue
        state = source.state or {}
        entries = state.get("entries") or []
        if not entries:
            continue
        generation = 1 + sum(1 for s in family if s != root)
        created = (await session.execute(
            _select(LedgerEntry)
            .where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.entity_id == voided_je_id,
                LedgerEntry.event_type == "acc.journal_entry.created",
            )
            .order_by(LedgerEntry.id.desc())
            .limit(1)
        )).scalars().first()
        # The allocation snapshot rides along so fulfillment still trues up against
        # what the restored JE recognizes.
        metadata_ = {
            **_recognition_metadata("doc.unvoided", doc_id,
                                    ((created.metadata_ or {}) if created else {}).get("cogs_allocations")),
            "restores": voided_je_id,
        }
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:{root}:unvoid:{generation}",
            idem_create=je_idempotency_key(doc_id, f"unvoid:{root}:{generation}", "c"),
            idem_posted=je_idempotency_key(doc_id, f"unvoid:{root}:{generation}", "p"),
            memo=state.get("memo") or f"Auto JE for {doc_id} unvoided",
            ts=state.get("ts"),
            entries=entries,
            metadata_=metadata_,
        )


async def create_for_doc_fulfilled(session, *, company_id, user_id, doc_id: str, total_cogs: float, cycle: int = 0, ts: str | None = None) -> None:
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
        ts=ts,
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
            data=je_void_data(f"Reversed: {doc_id} fulfillment reversed", row.state),
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=f"je:auto:{doc_id}:{cycle_tag}:void",
            metadata_={"trigger": "doc.fulfillment_reversed", "doc_id": doc_id},
        )


async def create_for_return_received(session, *, company_id, user_id, cn_id: str, total_cogs: float, je_suffix: str) -> None:
    """Reversing COGS JE when goods are returned via credit note: Debit Inventory (1130-P) / Credit COGS (5100).

    je_suffix names the receive-return call, so each return received on the credit note has its own entry.
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
        ts=__import__("datetime").date.today().isoformat(),
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
        ts=__import__("datetime").date.today().isoformat(),
        entries=[
            {"account": "5100", "debit": float(total_cogs), "credit": 0.0},
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": float(total_cogs)},
        ],
        metadata_={"trigger": "doc.return_undone", "cn_id": cn_id},
    )


async def void_landed_capitalisation(session, *, company_id, user_id, doc_id: str, undo_key: str) -> None:
    """Return the landed cost a bill's receipts capitalised, less what went back with returned
    goods, to the clearing accounts."""
    rows = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
        or_(*(Projection.entity_id.startswith(f"je:auto:{doc_id}:{kind}:", autoescape=True)
              for kind in ("landed-cap", "landed-rtn"))),
    ))).scalars().all()
    for row in rows:
        await _void_je_if_posted(
            session, company_id=company_id, user_id=user_id, doc_id=doc_id, je_id=row.entity_id,
            idem_key=f"{row.entity_id}:void:{undo_key}", reason="Goods received undone",
            trigger="doc.receive_undone",
        )


async def create_for_mfg_completed(session, *, company_id, user_id, order_id: str, input_cost: float, waste_cost: float) -> None:
    # Input and waste become money first; the output is what is left of them, so the entry balances.
    currency = await company_currency(session, company_id)
    input_amt, waste_amt = round_money(input_cost, currency), round_money(waste_cost, currency)
    output_amt = max(_Dec(0), input_amt - waste_amt)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{order_id}:mfg",
        idem_create=je_idempotency_key(order_id, "mfg.completed", "c"),
        idem_posted=je_idempotency_key(order_id, "mfg.completed", "p"),
        memo=f"Auto JE for {order_id} completion",
        ts=__import__("datetime").date.today().isoformat(),
        entries=[
            {"account": _INVENTORY_ACCT, "debit": to_stored_float(output_amt), "credit": 0.0},
            {"account": "5100", "debit": to_stored_float(waste_amt), "credit": 0.0},
            {"account": _INVENTORY_ACCT, "debit": 0.0, "credit": to_stored_float(input_amt)},
        ],
        metadata_={"trigger": "mfg.order.completed", "order_id": order_id},
    )


# Inventory-audit stock adjustment accounts:
#   shrinkage (count < system): Dr 6970 Inventory Shrinkage & Write-offs / Cr 1130-P Inventory
#   overage   (count > system): Dr 1130-P Inventory          / Cr 4300 Other Income
_AUDIT_SHRINKAGE_ACCT = "6970"
_AUDIT_OVERAGE_ACCT = "4300"
_AUDIT_INVENTORY_ACCT = _INVENTORY_ACCT


# Memo prefix per list-adjustment kind; the full memo is "<prefix> <list_id>".
_ADJUST_MEMO = {"audit": "Inventory audit adjustment", "writeoff": "Inventory write-off"}


def _list_adjustment_je_id(list_id: str, kind: str, cycle: int) -> str:
    return f"je:auto:{list_id}:{kind}:{cycle}"


async def create_for_line_adjustment(
    session, *, company_id, user_id, list_id: str, kind: str, entries: list[dict], cycle: int = 0,
) -> None:
    """Post one balanced auto JE for a list terminal that adjusts stock value.

    `entries` is the caller-built balanced set of debit/credit lines: audit builds fixed
    shrinkage/overage lines; write-off builds one debit per chosen destination account against
    a single Inventory credit. Idempotency is namespaced by kind+cycle so audit and write-off
    never collide, and a re-run after an undo posts a fresh cycle rather than a duplicate.
    """
    if not entries:
        return  # nothing to post (no value change)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=_list_adjustment_je_id(list_id, kind, cycle),
        idem_create=je_idempotency_key(list_id, f"{kind}.adjusted:{cycle}", "c"),
        idem_posted=je_idempotency_key(list_id, f"{kind}.adjusted:{cycle}", "p"),
        memo=f"{_ADJUST_MEMO.get(kind, kind)} {list_id}",
        ts=__import__("datetime").date.today().isoformat(),
        entries=entries,
        metadata_={"trigger": f"{kind}.adjusted", "list_id": list_id},
    )


async def void_for_list_adjustment(session, *, company_id, user_id, list_id: str, kind: str, cycle: int = 0) -> None:
    """Void a list-adjustment JE (undo). No-op if it was never posted (zero-value adjustment)."""
    je_id = _list_adjustment_je_id(list_id, kind, cycle)
    row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    if row is not None and row.state.get("status") == "posted":
        await emit_event(
            session,
            company_id=company_id,
            entity_id=je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data=je_void_data(f"Reversed: {kind} {list_id} stock adjustment undone", row.state),
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=je_idempotency_key(list_id, f"{kind}.undo:{cycle}", "void"),
            metadata_={"trigger": f"{kind}.undo", "list_id": list_id},
        )


async def create_for_audit_adjustment(
    session, *, company_id, user_id, list_id: str, shrinkage_value: float, overage_value: float, cycle: int = 0,
) -> None:
    """Post the balanced inventory write-down/up JE for an audit's stock adjustment.

    shrinkage_value = total value lost (count below system); overage_value = total value gained.
    Thin wrapper: builds the fixed-account shrinkage/overage entries and delegates to the shared
    list-adjustment poster (kind="audit"), so audit and write-off share one posting core.
    """
    entries: list[dict] = []
    if shrinkage_value > 1e-9:
        entries.append({"account": _AUDIT_SHRINKAGE_ACCT, "debit": float(shrinkage_value), "credit": 0.0})
        entries.append({"account": _AUDIT_INVENTORY_ACCT, "debit": 0.0, "credit": float(shrinkage_value)})
    if overage_value > 1e-9:
        entries.append({"account": _AUDIT_INVENTORY_ACCT, "debit": float(overage_value), "credit": 0.0})
        entries.append({"account": _AUDIT_OVERAGE_ACCT, "debit": 0.0, "credit": float(overage_value)})
    await create_for_line_adjustment(
        session, company_id=company_id, user_id=user_id, list_id=list_id,
        kind="audit", entries=entries, cycle=cycle,
    )


async def void_for_audit_adjustment(session, *, company_id, user_id, list_id: str, cycle: int = 0) -> None:
    """Void the audit-adjustment JE (undo). No-op if it was never posted (zero-value adjustment)."""
    await void_for_list_adjustment(
        session, company_id=company_id, user_id=user_id, list_id=list_id, kind="audit", cycle=cycle,
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
    JE itself). The gap is rounded once to the company currency; a positive representable
    amount emits/updates je:auto:opening-inventory:{company_id}, while zero voids the OB JE.

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

    # Inactive statuses: items whose stock is not counted - no longer owned, or
    # (draft) not yet committed. Must match get_valuation()'s filter.
    _INACTIVE = frozenset({"archived", "deleted", "void", "sold", "fulfilled", "merged", "expired", "draft", "disposed"})

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

    # Current OB JE amount (0 if not posted)
    current_amount = 0.0
    if ob_proj and ob_proj.state.get("status") == "posted":
        for entry in ob_proj.state.get("entries", []):
            if entry.get("account") == "1130-OB":
                current_amount = float(entry.get("debit") or 0)
                break

    needed_d = round_money(gap, base_currency)
    if needed_d < 0:
        needed_d = _Dec("0")
    current_d = round_money(current_amount, base_currency)
    needed = to_stored_float(needed_d)

    if needed_d == current_d:
        # Amount is correct - but also void+repost if the JE is missing a ts (dateless legacy)
        if not (ob_proj and ob_proj.state.get("status") == "posted" and not ob_proj.state.get("ts")):
            return  # already correct and has a date, nothing to do

    # This upsert runs inside report views (balance sheet), so a period lock on
    # the OB entry must degrade to "leave the books as they are" - a report GET
    # can never fail because the lock forbids restating the opening balance.
    # Both halves (void + repost) are lock-checked BEFORE anything mutates, so
    # a lock can never leave a half-done restatement behind.
    from datetime import date as _date

    from fastapi import HTTPException as _HTTPExc

    from celerp.events.engine import _check_period_lock

    today = str(_date.today())
    try:
        if ob_proj and ob_proj.state.get("status") == "posted":
            await _check_period_lock(session, company_id, je_void_data("", ob_proj.state))
        if needed_d > 0:
            await _check_period_lock(session, company_id, {"ts": today})
    except _HTTPExc as exc:
        if exc.status_code == 422 and "locked" in str(exc.detail).lower():
            return
        raise

    # Void the existing OB JE if posted (amount changed or gap closed)
    if ob_proj and ob_proj.state.get("status") == "posted":
        from celerp.events.engine import emit_event as _emit
        await _emit(
            session,
            company_id=company_id,
            entity_id=ob_je_id,
            entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data=je_void_data("opening inventory amount updated", ob_proj.state),
            actor_id=user_id,
            location_id=None,
            source="auto_je",
            idempotency_key=f"opening-inv:{company_id}:void:{current_amount}",
            metadata_={"trigger": "opening_inventory.auto"},
        )

    if needed_d <= 0:
        return  # gap closed, no new JE needed

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
