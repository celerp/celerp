# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Auto journal entry creation for document lifecycle events.

Uses doc-scoped idempotency keys so the same doc can never produce
duplicate JEs regardless of trigger source (API, import, doctor repair).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from decimal import Decimal as _Dec

from celerp.accounting_roles import (
    CONSIGNOR_PAYABLE_FIELD,
    INVENTORY_VALUE_ROLES,
    LANDED_ROLE_BY_KIND,
    LOT_ACCOUNT_FIELD,
    AccountRole,
)
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.account_roles import (
    AmbiguousOriginError,
    ConsignmentNoCostError,
    consignor_of,
    current_settings,
    is_consigned,
    line_has_role,
    line_roles,
    lot_account,
    resolve,
    resolve_many,
    party_key,
    scope_codes,
    sold_lot_account,
    sold_lot_key,
    split_party_key,
)
from celerp.services.business_time import business_date_of
from celerp.services.je_keys import je_idempotency_key, je_void_data, unminted_payment_key
from celerp.services.line_measures import splitting_allowed
from celerp.services.lot_origin import held_value, in_stock, recorded_value
from celerp.services.money import allocate_pro_rata, checked_exchange_rate, require_doc_rate, round_money, to_base, to_decimal, to_stored_float
from celerp.services.pick import doc_bound_lots, plan_lot_draws, resolve_pick_method
from celerp.services.units import is_non_stock_line
from sqlalchemy import or_
from sqlalchemy import select as _select

if TYPE_CHECKING:
    from celerp.models.ledger import LedgerEntry

R = AccountRole


def _line(code: str, role, debit=0.0, credit=0.0) -> dict:
    """A journal line posted for ``role``: the role rides on the line as its snapshot."""
    return {"account": code, "account_roles": [str(role)], "debit": debit, "credit": credit}


def _origin_line(settings: dict, code: str, role, debit=0.0, credit=0.0) -> dict:
    """A line continuing a balance on the account it was recognized on. The role rides
    along when that account has served it; an account recorded before the company had
    posting accounts is left for the books to classify once it does."""
    if code in scope_codes(settings, role):
        return _line(code, role, debit, credit)
    return {"account": code, "debit": debit, "credit": credit}


def _lot_line(settings: dict, key: str, debit=0.0, credit=0.0) -> dict:
    """A line moving a lot's value on the account the lot recorded (account_roles.
    sold_lot_key): purchased or opening inventory, whichever that account has served,
    or for consigned goods the consignor payable, naming the consignor it is owed to."""
    code, contact = split_party_key(key)
    if code in scope_codes(settings, R.CONSIGNOR_PAYABLE):
        role = R.CONSIGNOR_PAYABLE
    elif code in scope_codes(settings, R.INVENTORY_PURCHASED):
        role = R.INVENTORY_PURCHASED
    else:
        role = R.INVENTORY_OPENING
    line = _origin_line(settings, code, role, debit, credit)
    if contact:
        line["contact"] = contact
    return line


# Ledger metadata key set on a doc.created written by a raw snapshot import. It is
# the only doc.created that may carry an issued document, so the Doctor reads this
# record rather than the payload's status.
IMPORTED_SNAPSHOT = "imported_snapshot"

# Ledger metadata key set on the doc.created of a purchase order or bill imported into
# books whose opening balances already hold it: the goods received on it are opening
# stock and what is owed on it is an opening payable, so the import posts nothing and
# only what happens on the document afterwards does.
IMPORTED_OPENING = "in_opening_balances"

# Ledger metadata key on every event the one-time correction of earlier imports wrote.
IMPORTED_CUTOVER = "imported_cutover"


def imported_issue_kind(data: dict) -> str | None:
    """What an imported snapshot was issued as, or None for one not issued.

    An issued invoice posts its entry at import. An issued purchase order or bill
    posts nothing: the opening balances hold it (IMPORTED_OPENING). Shared by the
    import endpoints and the Doctor (which only repairs an entry the document's own
    history says should exist)."""
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


def _exchange_gap(entries: list[dict]) -> _Dec:
    return (sum((to_decimal(e.get("debit")) for e in entries), _Dec(0))
            - sum((to_decimal(e.get("credit")) for e in entries), _Dec(0)))


def _fx_difference_role(entries: list[dict]) -> AccountRole | None:
    """The role of the exchange difference that balances ``entries``: a gain when the
    credit side is short, a loss when the debit side is, None when they agree."""
    gap = _exchange_gap(entries)
    if gap == 0:
        return None
    return R.FX_GAIN if gap > 0 else R.FX_LOSS


def _balanced_with_fx_difference(entries: list[dict], difference: dict | None) -> list[dict]:
    """The entry, plus the exchange difference line that makes it balance.

    A receivable or payable can only be cleared at the rate it was raised at, and
    cash can only move at the rate it actually converted at. When a document is
    settled at a different rate from the one it was issued at, those two amounts
    differ and the entry is short on one side by exactly that difference. It is a
    realised exchange gain or loss, and this is the line an accountant writes by
    hand for it.

    The side is not decided here, it is read off the entry: whichever side is
    short takes the line. One rule covers a receipt and a payment, a gain and a
    loss, without a sign convention to get backwards. ``difference`` is the line's
    account and roles: the exchange gain or loss account for new recognition, or the
    original entry's own difference line when reversing it.

    Returned untouched when the two rates agree, which is every document in the
    company's own currency. A difference of zero is not a difference, and a line
    for it would put an account with no movement on the statement of every
    document ever settled.
    """
    gap = _exchange_gap(entries)
    if gap == 0:
        return entries
    short_side = "credit" if gap > 0 else "debit"
    return entries + [{**(difference or {}), "debit": 0.0, "credit": 0.0, short_side: to_stored_float(abs(gap))}]


class UnbalancedJournalEntry(ValueError):
    """An automatic journal entry whose lines do not balance in the company currency."""


async def company_currency(session, company_id) -> str:
    """The currency the company keeps its books in."""
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    return str((company.settings or {}).get("currency") or "USD").upper() if company else "USD"


async def entry_day(session, company_id, recorded: object = None) -> str:
    """The date an automatic entry carries, and so the date its period lock is checked
    against: the company's business day of the operation it records, from the date or
    timestamp the operation recorded, or today in the company's timezone when it recorded
    none (business_date_of). Never the server's own date."""
    from fastapi import HTTPException

    try:
        return business_date_of(recorded, (await current_settings(session, company_id)).get("timezone"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    currency: str | None = None,
) -> None:
    """Post an automatic JE. The one place its amounts become money: every line is rounded
    to the company currency (*currency* when the producer already holds the books it posts
    on), and an entry that does not balance after rounding is refused, so producers build
    their lines to balance once rounded."""
    currency = currency or await company_currency(session, company_id)
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
    the line prices at (lot_entity_id, qty, unit_cost, and the inventory account the
    lot is valued in), the provisional_qty no lot could cover (costed only once
    it ships), and the line's total amount. ``by_account``
    is the total split by those inventory accounts. ``ambiguous`` is True when at
    least one splittable line exceeds its bound lot, so bound-lot-only pricing is a
    guess rather than an exact cost. ``payables`` names the consignor payable account
    each consigned lot sold for the first time is costed against, which the poster
    records on the lot (record_consignor_payables).
    """
    total: float = 0.0
    allocations: dict[str, dict] = field(default_factory=dict)
    by_account: dict[str, float] = field(default_factory=dict)
    ambiguous: bool = False
    payables: dict[str, str] = field(default_factory=dict)



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
    claimed: dict[str, float],
) -> tuple[list[dict], float, float]:
    """Resolve a spanning line's draws across the SKU's sibling lots.

    Eligible siblings: same company, item entity, same SKU, positive quantity,
    not in exclude (lots bound to or already drawn by the document's other lines),
    and either available or reserved by doc_id (this document's own hold). Every lot,
    the bound one included, offers only the units other unshipped invoices have not
    already costed (``claimed``).
    Draw order is the bound lot first, then the effective pick method. Returns
    (lots, provisional_qty, amount); the shortfall no lot covers costs nothing until
    goods for it ship, when fulfillment books their actual cost.
    """
    from celerp.models.company import Company

    sku = str(primary_proj.state.get("sku") or "").strip()
    company = await session.get(Company, company_id)
    method = resolve_pick_method(primary_proj.state, (company.settings or {}) if company else {})

    def _lot(entity_id: str, created_at, state: dict) -> dict:
        return {
            "entity_id": entity_id,
            "quantity": max(0.0, float(state.get("quantity") or 0) - claimed.get(entity_id, 0.0)),
            "created_at": created_at.isoformat() if created_at else "",
            "expires_at": state.get("expires_at"),
            "unit_cost": lot_unit_cost(state),
            "state": state,
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
        if float(s.get("quantity") or 0) - claimed.get(r.entity_id, 0.0) <= 1e-9:
            continue
        status = s.get("status") or "available"
        if not (status == "available"
                or (status == "reserved" and doc_id is not None and s.get("status_doc_id") == doc_id)):
            continue
        siblings.append(_lot(r.entity_id, r.created_at, s))

    primary = _lot(primary_proj.entity_id, primary_proj.created_at, primary_proj.state)
    draws, short_qty = plan_lot_draws(primary, needed, siblings, method)
    lots = [{"lot_entity_id": lot["entity_id"], "qty": take, "unit_cost": lot["unit_cost"], "state": lot["state"]}
            for lot, take, _is_full in draws]
    amount = sum(take * lot["unit_cost"] for lot, take, _is_full in draws)
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
    backfill refuses to post a guess. span_lots=True (live finalize) costs only
    goods on hand that no other unshipped invoice has already costed
    (unshipped_claims, in finalize order): a line within its bound lot's free
    units prices there; a splittable line beyond them resolves the remainder
    across the SKU's other lots - available ones plus lots reserved by doc_id -
    in the effective pick order; whatever no lot covers is provisional_qty and
    costs nothing until it ships, when fulfillment books its actual cost.

    Lines are allocated together in document order, the way fulfillment draws
    them: a lot bound to another line, or already drawn by an earlier line's span,
    is never a sibling.

    Non-stock lines (service, freight) hold no goods and contribute nothing.
    Per-line amounts are clamped at zero so one mis-costed lot cannot cancel
    correctly costed siblings.

    Consigned goods are costed against the consignor payable: the one the lot recorded
    on its first sale, else the role's account now (in ``payables``). Consigned goods
    with no known cost are refused.
    """
    result = CogsResult()
    payable: list[str] = []  # the consignor payable role's account, resolved once when needed

    async def sold_account(lot_id: str, state: dict) -> str:
        if not is_consigned(state) or state.get(CONSIGNOR_PAYABLE_FIELD):
            return sold_lot_account(state)
        if not payable:
            payable.append(await resolve(session, company_id, R.CONSIGNOR_PAYABLE))
        result.payables[lot_id] = payable[0]
        return payable[0]

    line_items = doc.get("line_items", [])
    bound = doc_bound_lots(line_items)
    span_consumed: set[str] = set()
    claimed: dict[str, float] = {}
    if span_lots:
        skus = {str(li.get("sku") or "").strip() for li in line_items} - {""}
        for li in line_items:
            item_id = li.get("item_id") or li.get("entity_id")
            row = await session.get(Projection, {"company_id": company_id, "entity_id": str(item_id)}) if item_id else None
            if row is not None and (row.state or {}).get("sku"):
                skus.add(str(row.state["sku"]).strip())
        for claim in await unshipped_claims(session, company_id, exclude=doc_id, skus=skus):
            claimed[claim.lot_id] = claimed.get(claim.lot_id, 0.0) + claim.qty
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
        free = max(0.0, bound_qty - claimed.get(str(item_id), 0.0))
        if span_lots and line_qty > free + 1e-9 and splitting_allowed(state):
            lots, provisional_qty, amount = await _span_line_lots(
                session, company_id, proj, line_qty, doc_id,
                exclude=(bound - {str(item_id)}) | span_consumed, claimed=claimed)
            span_consumed.update(lot["lot_entity_id"] for lot in lots)
        elif span_lots and line_qty > free + 1e-9:
            lots = [{"lot_entity_id": str(item_id), "qty": free, "unit_cost": unit_cost, "state": state}
                    ] if free > 1e-9 else []
            provisional_qty = line_qty - free
            amount = unit_cost * free
        else:
            lots = [{"lot_entity_id": str(item_id), "qty": line_qty, "unit_cost": unit_cost, "state": state}]
            provisional_qty = 0.0
            amount = unit_cost * line_qty
        amount = max(0.0, amount)
        states = {lot["lot_entity_id"]: lot.pop("state") for lot in lots}
        states.setdefault(str(item_id), state)
        for lot_id, lot_state in states.items():
            if is_consigned(lot_state) and lot_state.get("cost_total") is None:
                raise ConsignmentNoCostError(str(lot_state.get("sku") or ""))
        # A lot's cost can only move on the account it is valued in, so a costed line
        # names it, and refuses when the lot's account cannot be proven.
        for lot in lots:
            lot["account"] = await sold_account(lot["lot_entity_id"], states[lot["lot_entity_id"]]) if amount > 0 else None
        if amount > 0:
            parts = {lot["lot_entity_id"]: lot["qty"] * lot["unit_cost"] for lot in lots}
            for lot_id, share in _shares(parts, amount).items():
                code = await sold_account(lot_id, states[lot_id])
                if is_consigned(states[lot_id]):
                    code = party_key(code, await consignor_of(session, company_id, lot_id, states[lot_id]))
                result.by_account[code] = result.by_account.get(code, 0.0) + share
        result.allocations[str(index)] = {
            "lots": lots, "provisional_qty": provisional_qty, "amount": amount}
        result.total += amount
    return result


async def record_consignor_payables(session, company_id, user_id, payables: dict[str, str]) -> None:
    """Record on each consigned lot the consignor payable its first sale was costed
    against (CogsResult.payables), where every later reversal or settlement of that
    sale moves (account_roles.sold_lot_account)."""
    for lot_id, code in sorted(payables.items()):
        await emit_event(
            session, company_id=company_id, entity_id=lot_id, entity_type="item",
            event_type="item.consignor_payable.recorded", data={CONSIGNOR_PAYABLE_FIELD: code},
            actor_id=user_id, location_id=None, source="auto_je",
            idempotency_key=f"consignor-payable:{lot_id}", metadata_={})


def _shares(parts: dict[str, float], amount: float) -> dict[str, float]:
    """``amount`` split in proportion to ``parts``, or all on the first part when no
    part carries weight."""
    weight = sum(v for v in parts.values() if v > 0)
    if weight <= 0:
        return {next(iter(parts)): amount}
    return {k: amount * v / weight for k, v in parts.items() if v > 0}


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
    # Recognize COGS with revenue: cost of the goods sold posts on the same JE, dated
    # the invoice date, so the P&L matches even when the invoice is never fulfilled.
    # span_lots is set only by the live finalize route, where a line exceeding its
    # bound lot recognizes at the sibling lots that will actually be drawn.
    cogs_result = await compute_doc_cogs(session, company_id, doc, span_lots=span_lots, doc_id=doc_id)
    cogs = cogs_result.total
    roles = [R.RECEIVABLE, R.SALES_REVENUE]
    if tax:
        roles.append(R.TAX_OUTPUT)
    if cogs > 0:
        roles.append(R.COGS)
    acc = await resolve_many(session, company_id, roles)
    entries = [
        _line(acc[R.RECEIVABLE], R.RECEIVABLE, debit=total),
        _line(acc[R.SALES_REVENUE], R.SALES_REVENUE, credit=revenue),
    ]
    if tax:
        entries.append(_line(acc[R.TAX_OUTPUT], R.TAX_OUTPUT, credit=tax))
    if cogs > 0:
        entries += _cogs_lines(await current_settings(session, company_id), acc[R.COGS],
                               cogs_result.by_account, await company_currency(session, company_id))
    await record_consignor_payables(session, company_id, user_id, cogs_result.payables)
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


_PURCHASE_TYPES = ("bill", "purchase_order")


def _control_role(doc_type: str) -> AccountRole:
    """The party control role a document's balance sits in."""
    return R.PAYABLE if doc_type in _PURCHASE_TYPES else R.RECEIVABLE


async def party_origin(session, company_id, doc_id: str, role, settings: dict | None = None) -> str | None:
    """The control account a document's receivable or payable was recognized on, read
    off its own recognition entry: the line that snapshots the role, or, for an entry
    from before snapshots, the line whose account served the role then, else the line
    the source system named the party on. None when the document recognized no
    balance. More than one account is reported, never guessed."""
    if settings is None:
        settings = await current_settings(session, company_id)
    rows = [row for suffix, row in (await _doc_recognition_jes(session, company_id, doc_id)).items()
            if suffix.split(":")[0] in ("fin", "bill")]
    posted = [row for row in rows if (row.state or {}).get("status") == "posted"]
    posted += await _doc_receipt_jes(session, company_id, doc_id)
    codes: set[str] = set()
    for row in posted or rows:
        entries = (row.state or {}).get("entries") or []
        found = {e["account"] for e in entries if line_has_role(settings, e, role)}
        if not found:
            found = {e["account"] for e in entries if e.get("contact") and e.get("account_roles") is None}
        codes |= found
    if len(codes) > 1:
        raise AmbiguousOriginError(str(role), f"Document {doc_id}", codes)
    return next(iter(codes), None)


async def _settlement_accounts(session, company_id, *, doc_id: str, control: AccountRole, bank: str | None,
                               entries_for) -> list[dict]:
    """A settlement entry: its control line on the document's own origin account, its
    bank line on ``bank`` (the default deposit account when the payment names none),
    and any exchange difference, all resolved from one read of the company's roles.

    ``entries_for(control_line, bank_code)`` returns the entry's two lines."""
    settings = await current_settings(session, company_id)
    origin = await party_origin(session, company_id, doc_id, control, settings)
    probe = entries_for(lambda debit=0.0, credit=0.0: {"debit": debit, "credit": credit}, "")
    roles = [r for r in (None if origin else control, None if bank else R.DEFAULT_DEPOSIT,
                         _fx_difference_role(probe)) if r is not None]
    acc = await resolve_many(session, company_id, roles) if roles else {}
    if origin:
        def control_line(debit=0.0, credit=0.0):
            return _origin_line(settings, origin, control, debit, credit)
    else:
        def control_line(debit=0.0, credit=0.0):
            return _line(acc[control], control, debit, credit)
    if bank:
        entries = entries_for(control_line, bank)
    else:
        entries = entries_for(control_line, acc[R.DEFAULT_DEPOSIT])
    fx_role = _fx_difference_role(entries)
    return _balanced_with_fx_difference(entries, _line(acc[fx_role], fx_role) if fx_role else None)


def payment_amounts(*, amount: float, base_currency: str, doc_rate: float, settlement_rate: float,
                    already_given_back: float = 0.0) -> tuple[float, float]:
    """What *amount* of a payment moves on its receivable or payable (at *doc_rate*) and
    on its bank (at *settlement_rate*), after *already_given_back* of it was given back.
    Each side is what the running total converts to, less what *already_given_back*
    converts to, so the pieces of a payment add up to exactly what it posted, in
    whatever order they are given back and restored."""
    def _piece(rate: float) -> float:
        rate = checked_exchange_rate(rate)
        before = to_decimal(to_base(already_given_back, rate, base_currency))
        return to_stored_float(to_decimal(to_base(to_decimal(already_given_back) + to_decimal(amount), rate, base_currency)) - before)

    return _piece(doc_rate), _piece(settlement_rate)


def payment_lines(doc_type: str, control, bank: dict, ledger_amount: float, bank_amount: float, *,
                  returning: bool = False) -> list[dict]:
    """The two lines a payment posts, or with *returning* the two that give it back:
    the line on *bank* moving *bank_amount* and ``control(debit=, credit=)``, the
    receivable or payable line, moving *ledger_amount*. A bill payment clears AP and a
    credit note's cash refund clears the credit balance it held against AR, so money
    leaves the bank; any other payment brings it in. Giving back moves it the other way."""
    bank_line = {**bank, "debit": 0.0, "credit": 0.0}
    if (doc_type in _PURCHASE_TYPES or doc_type == "credit_note") != returning:
        return [control(debit=ledger_amount), {**bank_line, "credit": bank_amount}]
    return [{**bank_line, "debit": bank_amount}, control(credit=ledger_amount)]


async def create_for_doc_payment(session, *, company_id, user_id, doc_id: str, amount: float, payment_index: int, bank_account_code: str | None, doc_type: str = "invoice", payment_date: str, base_currency: str = "USD", doc_rate: float, settlement_rate: float) -> None:
    """Create JE for a payment.

    bank_account_code: chart account the cash moves through. None only for a payment
        recorded without one, which moves through the company's default deposit account.
    doc_type: 'invoice' debits bank/credits AR; 'bill' debits AP/credits bank.
    payment_date: ISO date string (YYYY-MM-DD). Always required.
    payment_index: position of this payment in the payments list (0-based). Used as the
        idempotency key suffix so voiding and re-paying at the same amount never collides.
    base_currency: company base currency for JE conversion.
    doc_rate: the rate the document raised the receivable or payable at. That balance
        can only be cleared at the rate it was raised at, so this converts the AR/AP side.
    settlement_rate: the rate the cash actually converted at, which converts the bank
        side. Equal to doc_rate unless the payer recorded a rate of their own.

    The receivable or payable cleared is the account the document recognized it on,
    whatever the role points at today.

    Both rates are required - no default. A rate silently defaulting to 1 next to a real
    one would post a fabricated exchange difference, so a caller that omits either raises
    TypeError at call time instead.
    """
    paid_key = str(payment_index)
    ledger_amount, bank_amount = payment_amounts(amount=amount, base_currency=base_currency, doc_rate=doc_rate,
                                                 settlement_rate=settlement_rate)

    def entries_for(control, bank):
        return payment_lines(doc_type, control, {"account": bank}, ledger_amount, bank_amount)

    entries = await _settlement_accounts(
        session, company_id, doc_id=doc_id, control=_control_role(doc_type), bank=bank_account_code,
        entries_for=entries_for)
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:pay:{paid_key}",
        idem_create=je_idempotency_key(doc_id, f"invoice.paid:{paid_key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"invoice.paid:{paid_key}", "p"),
        memo=f"Auto JE for {doc_id} payment",
        ts=payment_date,
        currency=base_currency.upper(),
        entries=entries,
        metadata_={"trigger": "doc.payment.received", "doc_id": doc_id, "payment_index": payment_index},
    )


def _reversal_template(entry: dict) -> dict:
    """A line on the same account, with the same role snapshot, as ``entry``."""
    out = {"account": entry["account"], "debit": 0.0, "credit": 0.0}
    if entry.get("account_roles") is not None:
        out["account_roles"] = list(entry["account_roles"])
    return out


async def payment_return_entries(session, company_id, *, doc_id: str, payment_index: int, doc_type: str,
                                 bank_account_code: str | None, amount: float, already_given_back: float,
                                 base_currency: str, doc_rate: float, settlement_rate: float) -> list[dict]:
    """The balanced lines that give back *amount* of a payment after *already_given_back*
    of it was given back (``payment_amounts``), on the accounts the payment's own entry
    posted to - its bank, its receivable or payable, and its exchange difference -
    never on what the company's roles point at today. A payment with no entry of its
    own (recorded before automatic entries existed) gives back on its document's origin
    account and *bank_account_code*. A restored piece posts these lines swapped."""
    ledger_amount, bank_amount = payment_amounts(
        amount=amount, base_currency=base_currency, doc_rate=doc_rate, settlement_rate=settlement_rate,
        already_given_back=already_given_back)
    outflow = doc_type in _PURCHASE_TYPES or doc_type == "credit_note"

    def entries_for(control, bank):
        bank_line = bank if isinstance(bank, dict) else {"account": bank}
        return payment_lines(doc_type, control, bank_line, ledger_amount, bank_amount, returning=True)

    original = await session.get(Projection, {"company_id": company_id, "entity_id": f"je:auto:{doc_id}:pay:{payment_index}"})
    posted = (original.state or {}).get("entries") or [] if original is not None else []
    if len(posted) < 2:
        return await _settlement_accounts(
            session, company_id, doc_id=doc_id, control=_control_role(doc_type), bank=bank_account_code,
            entries_for=entries_for)
    # The payment's entry is [control, bank] for an outflow and [bank, control] for
    # a receipt, then its exchange difference, if any.
    control_entry, bank_entry = (posted[0], posted[1]) if outflow else (posted[1], posted[0])
    control_template = _reversal_template(control_entry)

    def control_line(debit=0.0, credit=0.0):
        return {**control_template, "debit": debit, "credit": credit}
    entries = entries_for(control_line, _reversal_template(bank_entry))
    difference = _reversal_template(posted[2]) if len(posted) > 2 else None
    if difference is None and _fx_difference_role(entries) is not None:
        fx_role = _fx_difference_role(entries)
        difference = _line((await resolve_many(session, company_id, [fx_role]))[fx_role], fx_role)
    return _balanced_with_fx_difference(entries, difference)


async def void_for_doc_payment(session, *, company_id, user_id, doc_id: str, payment_index: int, amount: float, bank_account_code: str | None, doc_type: str = "invoice", refund_date: str | None = None, base_currency: str = "USD", doc_rate: float, settlement_rate: float, refund_number: int | None = None, already_given_back: float = 0.0) -> None:
    """Reverse a payment JE, or the refunded share of it, by creating a counter-entry.

    The counter-entry posts to the accounts the payment's own entry posted to - its
    bank, its receivable or payable, and its exchange difference - never to what the
    company's roles point at today. A payment with no entry of its own (recorded before
    automatic entries existed) reverses on its document's origin account and
    ``bank_account_code``.

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
    if refund_number is None:
        kind, key, trigger = "payvoid", f"void_{payment_index}", "doc.payment.voided"
        memo = f"Auto JE for {doc_id} payment void (index {payment_index})"
    else:
        kind, key, trigger = "payrefund", f"refund_{payment_index}_{refund_number}", "doc.payment.refunded"
        memo = f"Auto JE for {doc_id} payment refund (index {payment_index})"
    op = trigger.removeprefix("doc.")
    key = await unminted_payment_key(session, company_id, doc_id, op, key)
    entries = await payment_return_entries(
        session, company_id, doc_id=doc_id, payment_index=payment_index, doc_type=doc_type,
        bank_account_code=bank_account_code, amount=amount, already_given_back=already_given_back,
        base_currency=base_currency, doc_rate=doc_rate, settlement_rate=settlement_rate,
    )
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{kind}:{key}",
        idem_create=je_idempotency_key(doc_id, f"{op}:{key}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"{op}:{key}", "p"),
        memo=memo,
        ts=refund_date,
        currency=base_currency.upper(),
        entries=entries,
        metadata_={"trigger": trigger, "doc_id": doc_id, "payment_index": payment_index},
    )


async def create_for_cn_application(session, *, company_id, user_id, doc_id: str, cn_id: str, amount: float, payment_index: int = 0, payment_date: str | None = None, base_currency: str = "USD", conversion_rate: float) -> None:
    """Create JE for credit note application: AR-to-AR transfer.

    The invoice's receivable is credited on the account the invoice recognized it on,
    and the credit note's own receivable debited on the account it recognized it on; a
    credit note that recognized nothing of its own (one issued in Celerp) clears against
    the invoice's account, so the application moves nothing between accounts.

    payment_index disambiguates repeated applications (void + re-apply) to the same CN-invoice pair.
    base_currency: company base currency for JE conversion.
    conversion_rate: doc-to-base-currency rate (1.0 for base currency docs).
    """
    rate = checked_exchange_rate(conversion_rate)
    base_amount = to_base(float(amount), rate, base_currency)
    app_key = f"cn_apply_{cn_id}:{payment_index}"
    settings = await current_settings(session, company_id)
    invoice_ar = await party_origin(session, company_id, doc_id, R.RECEIVABLE, settings)
    if invoice_ar is None:
        invoice_ar = (await resolve_many(session, company_id, [R.RECEIVABLE]))[R.RECEIVABLE]
    cn_ar = await party_origin(session, company_id, cn_id, R.RECEIVABLE, settings) or invoice_ar
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
            _origin_line(settings, invoice_ar, R.RECEIVABLE, credit=base_amount),
            _origin_line(settings, cn_ar, R.RECEIVABLE, debit=base_amount),
        ],
        metadata_={"trigger": "cn.applied", "doc_id": doc_id, "cn_id": cn_id},
    )


def bill_line_kind(line: dict) -> str:
    """What a bill line brings in: stock, an expense or an asset. A line naming no item
    or SKU, and no kind, is an expense."""
    kind = str(line.get("receive_as") or "").strip().lower()
    return kind or ("stock" if line.get("sku") or line.get("item_id") else "expense")


def po_receipt_role(doc: dict, receive_as: str = "stock") -> AccountRole:
    """The role a purchase order receipt debits: stock by the order's purchase kind."""
    if receive_as in ("expense", "asset"):
        return {"expense": R.GENERAL_EXPENSE, "asset": R.FIXED_ASSETS}[receive_as]
    purchase_kind = str(doc.get("purchase_kind") or "inventory").strip().lower()
    return {"expense": R.GENERAL_EXPENSE, "asset": R.FIXED_ASSETS}.get(purchase_kind, R.INVENTORY_PURCHASED)


async def _post_po_receipt(session, *, company_id, user_id, po_id: str, receipt_key: str | None,
                           debits: dict[AccountRole | str, float], receive_date: str | None) -> None:
    """Dr each receipt debit / Cr AP for the sum of the rounded debits.

    A role key posts to the role's account; an account key is the inventory account
    of the lot the goods were added to. AP continues on the account the order's
    earlier receipts recognized it on."""
    currency = await company_currency(session, company_id)
    rounded = {key: round_money(amount, currency) for key, amount in debits.items()}
    total = sum(rounded.values(), _Dec(0))
    if total <= 0:
        return
    settings = await current_settings(session, company_id)
    ap = await party_origin(session, company_id, po_id, R.PAYABLE, settings)
    roles = [k for k, amt in rounded.items() if amt and isinstance(k, AccountRole)]
    acc = await resolve_many(session, company_id, [*roles, *([] if ap else [R.PAYABLE])])
    entries = [_line(acc[k], k, debit=to_stored_float(amt)) if isinstance(k, AccountRole)
               else _lot_line(settings, k, debit=to_stored_float(amt))
               for k, amt in rounded.items() if amt]
    entries.append(_origin_line(settings, ap, R.PAYABLE, credit=to_stored_float(total)) if ap
                   else _line(acc[R.PAYABLE], R.PAYABLE, credit=to_stored_float(total)))
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


# JE id suffix of an imported document's own entry.
_IMPORTED_DOC_SUFFIX = {"invoice": "fin", "credit_note": "fin", "bill": "bill", "debit_note": "dn"}


async def create_for_imported_document(
    session, *, company_id, user_id, doc_id: str, doc_type: str, contact_id: str | None,
    party_account: str, entries: list[dict], ts: str | None, suffix: str | None = None,
    cogs_allocations: dict | None = None,
) -> None:
    """Post the entry an imported document carries in its source books.

    `entries` are the document's own line postings on the accounts the source used,
    already in base currency; `party_account`, the customer or supplier control account
    the source kept the document's balance on, takes the balancing line, named for the
    contact. The entry takes the id and keys of the
    document's normal recognition entry (`:fin` for the sales side, `:bill` for the
    purchase side), so a document can carry exactly one of the two. No cost of sales
    is computed: the source's own postings are the whole effect, and
    `cogs_allocations`, the per-line snapshot of the cost of sales those postings
    book, lets later deliveries, returns and cost corrections true it up as on an
    invoice finalized in Celerp. A debit note has
    no Celerp document: it posts on the bill it notes, `doc_id`, under its own
    `suffix`.
    """
    suffix = suffix or _IMPORTED_DOC_SUFFIX[doc_type]
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


async def create_for_po_receipt(
    session, *, company_id, user_id, po_id: str, receipt_key: str, debits: dict[AccountRole | str, float],
    receive_date: str | None = None,
) -> None:
    """Receipt entry for one batch of goods received on a purchase order.

    debits are what the received goods cost per role, or per lot inventory account
    for goods added to stock, in the books' currency: the same amounts the receipt
    adds to the lots' cost."""
    await _post_po_receipt(
        session, company_id=company_id, user_id=user_id, po_id=po_id, receipt_key=receipt_key,
        debits=debits, receive_date=receive_date,
    )


def _net_key(settings: dict, entry: dict) -> tuple[str, tuple[str, ...]]:
    """What a line nets against: its account and what it was posted for."""
    return entry["account"], tuple(sorted(line_roles(settings, entry)))


_LOT_ROLES = (R.INVENTORY_OPENING.value, R.INVENTORY_PURCHASED.value)


def _net_group(roles: tuple[str, ...]) -> tuple[str, ...]:
    """Lines that net together: a lot's value is one thing whether its account holds
    opening or purchased inventory."""
    return _LOT_ROLES if roles and set(roles) <= set(_LOT_ROLES) else roles


async def _doc_receipt_jes(session, company_id, doc_id: str) -> list[Projection]:
    """The posted receipt entries of a document."""
    prefix = f"je:auto:{doc_id}:rcv"
    rows = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix, autoescape=True),
    ))).scalars().all()
    return [row for row in rows if row.state.get("status") == "posted"
            and (row.entity_id == prefix or row.entity_id.startswith(f"{prefix}:"))]


async def _doc_receipt_booked(session, company_id, doc_id: str, settings: dict) -> dict[tuple, _Dec]:
    """Net debit per account and role of the posted receipt entries of a document."""
    net: dict[tuple, _Dec] = {}
    for row in await _doc_receipt_jes(session, company_id, doc_id):
        for e in row.state.get("entries") or []:
            key = _net_key(settings, e)
            net[key] = net.get(key, _Dec(0)) + to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
    return net


async def landed_role_for_line(session, company_id, li: dict) -> AccountRole | None:
    """The role a landed-cost bill line posts to, or None if it is not one.

    A line is a landed-cost component if it carries landed_cost_kind, or references a
    freight-typed item. Recoverable import VAT is input tax (not capitalised); every
    other capitalisable kind parks in its clearing role.
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
    if kind not in LANDED_ROLE_BY_KIND:
        return None
    if kind == "import_vat":
        # Fall back to the company default when recoverability is unspecified (worldwide: a company in a
        # recoverable-VAT jurisdiction sets import_vat_recoverable_default=True).
        if recoverable is None:
            from celerp.models.company import Company
            company = await session.get(Company, company_id)
            recoverable = bool((company.settings or {}).get("import_vat_recoverable_default")) if company else False
        if recoverable:
            return R.TAX_INPUT
    role = LANDED_ROLE_BY_KIND[kind]
    # A charge the bill posts to an account of its own is landed cost only when that
    # account has served as the kind's clearing account; anywhere else it is that account's.
    code = li.get("account_code")
    if code and code not in scope_codes(await current_settings(session, company_id), role):
        return None
    return role


def _inventory_lines(settings: dict, total: _Dec, by_account: dict[str, float], currency: str, *,
                     debit: bool) -> list[dict]:
    """``total`` on the inventory accounts of the lots it belongs to, split in proportion
    to ``by_account`` so the lines sum to ``total`` exactly."""
    codes = sorted(code for code, v in by_account.items() if v > 0)
    shares = allocate_pro_rata(total, [to_decimal(by_account[c]) for c in codes], currency)
    return [_lot_line(settings, code, debit=to_stored_float(share) if debit else 0.0,
                      credit=0.0 if debit else to_stored_float(share))
            for code, share in zip(codes, shares) if share]


_LANDED_ROLES = frozenset(role.value for role in LANDED_ROLE_BY_KIND.values())


async def landed_clearing(session, company_id, doc_id: str, settings: dict) -> dict[str, dict[str, _Dec]]:
    """Per landed-cost role, the clearing accounts a bill's charges sit in and how much
    is on each, read off the bill's own entry; for a bill received before it was
    finalized, off what its receipts drew from clearing."""
    bill = await session.get(Projection, {"company_id": company_id, "entity_id": f"je:auto:{doc_id}:bill"})
    if bill is not None and (bill.state or {}).get("status") == "posted":
        rows, sign = [bill], 1
    else:
        rows = [row for row in (await session.execute(_select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "journal_entry",
            Projection.entity_id.startswith(f"je:auto:{doc_id}:landed-cap:", autoescape=True),
        ))).scalars().all() if row.state.get("status") == "posted"]
        sign = -1
    held: dict[str, dict[str, _Dec]] = {}
    for row in rows:
        for e in row.state.get("entries") or []:
            amount = sign * (to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0))
            for role in _LANDED_ROLES.intersection(line_roles(settings, e)):
                on_role = held.setdefault(role, {})
                on_role[e["account"]] = on_role.get(e["account"], _Dec(0)) + amount
    return held


async def _clearing_lines(session, company_id, doc_id: str, settings: dict, amounts: dict[str, _Dec],
                          currency: str, *, debit: bool) -> list[dict]:
    """Each kind of landed cost in ``amounts`` on the clearing account the bill parked
    it in, split by what the bill put on each where it used more than one. A charge the
    bill has not recognized yet draws on the kind's current clearing account."""
    held = await landed_clearing(session, company_id, doc_id, settings)
    parked = {kind: {c: v for c, v in held.get(LANDED_ROLE_BY_KIND[kind].value, {}).items() if v > 0}
              for kind, amt in amounts.items() if amt}
    unheld = [LANDED_ROLE_BY_KIND[kind] for kind, codes in parked.items() if not codes]
    acc = await resolve_many(session, company_id, unheld) if unheld else {}
    lines = []
    for kind, codes in parked.items():
        role = LANDED_ROLE_BY_KIND[kind]
        codes = codes or {acc[role]: _Dec(1)}
        order = sorted(codes)
        for code, share in zip(order, allocate_pro_rata(amounts[kind], [codes[c] for c in order], currency)):
            if share:
                amount = to_stored_float(share)
                lines.append(_line(code, role, debit=amount if debit else 0.0, credit=0.0 if debit else amount))
    return lines


async def create_for_landed_capitalisation(
    session, *, company_id, user_id, doc_id: str, landed_by_kind: dict[str, float],
    landed_by_account: dict[str, float], receive_suffix: str, receive_date: str | None = None,
) -> None:
    """Capitalise received landed cost from the clearing accounts into goods inventory on receipt:
    Dr the receiving lots' inventory accounts (``landed_by_account``) / Cr the clearing account
    the bill parked each kind in. Balances by construction.

    The bill posting (create_for_bill_conversion) parks freight/insurance/duty/non-recoverable-VAT in
    the clearing accounts; this draws the received portion down into inventory so that COGS, which
    relieves the item's full cost_total (base + landed), reconciles against the same account.
    """
    currency = await company_currency(session, company_id)
    credits = {kind: round_money(amt or 0, currency) for kind, amt in landed_by_kind.items()}
    total = sum(credits.values(), _Dec(0))  # the debit is the sum of the rounded credits
    if total <= 0:
        return
    settings = await current_settings(session, company_id)
    entries = [*_inventory_lines(settings, total, landed_by_account, currency, debit=True),
               *await _clearing_lines(session, company_id, doc_id, settings, credits, currency, debit=False)]
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
    session, *, company_id, user_id, doc_id: str, return_key: str, goods: dict[AccountRole | str, float],
    landed_by_kind: dict[str, float], landed_by_account: dict[str, float], return_date: str | None = None,
) -> None:
    """Goods sent back to the supplier leave the books at what they carried.

    Dr AP / Cr goods for the goods, AP on the account the document recognized its
    payable on. ``goods`` is keyed by the inventory account of the lot the goods leave,
    or by the role goods not held in stock were received to. Each kind of landed cost they
    carried goes back to the clearing account the bill parked it in (Dr clearing / Cr the lots' inventory
    accounts, ``landed_by_account``) in an entry of its own, the reverse of the receipt's
    capitalisation, so undoing the receipt returns only the landed cost still on the shelf."""
    currency = await company_currency(session, company_id)
    settings = await current_settings(session, company_id)
    rounded = {key: round_money(amount or 0, currency) for key, amount in goods.items()}
    goods_d = sum(rounded.values(), _Dec(0))
    if goods_d > 0:
        ap = await party_origin(session, company_id, doc_id, R.PAYABLE, settings)
        roles = [k for k, amt in rounded.items() if amt and isinstance(k, AccountRole)]
        acc = await resolve_many(session, company_id, [*roles, *([] if ap else [R.PAYABLE])])
        ap_line = (_origin_line(settings, ap, R.PAYABLE, debit=to_stored_float(goods_d)) if ap
                   else _line(acc[R.PAYABLE], R.PAYABLE, debit=to_stored_float(goods_d)))
        goods_lines = [_line(acc[k], k, credit=to_stored_float(amt)) if isinstance(k, AccountRole)
                       else _lot_line(settings, k, credit=to_stored_float(amt))
                       for k, amt in rounded.items() if amt]
        await _emit_auto_posted_je(
            session,
            company_id=company_id,
            user_id=user_id,
            je_id=f"je:auto:{doc_id}:rtn:{return_key}",
            idem_create=je_idempotency_key(doc_id, f"items.returned:{return_key}", "c"),
            idem_posted=je_idempotency_key(doc_id, f"items.returned:{return_key}", "p"),
            memo=f"Auto JE for {doc_id} goods returned to supplier",
            ts=return_date,
            entries=[ap_line, *goods_lines],
            metadata_={"trigger": "doc.items_returned", "doc_id": doc_id},
        )
    landed = {kind: round_money(amt or 0, currency) for kind, amt in landed_by_kind.items()}
    landed_total = sum(landed.values(), _Dec(0))
    if landed_total > 0:
        entries = [*await _clearing_lines(session, company_id, doc_id, settings, landed, currency, debit=True),
                   *_inventory_lines(settings, landed_total, landed_by_account, currency, debit=False)]
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


async def _bill_entries(session, company_id, doc_id: str, doc: dict,
                        base_currency: str) -> tuple[list[dict], list[int | None], list[_Dec]] | None:
    """What a bill books, before any netting: its debit lines and then the AP line, the
    line index each debit comes from (None for tax and shipping), and each debit in base
    currency. None for a bill with nothing to book.

    A line's own account_code takes priority; otherwise the line posts to the role
    for what it brings in: inventory for stock, general expense for anything else."""
    currency = doc.get("currency", "USD")
    rate = require_doc_rate(doc, base_currency)
    total_d = round_money(doc.get("total", 0) or 0, currency)
    line_items = doc.get("line_items", [])
    # (account chosen on the line, the role to post to, or an account posted for a role;
    # amount in the document currency)
    lines: list[tuple[str | AccountRole | tuple[str, AccountRole], _Dec]] = []
    sources: list[int | None] = []  # the line index each debit comes from
    tax_total_d = _Dec(0)

    if line_items:
        for index, li in enumerate(line_items):
            line_total = round_money(
                to_decimal(li.get("line_total") or 0) or
                to_decimal(li.get("quantity", 0)) * to_decimal(li.get("unit_price", 0)),
                currency,
            )
            if line_total <= 0:
                continue
            # receive_as overrides SKU-based account selection for bills.
            receive_as = (li.get("receive_as") or "").strip().lower()
            landed_role = await landed_role_for_line(session, company_id, li)
            if li.get("account_code"):
                # A landed charge posted to a clearing account of its own clears from there.
                target = ((li["account_code"], landed_role) if landed_role and landed_role.value in _LANDED_ROLES
                          else li["account_code"])
            elif receive_as == "expense":
                target = R.GENERAL_EXPENSE
            elif receive_as == "asset":
                target = R.FIXED_ASSETS
            elif landed_role:
                # Landed-cost charge (freight/insurance/duty/import_vat): clearing or input tax.
                target = landed_role
            else:
                target = R.INVENTORY_PURCHASED if bill_line_kind(li) == "stock" else R.GENERAL_EXPENSE
            lines.append((target, line_total))
            sources.append(index)
        # Input VAT: debit the EFFECTIVE tax that create_doc rolled into `total` (line `taxes[].amount`
        # + doc_taxes), not a per-line `tax_rate` the structured-tax create path never sets.
        tax_total_d = round_money(to_decimal(doc.get("tax", 0) or 0), currency)

    # Doc-level shipping on a bill is inbound freight: debit the freight clearing account.
    shipping_d = round_money(doc.get("shipping", 0) or 0, currency)
    if total_d <= 0:
        return None
    # A bill total below its lines, tax and shipping is a discount on those lines: each line's
    # cost is reduced by its share, so the debits sum to what the bill says is owed. Any other
    # gap between the parts and the total is refused rather than posted unbalanced.
    goods_d = sum((a for _, a in lines), _Dec(0))
    discount_d = goods_d + tax_total_d + shipping_d - total_d
    if 0 < discount_d <= goods_d:
        shares = allocate_pro_rata(discount_d, [a for _, a in lines], currency)
        lines = [(target, a - share) for (target, a), share in zip(lines, shares)]
    if tax_total_d > 0:
        lines.append((R.TAX_INPUT, tax_total_d))
    if shipping_d > 0:
        lines.append((R.LANDED_FREIGHT, shipping_d))
    if not lines:
        lines.append((R.GENERAL_EXPENSE, total_d))
    sources += [None] * (len(lines) - len(sources))
    if sum((a for _, a in lines), _Dec(0)) != total_d:
        raise UnbalancedJournalEntry(
            f"Bill {doc_id}: its lines, tax and shipping do not add up to its total of {total_d} {currency}"
        )

    settings = await current_settings(session, company_id)
    # A charge its receipts already drew from clearing (goods received before the bill was
    # finalized) is booked on the account they drew it from.
    drawn = await landed_clearing(session, company_id, doc_id, settings)

    def drawn_home(target):
        held = {c: v for c, v in drawn.get(str(target), {}).items() if v > 0}
        if not isinstance(target, AccountRole) or not held:
            return target
        return min(held, key=lambda c: (-held[c], c)), target

    lines = [(drawn_home(target), a) for target, a in lines]
    acc = await resolve_many(session, company_id, [*(t for t, _ in lines if isinstance(t, AccountRole)), R.PAYABLE])

    def debit_line(target, amount: float) -> dict:
        if isinstance(target, AccountRole):
            return _line(acc[target], target, debit=amount)
        if isinstance(target, tuple):
            return _line(*target, debit=amount)
        return {"account": target, "debit": amount, "credit": 0.0}

    # AP is the bill total in base; the debits are converted line by line, and the unit
    # of rounding that conversion can leave goes to the largest debit so the entry balances.
    base_total = to_base(to_stored_float(total_d), rate, base_currency)
    debits = [to_decimal(to_base(to_stored_float(a), rate, base_currency)) for _, a in lines]
    largest = max(range(len(debits)), key=lambda i: debits[i])
    debits[largest] += to_decimal(base_total) - sum(debits, _Dec(0))
    entries = [debit_line(target, to_stored_float(d)) for (target, _), d in zip(lines, debits)]
    entries.append(_line(acc[R.PAYABLE], R.PAYABLE, credit=base_total))
    return entries, sources, debits


async def create_for_bill_conversion(
    session,
    *,
    company_id,
    user_id,
    doc_id: str,
    doc: dict,
    base_currency: str = "USD",
    revert_count: int = 0,
    correction: bool = False,
) -> dict[int, tuple[str, _Dec]]:
    """Create JE when a bill is finalized (direct bill) or when a PO is converted to a bill.

    Debit per-line expense/inventory accounts, credit AP (_bill_entries), less what the
    document's purchase order receipts already booked and less what the opening balances
    carry for a document imported into them (imported_carriage). ``correction`` marks the
    posting as the correction of an earlier import's (correct_earlier_import), apart from
    the cycle's own.
    Returns what each line debits, in base currency, by line index: {index: (account,
    amount)}, before any netting against receipts.
    """
    built = await _bill_entries(session, company_id, doc_id, doc, base_currency)
    if built is None:
        return {}
    entries, sources, debits = built
    by_line = {index: (e["account"], d) for index, e, d in zip(sources, entries, debits) if index is not None}
    settings = await current_settings(session, company_id)
    # What the document's purchase order receipts already booked is not booked again, so
    # receiving before or after finalizing ends in the same books.
    # A line first takes up what the receipts booked for the same purpose, on the accounts
    # they booked it to (goods added to a lot on the lot's own account, AP recognized before
    # a remap); only what no receipt booked lands on the line's own account.
    # The part of an imported document the opening balances hold is booked there already.
    booked = await _doc_receipt_booked(session, company_id, doc_id, settings)
    for e in await imported_carriage(session, company_id, doc_id):
        key = _net_key(settings, e)
        booked[key] = booked.get(key, _Dec(0)) + to_decimal(e["debit"]) - to_decimal(e["credit"])
    if booked:
        unfilled = dict(booked)
        net: dict[tuple, _Dec] = {}
        for e in entries:
            key = _net_key(settings, e)
            left = to_decimal(e["debit"]) - to_decimal(e["credit"])
            slots = sorted((k for k in unfilled if k[1] and _net_group(k[1]) == _net_group(key[1])),
                           key=lambda k: (k != key, -abs(unfilled[k]), k[0]))
            for slot in slots:
                if left and unfilled[slot] and (unfilled[slot] > 0) == (left > 0):
                    take = min(abs(left), abs(unfilled[slot])) * (1 if left > 0 else -1)
                    net[slot] = net.get(slot, _Dec(0)) + take
                    unfilled[slot] -= take
                    left -= take
            if left:
                net[key] = net.get(key, _Dec(0)) + left
        for key, amount in booked.items():
            net[key] = net.get(key, _Dec(0)) - amount
        entries = [{"account": acct, "account_roles": list(roles),
                    "debit": to_stored_float(max(v, _Dec(0))), "credit": to_stored_float(max(-v, _Dec(0)))}
                   for (acct, roles), v in net.items() if v]
        if not entries:
            return by_line

    tag = f":{_CORRECTION}" if correction else ""
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:bill",
        idem_create=je_idempotency_key(doc_id, f"po.converted_to_bill:{revert_count}{tag}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"po.converted_to_bill:{revert_count}{tag}", "p"),
        memo=f"Auto JE for {doc_id} converted to bill",
        ts=await entry_day(session, company_id, doc.get("issue_date") or doc.get("finalized_at")),
        entries=entries,
        metadata_={"trigger": "doc.converted_to_bill", "doc_id": doc_id, **({IMPORTED_CUTOVER: True} if correction else {})},
    )
    return by_line


@dataclass
class ImportedDocument:
    """A purchase order or bill imported already issued, and what its history did with
    the value the opening balances hold for it.

    in_opening: imported now, into books whose opening balances hold it; nothing posted.
    origins: ledger ids of the entries an earlier release posted for the import itself
    (the order's receipt or the bill's own entry, booking the document a second time on
    top of the opening balances), and of every unvoid restore of one.
    """
    doc_id: str
    snapshot: dict
    in_opening: bool
    origins: set[int]
    posted_origins: list[str]
    reversals: list[LedgerEntry]
    posted_reversals: list[str]
    je_events: list[LedgerEntry]

    @property
    def carried(self) -> bool:
        """The opening balances hold the document now: it stands as imported, neither
        reversed with it (a void or revert) nor still carried by an earlier release's entry."""
        return (self.in_opening or bool(self.origins)) and not self.posted_origins and not self.posted_reversals


_IMPORTED_REVERSAL = "imported-rev"
_CORRECTION = "imported-cutover"
_CORRECTION_TRIGGER = "imported.cutover_repair"


def _is_origin(event: LedgerEntry, doc_id: str, kind: str, origins: set[int], latest: dict[str, int]) -> bool:
    """Whether a JE creation event is the entry an earlier release posted for the import."""
    meta = event.metadata_ or {}
    restores = meta.get("restores")
    if restores:
        return latest.get(restores) in origins
    if kind == "purchase_order":
        return (event.entity_id == f"je:auto:{doc_id}:rcv"
                and event.idempotency_key == je_idempotency_key(doc_id, "po.received", "c"))
    return (event.entity_id == f"je:auto:{doc_id}:bill"
            and event.idempotency_key == je_idempotency_key(doc_id, "po.converted_to_bill:0", "c")
            and meta.get("trigger") == "doc.converted_to_bill")


async def imported_document(session, company_id, doc_id: str) -> ImportedDocument | None:
    """The document's import, when it is a purchase order or bill imported issued into
    books whose opening balances hold it (now, or under an earlier release that also
    booked it on import); None for any other document."""
    from celerp.models.ledger import LedgerEntry

    created = (await session.execute(_select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == doc_id,
        LedgerEntry.event_type == "doc.created",
    ).order_by(LedgerEntry.id).limit(1))).scalars().first()
    meta = (created.metadata_ or {}) if created is not None else {}
    if not meta.get(IMPORTED_SNAPSHOT):
        return None
    kind = imported_issue_kind(created.data or {})
    if kind not in ("purchase_order", "bill"):
        return None
    in_opening = bool(meta.get(IMPORTED_OPENING))
    prefix = f"je:auto:{doc_id}:"
    events = list((await session.execute(_select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "journal_entry",
        LedgerEntry.entity_id.startswith(prefix, autoescape=True),
    ).order_by(LedgerEntry.id))).scalars())
    origins: set[int] = set()
    latest: dict[str, int] = {}  # je id -> ledger id of its latest creation
    reversals: list[LedgerEntry] = []
    for event in events:
        if event.event_type != "acc.journal_entry.created":
            continue
        if not in_opening and _is_origin(event, doc_id, kind, origins, latest):
            origins.add(event.id)
        if event.entity_id.startswith(f"{prefix}{_IMPORTED_REVERSAL}:"):
            reversals.append(event)
        latest[event.entity_id] = event.id
    if not in_opening and not origins:
        return None
    ids = {*(je for je, seq in latest.items() if seq in origins), *(e.entity_id for e in reversals)}
    posted = set((await session.execute(_select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_id.in_(sorted(ids)),
        Projection.state["status"].as_string() == "posted",
    ))).scalars()) if ids else set()
    return ImportedDocument(
        doc_id=doc_id, snapshot=created.data or {}, in_opening=in_opening, origins=origins,
        posted_origins=sorted(je for je, seq in latest.items() if seq in origins and je in posted),
        reversals=reversals, posted_reversals=[e.entity_id for e in reversals if e.entity_id in posted],
        je_events=events,
    )


async def _carriage_entries(session, company_id, imported: ImportedDocument) -> list[dict]:
    """What the opening balances hold for an imported document, as the entry its bill
    would book: a bill's whole entry; for a purchase order, each line's share received
    when it was imported (tax and shipping take the goods' overall share)."""
    snapshot = imported.snapshot
    base_currency = await company_currency(session, company_id)
    built = await _bill_entries(session, company_id, imported.doc_id, snapshot, base_currency)
    if built is None:
        return []
    entries, sources, debits = built
    if snapshot.get("doc_type") == "bill":
        return entries
    from celerp.services.document_lines import doc_line_index

    lines = snapshot.get("line_items") or []
    received: dict[int, _Dec] = {}
    for x in snapshot.get("received_items") or []:
        index = doc_line_index(lines, int(x.get("po_line_index", -1)), x.get("item_id"), x.get("sku"))
        if index is not None:
            received[index] = received.get(index, _Dec(0)) + to_decimal(x.get("quantity_received"))
    share: dict[int, _Dec] = {}
    for index, li in enumerate(lines):
        ordered = to_decimal(li.get("quantity"))
        share[index] = min(received.get(index, _Dec(0)) / ordered, _Dec(1)) if ordered > 0 else _Dec(0)
    goods = sum((d for d, s in zip(debits, sources) if s is not None), _Dec(0))
    overall = (sum((d * share[s] for d, s in zip(debits, sources) if s is not None), _Dec(0)) / goods
               if goods else _Dec(0))
    carried = []
    for e, s, d in zip(entries, sources, debits):
        amount = round_money(d * (share[s] if s is not None else overall), base_currency)
        if amount:
            carried.append({**e, "debit": to_stored_float(amount)})
    if not carried:
        return []
    total = sum((to_decimal(e["debit"]) for e in carried), _Dec(0))
    return [*carried, {**entries[-1], "credit": to_stored_float(total)}]


async def imported_carriage(session, company_id, doc_id: str) -> list[dict]:
    """The entry the opening balances hold for an imported document while they hold it
    (ImportedDocument.carried), or [] when they do not."""
    imported = await imported_document(session, company_id, doc_id)
    if imported is None or not imported.carried:
        return []
    return await _carriage_entries(session, company_id, imported)


async def reverse_imported_carriage(session, *, company_id, user_id, doc_id: str, trigger: str,
                                    carriage: list[dict], metadata_: dict | None = None) -> None:
    """Take out of the books what the opening balances hold for an imported document,
    when it is voided or reverted to draft: the imported value stops being owed and the
    goods billed with it stop being carried. ``trigger`` is the operation, so an unvoid
    knows which reversal is its void's to take back."""
    if not carriage:
        return
    prefix = f"je:auto:{doc_id}:{_IMPORTED_REVERSAL}:"
    n = 1 + len((await session.execute(_select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix, autoescape=True),
    ))).scalars().all())
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"{prefix}{n}",
        idem_create=je_idempotency_key(doc_id, f"{_IMPORTED_REVERSAL}:{n}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"{_IMPORTED_REVERSAL}:{n}", "p"),
        memo=f"Reversed: {doc_id} as held in the opening balances",
        ts=await entry_day(session, company_id),
        entries=[{**e, "debit": e["credit"], "credit": e["debit"]} for e in carriage],
        metadata_={"trigger": trigger, "doc_id": doc_id, **(metadata_ or {})},
    )


async def take_back_imported_reversals(session, *, company_id, user_id, doc_id: str) -> None:
    """On an unvoid, void the reversals the document's voids posted, so the opening
    balances hold it again (a revert's reversal stands: the document was booked anew)."""
    imported = await imported_document(session, company_id, doc_id)
    if imported is None:
        return
    for event in imported.reversals:
        if (event.metadata_ or {}).get("trigger") != "doc.voided" or event.entity_id not in imported.posted_reversals:
            continue
        await _void_je_if_posted(
            session, company_id=company_id, user_id=user_id, doc_id=doc_id, je_id=event.entity_id,
            idem_key=je_idempotency_key(doc_id, f"unvoid:{event.entity_id.rsplit(':', 1)[-1]}:{_IMPORTED_REVERSAL}", "void"),
            reason=f"Reversed: {doc_id} unvoided", trigger="doc.unvoided",
        )


async def correct_earlier_import(session, *, company_id, doc_id: str, doc: dict) -> bool:
    """Bring a document an earlier release imported to where the books would be had it
    been imported now (imported_document). Returns whether anything was posted.

    That release booked the import itself on top of the opening balances (its origins).
    Each origin still posted is voided, on its own date. A purchase order since converted
    to a bill had that bill's entry netted against the origin, so a live bill's entry is
    voided and booked again against what the opening balances hold. A document voided or
    ever reverted to draft has the opening balances' value reversed, as a void or revert
    does now, unless that reversal is already posted. Safe to run again: a second run
    finds nothing posted to void and the reversal in place."""
    imported = await imported_document(session, company_id, doc_id)
    if imported is None or imported.in_opening:
        return False
    marker = {IMPORTED_CUTOVER: True}
    latest = {e.entity_id: e.id for e in imported.je_events if e.event_type == "acc.journal_entry.created"}
    changed = False

    async def void(je_id: str) -> None:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
        if row is None or (row.state or {}).get("status") != "posted":
            return
        await emit_event(
            session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
            event_type="acc.journal_entry.voided",
            data=je_void_data(f"Reversed: {doc_id} is held in the opening balances", row.state),
            actor_id=None, location_id=None, source="auto_je",
            idempotency_key=f"{_CORRECTION}:void:{je_id}:{latest.get(je_id)}",
            metadata_={"trigger": _CORRECTION_TRIGGER, "doc_id": doc_id, **marker},
        )

    for je_id in imported.posted_origins:
        await void(je_id)
        changed = True
    status = doc.get("status")
    if changed and doc.get("doc_type") == "bill" and imported.snapshot.get("doc_type") == "purchase_order" \
            and status not in ("draft", "void"):
        for suffix, row in (await _doc_recognition_jes(session, company_id, doc_id)).items():
            if _recognition_root(suffix) == "bill":
                await void(row.entity_id)
        await create_for_bill_conversion(
            session, company_id=company_id, user_id=None, doc_id=doc_id, doc=doc,
            base_currency=await company_currency(session, company_id),
            revert_count=int(doc.get("revert_count", 0) or 0), correction=True,
        )

    from celerp.models.ledger import LedgerEntry

    reverted = (await session.execute(_select(LedgerEntry.id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == doc_id,
        LedgerEntry.event_type == "doc.reverted_to_draft",
    ).limit(1))).first() is not None
    if reverted or status == "void":
        imported = await imported_document(session, company_id, doc_id)
        if not imported.posted_reversals and not imported.posted_origins:
            carriage = await _carriage_entries(session, company_id, imported)
            await reverse_imported_carriage(
                session, company_id=company_id, user_id=None, doc_id=doc_id,
                trigger="doc.reverted_to_draft" if reverted else "doc.voided",
                carriage=carriage, metadata_=marker,
            )
            changed = changed or bool(carriage)
    return changed


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
# fin:{cycle} after reverts), bill conversion, the one-time COGS backfill, the
# fulfillment COGS adjustment (cogs-adj:{cycle_tag}), and the COGS an invoice
# booked at fulfillment before COGS moved into the finalize JE (fulfill,
# fulfill-{cycle}), which no flow posts or reverses any more. These are what a
# doc void or revert to draft reverses and an unvoid restores. Settlement and
# stock-movement JEs (payments, credit-note applications, receiving, landed
# cost, returns) are not recognition: they reverse through their own flows.
_RECOGNITION_FAMILIES = ("fin", "bill", "cogs-backfill", "cogs-adj")
_FULFILLMENT_COGS = re.compile(r"fulfill(?:-\d+)?")


def _recognition_root(suffix: str) -> str | None:
    """The recognition root of a JE id suffix, or None for non-recognition JEs.

    The root is the suffix with any unvoid-restore generations stripped, so a
    restore shares its original's root: fin, fin:2, fin:unvoid, fin:2:unvoid:1
    all root to their cycle id; cogs-adj:fulfill-0:l0:unvoid:1 roots to
    cogs-adj:fulfill-0:l0, fulfill-1:unvoid to fulfill-1. A payment (pay:0) or
    receipt (rcv:0) suffix returns None.
    """
    root = re.sub(r"(?::unvoid(?::\d+)?)+$", "", suffix)
    if _FULFILLMENT_COGS.fullmatch(root):
        return root
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
    that booked its receipts and its returns to the supplier reverse with it. What the
    opening balances hold for an imported document is reversed with it too
    (reverse_imported_carriage).

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
    await reverse_imported_carriage(
        session, company_id=company_id, user_id=user_id, doc_id=doc_id, trigger="doc.reverted_to_draft",
        carriage=await imported_carriage(session, company_id, doc_id))


async def void_for_doc_voided(session, *, company_id, user_id, doc_id: str) -> None:
    """Reverse every posted recognition-family auto-JE when a finalized doc is
    voided, stamping each void event with this void's batch number.

    Symmetric with create_for_doc_unvoided, which restores exactly the JEs the
    most recent batch voided. The candidates come from the doc's actual JE
    projections (finalize cycles, unvoid restores, COGS backfill, COGS
    adjustments), so nothing live is left behind and nothing settled (payments,
    fulfillment stock moves) is touched. The batch number in both the metadata
    and the idempotency key keeps every void/unvoid cycle's events distinct:
    a second cycle's voids can never dedup against the first's. What the opening
    balances hold for an imported document is reversed with it too
    (reverse_imported_carriage), and the unvoid takes that reversal back.
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
    await reverse_imported_carriage(
        session, company_id=company_id, user_id=user_id, doc_id=doc_id, trigger="doc.voided",
        carriage=await imported_carriage(session, company_id, doc_id))


def _cogs_lines(settings: dict, cogs_code: str, by_account: dict[str, float], currency: str) -> list[dict]:
    """Cost of goods sold against the inventory accounts the goods are valued in.

    ``by_account`` is a signed amount per inventory account: positive takes goods off
    that account into COGS, negative puts them back. Each amount is rounded once and
    the COGS line is their sum, so the entry balances once rounded."""
    amounts = {code: round_money(v, currency) for code, v in sorted(by_account.items())}
    amounts = {code: a for code, a in amounts.items() if a != 0}
    total = sum(amounts.values(), _Dec(0))
    lines = []
    if total != 0:
        lines.append(_line(cogs_code, R.COGS, debit=to_stored_float(max(total, _Dec(0))),
                           credit=to_stored_float(max(-total, _Dec(0)))))
    for code, a in amounts.items():
        lines.append(_lot_line(settings, code, debit=to_stored_float(max(-a, _Dec(0))),
                               credit=to_stored_float(max(a, _Dec(0)))))
    return lines


async def _cogs_entries(session, company_id, by_account: dict[str, float], *, expense: bool = True) -> list[dict]:
    """Cost of goods sold against the inventory accounts the goods are valued in:
    ``expense`` moves each account's amount out of stock into COGS, otherwise back
    from COGS into stock."""
    cogs_code = await resolve(session, company_id, R.COGS)
    settings = await current_settings(session, company_id)
    sign = 1 if expense else -1
    return _cogs_lines(settings, cogs_code, {code: sign * v for code, v in by_account.items()},
                       await company_currency(session, company_id))


async def lots_by_account(session, company_id, amounts: dict[str, float]) -> dict[str, float]:
    """{lot entity id: amount} summed onto the account each lot is costed against
    (sold_lot_key)."""
    out: dict[str, float] = {}
    for lot_id, amount in amounts.items():
        if not amount:
            continue
        row = await session.get(Projection, {"company_id": company_id, "entity_id": lot_id})
        code = await sold_lot_key(session, company_id, lot_id, (row.state or {}) if row is not None else {})
        out[code] = out.get(code, 0.0) + amount
    return out


async def stock_relief_lines(session, company_id, by_account: dict[str, float]) -> list[dict]:
    """The inventory credits that take goods off the books, each on the account the
    goods are valued in."""
    settings = await current_settings(session, company_id)
    return [_lot_line(settings, code, credit=float(amount))
            for code, amount in sorted(by_account.items()) if amount]


async def create_for_doc_cogs_backfill(session, *, company_id, user_id, doc_id: str, by_account: dict[str, float], ts: str | None) -> None:
    """Post the one-time COGS JE for a finalized invoice that predates
    COGS-at-finalize and never received its COGS at fulfillment.

    ts carries the doc's finalize-family JE date so the expense lands in the
    period that recognized the revenue; a dateless doc stays dateless. by_account
    is the cost per inventory account the goods are valued in (CogsResult).
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
        entries=await _cogs_entries(session, company_id, by_account),
        metadata_={"trigger": "doc.cogs_backfill", "doc_id": doc_id},
    )


@dataclass
class RecognizedCogs:
    """The per-line COGS a doc's live finalize-family JE recognized.

    cycle is the recognition root of that JE (fin, fin:2, ...), shared by its
    unvoid restores; allocations is the snapshot keyed by line index, less the goods
    other invoices shipped and took the cost of since (cost moves); je_id is that JE
    and seq the ledger id of its creation, the order invoices took their goods in."""

    cycle: str
    allocations: dict
    je_id: str = ""
    seq: int = 0


def _finalize_root(doc_id: str, je_id: str) -> str | None:
    """The finalize cycle root of a doc's JE id (fin, fin:2), or None for any other JE."""
    prefix = f"je:auto:{doc_id}:"
    if not je_id.startswith(prefix):
        return None
    root = _recognition_root(je_id[len(prefix):])
    return root if root is not None and (root == "fin" or root.startswith("fin:")) else None


def _finalize_doc(je_id: str, doc_ids: set[str]) -> str | None:
    """The doc of ``doc_ids`` whose finalize-family JE ``je_id`` is, or None."""
    body = je_id[len("je:auto:"):] if je_id.startswith("je:auto:") else ""
    at = body.find(":fin")
    while at > 0:
        if body[:at] in doc_ids and _finalize_root(body[:at], je_id) is not None:
            return body[:at]
        at = body.find(":fin", at + 1)
    return None


async def _created_metadata(session, company_id, je_ids) -> dict[str, tuple[int, dict]]:
    """je id -> (ledger id, metadata) of the latest creation event of each JE."""
    from celerp.models.ledger import LedgerEntry

    if not je_ids:
        return {}
    rows = (await session.execute(
        _select(LedgerEntry.id, LedgerEntry.entity_id, LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.event_type == "acc.journal_entry.created",
            LedgerEntry.entity_id.in_(sorted(je_ids)),
        ).order_by(LedgerEntry.id)
    )).all()
    return {je_id: (seq, metadata_ or {}) for seq, je_id, metadata_ in rows}


async def _recognitions(session, company_id, doc_ids) -> dict[str, RecognizedCogs]:
    """recognized_cogs for each of ``doc_ids``, read in a fixed number of queries."""
    docs = set(doc_ids)
    if not docs:
        return {}
    query = _select(Projection.entity_id, Projection.created_at, Projection.state["status"].as_string()).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry")
    if len(docs) == 1:
        query = query.where(Projection.entity_id.startswith(f"je:auto:{next(iter(docs))}:", autoescape=True))
    else:
        query = query.where(Projection.entity_id.like("je:auto:%:fin%"))
    live: dict[str, tuple[str, datetime | None]] = {}
    for je_id, created_at, status in (await session.execute(query)).all():
        doc_id = _finalize_doc(je_id, docs) if status == "posted" else None
        if doc_id is None:
            continue
        current = live.get(doc_id)
        if current is None or (created_at is not None and (current[1] is None or created_at > current[1])):
            live[doc_id] = (je_id, created_at)
    created = await _created_metadata(session, company_id, {je_id for je_id, _at in live.values()})
    moved = await _cost_moves(session, company_id, from_jes={je_id for je_id, _at in live.values()})
    out: dict[str, RecognizedCogs] = {}
    for doc_id, (je_id, _at) in live.items():
        seq, metadata_ = created.get(je_id, (0, {}))
        allocations = metadata_.get("cogs_allocations")
        if allocations:
            out[doc_id] = RecognizedCogs(
                cycle=_finalize_root(doc_id, je_id), je_id=je_id, seq=seq,
                allocations=_less_moved(allocations, [m for e in moved if e["from_je"] == je_id
                                                      for m in e["moves"]]))
    return out


async def recognized_cogs(session, company_id, doc_id: str) -> RecognizedCogs | None:
    """The COGS the doc's currently posted finalize-family JE recognized.

    Reads the allocation snapshot off the creation event of the currently posted
    finalize-family JE (fin, a re-finalize cycle, or an unvoid restore of one), less
    the goods other invoices shipped and took the cost of from it (cost moves).
    None when no posted finalize-family JE carries a snapshot - which is every doc
    finalized before snapshots existed, where there is no recognized basis to
    true up against and no adjustment may be posted.
    """
    return (await _recognitions(session, company_id, [doc_id])).get(doc_id)


def _less_moved(allocations: dict, moves: list[dict]) -> dict:
    """An allocation snapshot less the goods cost moves took from it: each moved
    quantity leaves its lot and becomes provisional (costed when the line ships), and
    the line's amount gives up the cost that moved."""
    if not moves:
        return allocations
    out = {idx: {**alloc, "lots": [dict(lot) for lot in alloc.get("lots") or []]}
           for idx, alloc in allocations.items()}
    for move in moves:
        alloc = out.get(str(move["line"]))
        if alloc is None:
            continue
        left = float(move["qty"])
        for lot in alloc["lots"]:
            if lot.get("lot_entity_id") == move["lot_id"] and left > 1e-9:
                take = min(float(lot.get("qty") or 0), left)
                lot["qty"] = float(lot.get("qty") or 0) - take
                left -= take
        alloc["lots"] = [lot for lot in alloc["lots"] if float(lot.get("qty") or 0) > 1e-9]
        alloc["provisional_qty"] = float(alloc.get("provisional_qty") or 0) + float(move["qty"]) - left
        alloc["amount"] = max(0.0, float(alloc.get("amount") or 0) - float(move["amount"]))
    return out


def lot_cost_of_sale(state: dict) -> float:
    """What a lot costs when it leaves on a sale: its cost_total, or its unit
    cost_price times its quantity when it carries no total."""
    cost_total = state.get("cost_total")
    if cost_total is not None:
        return float(cost_total)
    return float(state.get("cost_price") or 0) * float(state.get("quantity") or 0)


@dataclass(frozen=True)
class MergeReclassification:
    """Where a merged lot is valued and the carrying value that moves there.

    ``moves`` is the amount leaving each other inventory account, rounded once per
    account; empty when every source already sits in ``destination``."""

    destination: str | None
    moves: dict[str, _Dec]
    currency: str

    def disclosure(self) -> dict | None:
        if not self.moves:
            return None
        return {"destination": self.destination, "currency": self.currency,
                "moves": [{"account": code, "amount": to_stored_float(a)} for code, a in self.moves.items()]}


def merge_reclassification(survivor: dict, sources: list[dict], currency: str) -> MergeReclassification:
    """The inventory account a merge keeps and the value it moves into it.

    The merged lot keeps the surviving lot's own account, never the account new stock
    goes to today. Every other source's carrying value (what a sale of it would
    relieve, landed cost included) moves out of the account that lot is held in."""
    recorded = {s.get(LOT_ACCOUNT_FIELD) for s in sources}
    if len(recorded) == 1 and None not in recorded:
        return MergeReclassification(recorded.pop(), {}, currency)
    if not any(lot_cost_of_sale(state) for state in sources):
        # No source carries value, so nothing moves and no account needs proving. The
        # merged lot records the survivor's own account, unknown included, as any lot
        # carved from it would.
        return MergeReclassification(survivor.get(LOT_ACCOUNT_FIELD), {}, currency)
    destination = lot_account(survivor)
    by_account: dict[str, _Dec] = {}
    for state in sources:
        code = lot_account(state)
        if code != destination:
            by_account[code] = by_account.get(code, _Dec(0)) + to_decimal(lot_cost_of_sale(state))
    rounded = {code: round_money(v, currency) for code, v in sorted(by_account.items())}
    moves = {code: amount for code, amount in rounded.items() if amount != 0}
    return MergeReclassification(destination, moves, currency)


def merge_reclass_je_id(merged_id: str) -> str:
    return f"je:auto:{merged_id}:merge-reclass"


async def create_for_merge_reclassification(
    session, *, company_id, user_id, merged_id: str, merged_sku: str, source_ids: list[str],
    reclass: MergeReclassification, ts: str,
) -> None:
    """Post the merge's reclassification: one credit per account the value leaves, one
    debit to the surviving account. Nothing when no value moves."""
    if not reclass.moves:
        return
    settings = await current_settings(session, company_id)
    total = sum(reclass.moves.values(), _Dec(0))
    lines = []
    if total != 0:
        lines.append(_lot_line(settings, reclass.destination, debit=to_stored_float(max(total, _Dec(0))),
                               credit=to_stored_float(max(-total, _Dec(0)))))
    for code, a in reclass.moves.items():
        lines.append(_lot_line(settings, code, debit=to_stored_float(max(-a, _Dec(0))), credit=to_stored_float(max(a, _Dec(0)))))
    await _emit_auto_posted_je(
        session, company_id=company_id, user_id=user_id, je_id=merge_reclass_je_id(merged_id),
        idem_create=je_idempotency_key(merged_id, "item.merged.reclass", "c"),
        idem_posted=je_idempotency_key(merged_id, "item.merged.reclass", "p"),
        memo=f"Inventory reclassified on merge into {merged_sku}",
        entries=lines,
        metadata_={"trigger": "item.merged", "item_id": merged_id, "source_ids": source_ids},
        ts=ts,
    )


async def void_for_merge_reclassification(session, *, company_id, user_id, merged_id: str) -> bool:
    """Reverse a merge's reclassification exactly, on its own date."""
    return await _void_je_if_posted(
        session, company_id=company_id, user_id=user_id, doc_id=merged_id, je_id=merge_reclass_je_id(merged_id),
        idem_key=je_idempotency_key(merged_id, "item.merged.reclass", "void"),
        reason="Merge undone", trigger="item.merge_undone",
    )


def recorded_line_index(event) -> int | None:
    """The doc line index an item.fulfilled event recorded, if it recorded one."""
    idx = (event.metadata_ or {}).get("line_index")
    return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


def line_of_lot(line_items: list[dict], lot_id: str, lot_state: dict, recorded: int | None) -> int | None:
    """The index of the doc line a lot belongs to: the line naming the lot, else the
    line its latest fulfillment for the doc recorded (``recorded``), else the only line
    of its SKU."""
    for idx, line in enumerate(line_items):
        if (line.get("entity_id") or line.get("item_id")) == lot_id:
            return idx
    if recorded is not None:
        return recorded
    sku = str(lot_state.get("sku") or "").strip()
    matches = [idx for idx, line in enumerate(line_items) if str(line.get("sku") or "").strip() == sku]
    return matches[0] if len(matches) == 1 else None


async def doc_line_of_lot(session, company_id, doc_id: str, doc_state: dict, lot_id: str, lot_state: dict) -> int | None:
    """line_of_lot for one lot, reading its latest fulfillment for this doc from the ledger."""
    from celerp.models.ledger import LedgerEntry

    rows = (await session.execute(
        _select(LedgerEntry).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == lot_id,
            LedgerEntry.event_type == "item.fulfilled",
        ).order_by(LedgerEntry.id.desc())
    )).scalars().all()
    latest = next((e for e in rows if (e.data or {}).get("source_doc_id") == doc_id), None)
    recorded = recorded_line_index(latest) if latest is not None else None
    return line_of_lot(doc_state.get("line_items", []), lot_id, lot_state, recorded)


async def allocations_naming_lot(session, company_id, lot_id: str) -> dict[tuple[str, str, int], float]:
    """{(doc_id, finalize cycle, line index): quantity} for every current finalize
    allocation that prices a line at this lot, less what cost moves took from it.

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
    moved = await _cost_moves(session, company_id, from_jes={je_id for je_id, _m in rows})
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
        allocations = _less_moved(allocations, [m for e in moved if e["from_je"] == je_id for m in e["moves"]])
        for idx, alloc in allocations.items():
            qty = sum(float(lot.get("qty") or 0) for lot in alloc.get("lots", [])
                      if lot.get("lot_entity_id") == lot_id)
            if qty:
                found[(doc_id, cycle, int(idx))] = qty
    return found


async def _repricings(session, company_id) -> dict[tuple[str, str], dict[int, float]]:
    """(doc id, finalize cycle) -> line index -> total cost correction recorded against
    that cycle."""
    from sqlalchemy import Text, cast

    from celerp.models.ledger import LedgerEntry

    rows = (await session.execute(
        _select(LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "item",
            cast(LedgerEntry.metadata_, Text).like("%cogs_repriced%"),
        )
    )).scalars().all()
    out: dict[tuple[str, str], dict[int, float]] = {}
    for metadata_ in rows:
        for rec in (metadata_ or {}).get("cogs_repriced") or []:
            by_line = out.setdefault((rec.get("doc_id"), rec.get("cycle")), {})
            by_line[int(rec["line"])] = by_line.get(int(rec["line"]), 0.0) + float(rec["amount"])
    return out


@dataclass
class _Shipments:
    """What invoices shipped, read once for many: per doc, the lots it ships (latest
    fulfillment event for the doc not reversed) and the lots it took back into stock and
    no longer holds, each with the quantity that came back (a lot reversed and then
    reserved to the doc again is still held for it, so it is in neither), and per
    (doc, lot) the line index its latest fulfillment for the doc recorded."""

    out: dict[str, list[Projection]] = field(default_factory=dict)
    back: dict[str, list[tuple[Projection, float]]] = field(default_factory=dict)
    recorded: dict[tuple[str, str], int | None] = field(default_factory=dict)


async def _load_rows(session, company_id, entity_ids) -> dict[str, Projection]:
    """The projections of ``entity_ids``, in one query."""
    if not entity_ids:
        return {}
    rows = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id, Projection.entity_id.in_(sorted(entity_ids))))).scalars().all()
    return {r.entity_id: r for r in rows}


async def _shipments(session, company_id, doc_ids) -> _Shipments:
    from celerp.models.ledger import LedgerEntry

    result = _Shipments()
    if not doc_ids:
        return result
    rows = (await session.execute(
        _select(LedgerEntry.entity_id, LedgerEntry.event_type, LedgerEntry.data, LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "item",
            LedgerEntry.event_type.in_(("item.fulfilled", "item.fulfillment_reversed")),
            LedgerEntry.data["source_doc_id"].as_string().in_(sorted(doc_ids)),
        ).order_by(LedgerEntry.id)
    )).all()
    last: dict[tuple[str, str], tuple[str, dict]] = {}
    for entity_id, event_type, data, metadata_ in rows:
        key = (str((data or {}).get("source_doc_id")), entity_id)
        last[key] = (event_type, data or {})
        if event_type == "item.fulfilled":
            result.recorded[key] = recorded_line_index(SimpleNamespace(metadata_=metadata_))
    lots = await _load_rows(session, company_id, {lot_id for _doc, lot_id in last})
    for (doc_id, lot_id), (event_type, data) in sorted(last.items()):
        row = lots.get(lot_id)
        if row is None:
            continue
        if event_type == "item.fulfilled":
            result.out.setdefault(doc_id, []).append(row)
        elif not ((row.state or {}).get("status") == "reserved" and (row.state or {}).get("status_doc_id") == doc_id):
            result.back.setdefault(doc_id, []).append((row, float(data.get("quantity_restored") or 0)))
    return result


@dataclass
class _Books:
    """What reading many invoices' recognized cost needs, read once (_read_books)."""

    recognized: dict[str, RecognizedCogs]
    shipments: _Shipments
    repriced: dict[tuple[str, str], dict[int, float]]
    payable_codes: frozenset


async def _read_books(session, company_id, doc_ids) -> _Books:
    """The recognitions, shipments and cost corrections of ``doc_ids``, with every lot
    they name loaded into the session, in a fixed number of queries."""
    recognized = await _recognitions(session, company_id, doc_ids)
    shipments = await _shipments(session, company_id, set(recognized))
    await _load_rows(session, company_id, {lot["lot_entity_id"] for r in recognized.values()
                                           for alloc in r.allocations.values() for lot in alloc.get("lots") or []})
    return _Books(recognized=recognized, shipments=shipments, repriced=await _repricings(session, company_id),
                  payable_codes=frozenset(scope_codes(await current_settings(session, company_id),
                                                      R.CONSIGNOR_PAYABLE)))


async def _recognized_by_account(
    session, company_id, doc_id: str, doc_state: dict, books: _Books,
) -> tuple[dict[str, float], dict[int, dict[str, float]], dict[int, float]]:
    """What an invoice recognizes today, per inventory account, in two parts: the actual
    cost of the lots it shipped, and, per line, its allocation's share (with every cost
    correction since) for the goods it has not shipped, with the quantity not shipped.
    Raises ValueError when a shipped or returned lot cannot be matched to one of the
    invoice's lines."""
    recognized = books.recognized[doc_id]
    shipped: dict[str, float] = {}
    held: dict[int, dict[str, float]] = {}
    unshipped_qty: dict[int, float] = {}

    def _add(into: dict[str, float], by_account: dict[str, float]) -> None:
        for code, amount in by_account.items():
            into[code] = into.get(code, 0.0) + amount

    def _line(lot: Projection) -> int | None:
        return line_of_lot(doc_state.get("line_items", []), lot.entity_id, lot.state or {},
                           books.shipments.recorded.get((doc_id, lot.entity_id)))

    shipped_qty: dict[int, float] = {}
    back_qty: dict[int, float] = {}
    for lot in books.shipments.out.get(doc_id, []):
        idx = _line(lot)
        if idx is None:
            raise ValueError("cannot safely identify the invoice line of every shipped lot")
        cost = lot_cost_of_sale(lot.state or {})
        if cost:
            _add(shipped, {await sold_lot_key(session, company_id, lot.entity_id, lot.state or {}): cost})
        shipped_qty[idx] = shipped_qty.get(idx, 0.0) + float((lot.state or {}).get("quantity") or 0)
    for lot, qty in books.shipments.back.get(doc_id, []):
        idx = _line(lot)
        if idx is None:
            raise ValueError("cannot safely identify the invoice line of every lot taken back")
        back_qty[idx] = back_qty.get(idx, 0.0) + qty
    repriced = books.repriced.get((doc_id, recognized.cycle), {})
    for idx, alloc in recognized.allocations.items():
        amount = float(alloc.get("amount") or 0)
        allocated = sum(float(lot.get("qty") or 0) for lot in alloc.get("lots", [])) + float(
            alloc.get("provisional_qty") or 0)
        if int(idx) not in shipped_qty and int(idx) not in back_qty:
            _add(held.setdefault(int(idx), {}), await _allocation_by_account(
                session, company_id, alloc, amount + repriced.get(int(idx), 0.0), books.payable_codes))
            unshipped_qty[int(idx)] = allocated
            continue
        if int(idx) not in shipped_qty:
            amount += repriced.get(int(idx), 0.0)
        unshipped = allocated - shipped_qty.get(int(idx), 0.0) - back_qty.get(int(idx), 0.0)
        if allocated > 0 and unshipped > 1e-9:
            _add(held.setdefault(int(idx), {}), await _allocation_by_account(
                session, company_id, alloc, amount * unshipped / allocated, books.payable_codes))
            unshipped_qty[int(idx)] = unshipped
    return shipped, held, unshipped_qty


@dataclass(frozen=True)
class UnshippedClaim:
    """Goods on hand a finalized invoice costed and has not shipped: ``qty`` units of
    lot ``lot_id`` on line ``line``, ``amount`` of the invoice's recognized cost on
    ``key``, and the value the lot holds now (``on_hand``)."""

    doc_id: str
    line: int
    lot_id: str
    qty: float
    key: str
    amount: float
    on_hand: float


def _on_hand_value(row: Projection) -> _Dec | None:
    """The value goods a lot holds in stock carry, or None when it holds none: owned
    stock at its value on the books (held_value), consigned goods at their recorded
    cost, owed to the consignor."""
    value = held_value(row)
    if value is None and is_consigned(row.state or {}) and in_stock(row.state):
        value = recorded_value(row.state or {})
    return value


def _line_skus(state: dict) -> set[str] | None:
    """The SKUs an invoice's lines name, or None when a stock line names none."""
    skus: set[str] = set()
    for li in state.get("line_items") or []:
        sku = str(li.get("sku") or "").strip()
        if not sku:
            if li.get("item_id") or li.get("entity_id"):
                return None
            continue
        skus.add(sku)
    return skus


async def unshipped_claims(session, company_id, *, exclude: str | None = None,
                           skus: set[str] | None = None) -> list[UnshippedClaim]:
    """Every claim finalized invoices hold on goods still on hand (UnshippedClaim), in
    the order the invoices were finalized, leaving out the invoice ``exclude``, and, when
    ``skus`` is given, invoices none of whose lines sell those SKUs.

    Only invoices that are final and not shipped in full count: not drafts, not voided.
    A line's unshipped quantity is taken from its allocated lots in allocation order,
    each lot only while it is still in stock and only up to what was allocated from it;
    the line's recognized cost for its unshipped goods is shared over those lots by
    their allocated cost. A lot no longer in stock claims nothing: its goods left stock
    some other way."""
    docs = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    open_docs: dict[str, dict] = {}
    for doc in docs:
        state = doc.state or {}
        if (doc.entity_id == exclude or state.get("doc_type") != "invoice"
                or state.get("status") in ("draft", "void") or state.get("fulfillment_status") == "fulfilled"):
            continue
        named = _line_skus(state) if skus is not None else None
        if named is not None and not named & skus:
            continue
        open_docs[doc.entity_id] = state
    books = await _read_books(session, company_id, open_docs)
    claims: list[UnshippedClaim] = []
    for doc_id, recognized in sorted(books.recognized.items(), key=lambda d: (d[1].seq, d[0])):
        _shipped, held, unshipped = await _recognized_by_account(session, company_id, doc_id, open_docs[doc_id], books)
        for idx, by_key in sorted(held.items()):
            left = unshipped.get(idx, 0.0)
            taken: list[tuple[str, float, str, float, float]] = []  # lot, qty, key, weight, on hand
            for lot in recognized.allocations[str(idx)].get("lots") or []:
                row = await session.get(Projection, {"company_id": company_id, "entity_id": lot["lot_entity_id"]})
                value = _on_hand_value(row) if row is not None else None
                qty = min(float(lot.get("qty") or 0), left)
                if value is None or qty <= 1e-9:
                    continue
                left -= qty
                key = await _allocated_lot_key(session, company_id, lot, books.payable_codes)
                taken.append((lot["lot_entity_id"], qty, key, qty * float(lot.get("unit_cost") or 0), float(value)))
            # A lot set aside at no cost still holds its claim: the goods move with it
            # when another invoice ships them, at a cost of nothing.
            for key in dict.fromkeys(t[2] for t in taken):
                mine = [t for t in taken if t[2] == key]
                shares = _shares({t[0]: t[3] for t in mine}, by_key.get(key, 0.0))
                claims += [UnshippedClaim(doc_id=doc_id, line=idx, lot_id=lot_id, qty=qty, key=key,
                                          amount=shares.get(lot_id, 0.0), on_hand=on_hand)
                           for lot_id, qty, _key, _weight, on_hand in mine]
    return claims


async def recognized_unshipped(session, company_id) -> dict[str, float]:
    """Per inventory account, the cost finalized invoices have recognized for goods they
    have not shipped: relieved from the books at finalize while the lots are still on hand.
    The books check and the stock oracles count it as stock the books already gave up.

    A lot counts at most once, however many invoices costed it: each claim, in the order
    the invoices were finalized, counts only up to what the lot still holds after the
    claims before it, so goods two invoices costed, or that left stock another way,
    still show as a gap."""
    left: dict[str, float] = {}
    total: dict[str, float] = {}
    for claim in await unshipped_claims(session, company_id):
        room = left.setdefault(claim.lot_id, claim.on_hand)
        take = max(0.0, min(claim.amount, room))
        left[claim.lot_id] = room - take
        code = split_party_key(claim.key)[0]
        total[code] = total.get(code, 0.0) + take
    return total


_COST_MOVE_IDS = "je:auto:%:cost-move:%"


async def _cost_moves(session, company_id, *, from_jes: set[str] | None = None,
                      doc_id: str | None = None) -> list[dict]:
    """The metadata of every cost move (create_for_cost_moves) taken from one of the
    finalize JEs ``from_jes``, or into or out of invoice ``doc_id``."""
    from celerp.models.ledger import LedgerEntry

    if from_jes is not None and not from_jes:
        return []
    query = _select(LedgerEntry.metadata_).where(
        LedgerEntry.company_id == company_id,
        LedgerEntry.event_type == "acc.journal_entry.created",
        LedgerEntry.entity_id.like(_COST_MOVE_IDS),
    )
    if from_jes is not None:
        query = query.where(LedgerEntry.metadata_["from_je"].as_string().in_(sorted(from_jes)))
    if doc_id is not None:
        query = query.where(or_(LedgerEntry.metadata_["doc_id"].as_string() == doc_id,
                                LedgerEntry.metadata_["from_doc_id"].as_string() == doc_id))
    return [m for m in (await session.execute(query)).scalars().all() if m]


async def moved_costs(session, company_id, *, doc_id: str, cycle_tag: str,
                      taken: dict[str, float]) -> dict[str, list[UnshippedClaim]]:
    """The claims other open invoices hold on goods invoice ``doc_id`` is about to ship,
    that shipping them leaves no goods for: ``taken`` is the quantity it ships from each
    lot, read while the lots still hold it (after any split). A lot keeps what remains in
    stock for the invoices that took it first; the claims the shipment leaves uncovered
    are the latest ones. Returns {source doc id: [claim, with the quantity moving]}."""
    if not taken:
        return {}
    rows = await _load_rows(session, company_id, set(taken))
    skus = {str((r.state or {}).get("sku") or "").strip() for r in rows.values()}
    claims = [c for c in await unshipped_claims(session, company_id, exclude=doc_id, skus=skus)
              if c.lot_id in taken]
    moving: dict[str, list[UnshippedClaim]] = {}
    for lot_id, take in taken.items():
        row = rows.get(lot_id)
        on = [c for c in claims if c.lot_id == lot_id]
        remaining = float((row.state or {}).get("quantity") or 0) - take if row is not None else 0.0
        over = min(take, sum(c.qty for c in on) - max(remaining, 0.0))
        for claim in reversed(on):
            if over <= 1e-9:
                break
            qty = min(claim.qty, over)
            over -= qty
            moving.setdefault(claim.doc_id, []).append(
                UnshippedClaim(doc_id=claim.doc_id, line=claim.line, lot_id=lot_id, qty=qty, key=claim.key,
                               amount=claim.amount * qty / claim.qty if claim.qty else 0.0, on_hand=claim.on_hand))
    return moving


async def create_for_cost_moves(session, *, company_id, user_id, doc_id: str, doc_number: str, cycle_tag: str,
                                moving: dict[str, list[UnshippedClaim]], ts: str | None) -> list[dict]:
    """Move the cost of goods invoice ``doc_id`` ships from the open invoices that had set
    them aside (moved_costs): one entry per invoice, Dr cost of goods sold for the shipper,
    Cr cost of goods sold for the other, at the cost that invoice set them aside at.
    Inventory is not touched: it gave the goods up once, when they were set aside. The
    other invoice holds that many fewer units (recognized_cogs) and is costed for them
    when it ships. Returns the moves for the notice: {sku, lot_id, doc_id, doc_number}."""
    if not moving:
        return []
    cogs_code = await resolve(session, company_id, R.COGS)
    currency = await company_currency(session, company_id)
    sources = await _recognitions(session, company_id, list(moving))
    rows = await _load_rows(session, company_id, set(moving) | {c.lot_id for cs in moving.values() for c in cs})
    notice: list[dict] = []
    for from_doc, claims in sorted(moving.items()):
        source = sources.get(from_doc)
        if source is None:
            continue
        from_state = (rows[from_doc].state or {}) if from_doc in rows else {}
        from_number = from_state.get("doc_number") or from_state.get("ref_id") or from_doc
        amount = to_stored_float(round_money(sum(c.amount for c in claims), currency))
        skus = sorted({str((rows[c.lot_id].state or {}).get("sku") or "") for c in claims if c.lot_id in rows})
        await _emit_auto_posted_je(
            session, company_id=company_id, user_id=user_id,
            je_id=f"je:auto:{doc_id}:cost-move:{cycle_tag}:{from_doc}",
            idem_create=je_idempotency_key(doc_id, f"cost_move:{cycle_tag}:{from_doc}", "c"),
            idem_posted=je_idempotency_key(doc_id, f"cost_move:{cycle_tag}:{from_doc}", "p"),
            memo=f"Cost of {', '.join(skus)} moved from {from_number} to {doc_number}, which shipped them",
            ts=ts, currency=currency,
            entries=[_line(cogs_code, R.COGS, debit=amount), _line(cogs_code, R.COGS, credit=amount)],
            metadata_={"trigger": "doc.cost_moved", "doc_id": doc_id, "from_doc_id": from_doc,
                       "from_je": source.je_id,
                       "moves": [{"line": c.line, "lot_id": c.lot_id, "qty": c.qty, "key": c.key,
                                  "amount": c.amount} for c in claims]},
        )
        for lot_id in dict.fromkeys(c.lot_id for c in claims):
            notice.append({"sku": (rows[lot_id].state or {}).get("sku") if lot_id in rows else lot_id,
                           "lot_id": lot_id, "doc_id": from_doc, "doc_number": from_number})
    return notice


async def reconcile_doc_cogs(
    session, *, company_id, user_id, doc_id: str, cycle_tag: str, ts: str | None,
    trigger: str, memo: str | None = None, context: dict | None = None,
) -> None:
    """Bring an invoice's booked COGS to what it recognizes today, in one entry.

    A shipped line recognizes the actual cost of the lots it shipped, plus its
    allocation's share for any quantity it has not shipped (an imported invoice can
    deliver part of a line). A line not shipped recognizes its finalize allocation plus
    every cost correction since recorded against that allocation, less the goods other
    invoices shipped and took the cost of. Goods the invoice took back into stock and no
    longer holds (Set as available after shipping) take their share of the allocation
    with them, so a lot sold again elsewhere is costed once. The difference from the
    cost of sales the invoice already books - the net relief of inventory by its live
    entries, whichever account carries the expense, plus the cost it took over from other
    invoices less the cost they took over from it (cost moves) - is taken once each
    account's truth is rounded to the currency, for the whole invoice, and posted
    through create_for_doc_cogs_adjustment.

    An invoice void or back in draft recognizes nothing: once its own entries are
    reversed, any cost it still carries through moves is settled on a cost-settle entry,
    which no void or restore touches. An invoice with no recognized allocation on record
    posts nothing. Raises ValueError when a shipped lot cannot be matched to one of the
    invoice's lines.
    """
    settings = await current_settings(session, company_id)
    doc = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    doc_state = (doc.state or {}) if doc is not None else {}
    prefix = f"je:auto:{doc_id}:"
    jes = (await session.execute(_select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix, autoescape=True)))).scalars().all()
    live = [r for r in jes if (r.state or {}).get("status") == "posted"]
    standing = any(_finalize_root(doc_id, r.entity_id) for r in live)
    truth: dict[str, float] = {}
    if standing:
        books = await _read_books(session, company_id, [doc_id])
        if doc_id not in books.recognized:
            return
        truth, held, _unshipped = await _recognized_by_account(session, company_id, doc_id, doc_state, books)
        for by_account in held.values():
            for code, amount in by_account.items():
                truth[code] = truth.get(code, 0.0) + amount
    booked: dict[str, float] = {}
    for row in live:
        suffix = row.entity_id[len(prefix):]
        if _recognition_root(suffix) is None and not suffix.startswith("cost-settle:"):
            continue
        for e in (row.state or {}).get("entries", []):
            if any(line_has_role(settings, e, r) for r in (R.INVENTORY_PURCHASED, R.INVENTORY_OPENING,
                                                           R.CONSIGNOR_PAYABLE)):
                # The consignor payable is owed per consignor, so each is trued up apart.
                key = party_key(e["account"], e.get("contact")) if line_has_role(
                    settings, e, R.CONSIGNOR_PAYABLE) else e["account"]
                booked[key] = booked.get(key, 0.0) + float(e.get("credit") or 0) - float(e.get("debit") or 0)
    for move in await _cost_moves(session, company_id, doc_id=doc_id):
        sign = 1.0 if move.get("doc_id") == doc_id else -1.0
        for m in move.get("moves") or []:
            booked[m["key"]] = booked.get(m["key"], 0.0) + sign * float(m["amount"])
    # Booked amounts are already money, so the truth is compared once it is money too:
    # half a cent of cost recognized at finalize is not given back at fulfillment.
    currency = await company_currency(session, company_id)
    truth = {code: to_stored_float(round_money(amount, currency)) for code, amount in truth.items()}
    await create_for_doc_cogs_adjustment(
        session, company_id=company_id, user_id=user_id, doc_id=doc_id,
        delta={code: truth.get(code, 0.0) - booked.get(code, 0.0) for code in truth.keys() | booked.keys()},
        cycle_tag=cycle_tag, doc_number=doc_state.get("doc_number") or doc_state.get("ref_id") or doc_id,
        ts=ts, trigger=trigger, memo=memo, context=context, family="cogs-adj" if standing else "cost-settle",
    )


async def _allocation_by_account(session, company_id, alloc: dict, amount: float, payable_codes) -> dict[str, float]:
    """``amount`` of a line's finalize allocation, split over the accounts its lots are
    costed against (sold_lot_key), by each lot's share of the allocated cost. A lot
    allocated while on consignment and bought since is costed against the inventory it
    became."""
    lots = alloc.get("lots") or []
    if not amount or not lots:
        return {}
    parts: dict[str, float] = {}
    for lot in lots:
        code = await _allocated_lot_key(session, company_id, lot, payable_codes)
        parts[code] = parts.get(code, 0.0) + float(lot.get("qty") or 0) * float(lot.get("unit_cost") or 0)
    return _shares(parts, amount)


async def _allocated_lot_key(session, company_id, lot: dict, payable_codes) -> str:
    """The account an allocated lot is costed against (sold_lot_key), the one recorded
    in the allocation unless it is a consignor payable."""
    code = lot.get("account")
    if not code or code in payable_codes:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": lot["lot_entity_id"]})
        code = await sold_lot_key(session, company_id, lot["lot_entity_id"],
                                  (row.state or {}) if row is not None else {})
    return code


async def create_for_doc_cogs_adjustment(
    session, *, company_id, user_id, doc_id: str, delta: dict[str, float], cycle_tag: str, doc_number: str,
    ts: str | None = None, trigger: str = "doc.fulfilled", memo: str | None = None,
    context: dict | None = None, family: str = "cogs-adj",
) -> None:
    """Post one COGS true-up JE for a document (see reconcile_doc_cogs), on the family
    ``family``: cogs-adj, a recognition entry void and restore carry with the invoice, or
    cost-settle, which settles cost an invoice no longer standing carries and stays.

    delta is per inventory account: a positive amount debits COGS and relieves that
    account; a negative one reverses that. Each amount is rounded once to the
    company currency and nothing posts when they all round to zero. cycle_tag scopes the JE id and its idempotency keys to the
    event that triggered it (fulfill-0:l0-1, reverse-1:l2, restate-<id>), so
    replaying that event is a no-op while each distinct event trues up on its
    own JE.
    """
    currency = await company_currency(session, company_id)
    rounded = {code: round_money(v, currency) for code, v in delta.items()}
    rounded = {code: v for code, v in rounded.items() if v != 0}
    if not rounded:
        return
    entries = await _cogs_entries(session, company_id, {code: to_stored_float(v) for code, v in rounded.items()})
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{doc_id}:{family}:{cycle_tag}",
        idem_create=je_idempotency_key(doc_id, f"{family}:{cycle_tag}" if family != "cogs-adj" else f"cogs_adjustment:{cycle_tag}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"{family}:{cycle_tag}" if family != "cogs-adj" else f"cogs_adjustment:{cycle_tag}", "p"),
        memo=memo or f"COGS adjustment for {doc_number}",
        ts=ts,
        entries=entries,
        metadata_={
            "trigger": trigger, "doc_id": doc_id, "cogs_delta": to_stored_float(sum(rounded.values(), _Dec(0))),
            **(context or {}),
        },
    )


async def _free_goods_only(session, company_id, doc_id: str, allocations: dict) -> dict:
    """A restored invoice's finalize allocation, holding only goods still on hand that no
    other open invoice has set aside (unshipped_claims), lines in document order. What it
    can no longer hold becomes provisional, costed when the invoice ships, and the line's
    amount keeps only the cost of what it still holds; the reconcile after the restore
    brings the books to it."""
    claimed: dict[str, float] = {}
    for claim in await unshipped_claims(session, company_id, exclude=doc_id, skus=None):
        claimed[claim.lot_id] = claimed.get(claim.lot_id, 0.0) + claim.qty
    out: dict = {}
    for idx in sorted(allocations, key=int):
        alloc = allocations[idx]
        lots, cut, cost, kept_cost = [], 0.0, 0.0, 0.0
        for lot in alloc.get("lots") or []:
            row = await session.get(Projection, {"company_id": company_id, "entity_id": lot["lot_entity_id"]})
            free = 0.0
            if row is not None and _on_hand_value(row) is not None:
                free = max(0.0, float((row.state or {}).get("quantity") or 0) - claimed.get(lot["lot_entity_id"], 0.0))
            qty = float(lot.get("qty") or 0)
            keep = min(qty, free)
            claimed[lot["lot_entity_id"]] = claimed.get(lot["lot_entity_id"], 0.0) + keep
            cost += qty * float(lot.get("unit_cost") or 0)
            kept_cost += keep * float(lot.get("unit_cost") or 0)
            cut += qty - keep
            if keep > 1e-9:
                lots.append({**lot, "qty": keep})
        amount = float(alloc.get("amount") or 0)
        out[idx] = {**alloc, "lots": lots, "provisional_qty": float(alloc.get("provisional_qty") or 0) + cut,
                    "amount": amount * kept_cost / cost if cut > 1e-9 and cost else (amount if not cut else 0.0)}
    return out


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

    An imported document's own entry from an earlier release is never restored (the
    opening balances hold the document), and the reversal its void posted of what they
    hold is taken back (take_back_imported_reversals).

    Docs voided before batch stamping existed have per-JE void events with no
    batch number; for those, each recognition family whose latest void event
    came from a doc void (not a revert to draft) restores the JE that event
    reversed.
    """
    from celerp.models.ledger import LedgerEntry

    prefix = f"je:auto:{doc_id}:"
    jes = await _doc_recognition_jes(session, company_id, doc_id)
    void_events = await _doc_void_events(session, company_id, doc_id)
    imported = await imported_document(session, company_id, doc_id)

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
        if imported is not None and created is not None and created.id in imported.origins:
            continue  # an earlier release's entry for the import: the opening balances hold it
        # The allocation snapshot rides along so fulfillment still trues up against
        # what the restored JE recognizes.
        allocations = ((created.metadata_ or {}) if created else {}).get("cogs_allocations")
        if allocations:
            allocations = await _free_goods_only(session, company_id, doc_id, allocations)
        metadata_ = {
            **_recognition_metadata("doc.unvoided", doc_id, allocations),
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
    await take_back_imported_reversals(session, company_id=company_id, user_id=user_id, doc_id=doc_id)


async def create_for_doc_fulfilled(session, *, company_id, user_id, doc_id: str, lot_costs: dict[str, float], cycle: int = 0, ts: str | None = None) -> None:
    """Create COGS JE when a doc is fulfilled: Debit COGS / Credit Inventory.

    lot_costs is the cost each shipped lot takes off the books, relieved on the
    inventory account that lot is valued in. cycle must be incremented each time a doc is re-fulfilled (e.g. use doc revert_count so that
    fulfill → revert → re-fulfill produces distinct JE idempotency keys and entity IDs).
    """
    if sum(lot_costs.values()) <= 0:
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
        entries=await _cogs_entries(session, company_id, await lots_by_account(session, company_id, lot_costs)),
        metadata_={"trigger": "doc.fulfilled", "doc_id": doc_id},
    )


async def create_for_return_received(session, *, company_id, user_id, cn_id: str, lot_costs: dict[str, float], je_suffix: str,
                                     received_at: str) -> None:
    """Reversing COGS JE when goods are returned via credit note: Debit Inventory / Credit COGS.

    lot_costs is each returned lot's cost, put back on the inventory account that lot is valued in.

    je_suffix names the receive-return call, so each return received on the credit note has its own entry.
    received_at is when the return recorded the goods received; the entry is dated its business day.
    """
    if sum(lot_costs.values()) <= 0:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{cn_id}:return:{je_suffix}",
        idem_create=je_idempotency_key(cn_id, f"return:{je_suffix}", "c"),
        idem_posted=je_idempotency_key(cn_id, f"return:{je_suffix}", "p"),
        memo=f"Auto JE for {cn_id} return received (COGS reversal)",
        ts=await entry_day(session, company_id, received_at),
        entries=await _cogs_entries(session, company_id, await lots_by_account(session, company_id, lot_costs),
                                    expense=False),
        metadata_={"trigger": "doc.return_received", "cn_id": cn_id},
    )


async def create_for_consigned_return_bought(
    session, *, company_id, user_id, bill_id: str, lot_id: str, account: str, value: float,
    payable: str, owed: float, ts: str,
) -> None:
    """Consigned goods a customer returned, bought on a bill: Dr inventory / Cr Consignor
    payable, the difference to COGS.

    The return took the goods' recorded cost (``owed``) back off cost of goods sold onto
    the consignor payable, while the invoice that sold them settles that payable once the
    goods are bought. The goods held are now the company's own at the bill's cost
    (``value``), so that cost goes onto ``account`` and the payable the return recognized
    is cleared against cost of goods sold. Returned goods that went back to the consignor
    are not held, so they add nothing to ``value``. Keyed by the bill and the returned
    lot, so it posts once."""
    entries = await _cogs_entries(session, company_id, {account: -value, payable: owed})
    if not entries:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{bill_id}:consigned-return:{lot_id}",
        idem_create=je_idempotency_key(bill_id, f"consigned_return:{lot_id}", "c"),
        idem_posted=je_idempotency_key(bill_id, f"consigned_return:{lot_id}", "p"),
        memo=f"Consigned goods returned by a customer, bought on {bill_id}",
        ts=ts,
        entries=entries,
        metadata_={"trigger": "doc.converted_to_bill", "doc_id": bill_id, "item_id": lot_id},
    )


async def create_for_return_undone(session, *, company_id, user_id, cn_id: str, lot_costs: dict[str, float], unique_suffix: str,
                                   undone_at: str) -> None:
    """Reverse the COGS reversal JE when a receive-return is undone: Debit COGS / Credit Inventory.

    lot_costs is each returned lot's cost, taken off the inventory account that lot is valued in.

    unique_suffix must be unique per call (e.g. a UUID) so repeated undo attempts each get their own JE.
    undone_at is when the undo was recorded; the entry is dated its business day.
    """
    if sum(lot_costs.values()) <= 0:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{cn_id}:return:undo:{unique_suffix}",
        idem_create=je_idempotency_key(cn_id, f"return.undo.{unique_suffix}", "c"),
        idem_posted=je_idempotency_key(cn_id, f"return.undo.{unique_suffix}", "p"),
        memo=f"Auto JE for {cn_id} return undone (COGS re-reversal)",
        ts=await entry_day(session, company_id, undone_at),
        entries=await _cogs_entries(session, company_id, await lots_by_account(session, company_id, lot_costs)),
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


async def create_for_mfg_movement(
    session, *, company_id, user_id, order_id: str, movement: str, memo: str, wip_code: str | None,
    wip: _Dec, lots: dict[str, _Dec], waste: _Dec = _Dec(0), equity: _Dec = _Dec(0), day: str,
) -> None:
    """Post one production run movement: ``wip`` onto (positive) or off (negative) the run's
    work in progress account ``wip_code``, ``lots`` onto or off the inventory accounts the lots
    record, ``waste`` to cost of goods sold, and ``equity`` (value the books first recognize,
    as opening stock is) to retained earnings. Amounts are already money in the company
    currency and balance. ``movement`` names the operation (issue:<key>, return:<key>,
    receive:<key>, unreceive:<key>, complete:<key>, reopen:<key>, wip-opened) and keys the entry, so a retried operation posts nothing more. Nothing posts
    when every amount is zero."""
    def _side(amount: _Dec) -> dict:
        value = to_stored_float(abs(amount))
        return {"debit": value} if amount > 0 else {"credit": value}

    settings = await current_settings(session, company_id)
    entries = [_lot_line(settings, code, **_side(amount)) for code, amount in sorted(lots.items()) if amount]
    if wip:
        entries.append(_line(wip_code, R.WORK_IN_PROGRESS, **_side(wip)))
    if waste:
        entries.append(_line(await resolve(session, company_id, R.COGS), R.COGS, **_side(waste)))
    if equity:
        entries.append(_line(await resolve(session, company_id, R.RETAINED_EARNINGS), R.RETAINED_EARNINGS,
                             **_side(equity)))
    if not entries:
        return
    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=f"je:auto:{order_id}:{movement}",
        idem_create=je_idempotency_key(order_id, f"mfg.{movement}", "c"),
        idem_posted=je_idempotency_key(order_id, f"mfg.{movement}", "p"),
        memo=memo,
        ts=day,
        entries=entries,
        metadata_={"trigger": f"mfg.order.{movement.split(':')[0]}", "order_id": order_id},
    )


# Memo prefix per list-adjustment kind; the full memo is "<prefix> <list_id>".
_ADJUST_MEMO = {"audit": "Inventory audit adjustment", "writeoff": "Inventory write-off"}


def _list_adjustment_je_id(list_id: str, kind: str, cycle: int) -> str:
    return f"je:auto:{list_id}:{kind}:{cycle}"


async def create_for_line_adjustment(
    session, *, company_id, user_id, list_id: str, kind: str, entries: list[dict], cycle: int = 0,
    recorded: object = None,
) -> None:
    """Post one balanced auto JE for a list terminal that adjusts stock value, dated the
    business day of ``recorded``, when the terminal started (entry_day).

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
        ts=await entry_day(session, company_id, recorded),
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
    session, *, company_id, user_id, list_id: str, shrinkage: dict[str, float], overage: dict[str, float],
    cycle: int = 0, recorded: object = None,
) -> None:
    """Post the balanced inventory write-down/up JE for an audit's stock adjustment.

    shrinkage / overage = value lost (count below system) / gained, per inventory account
    of the lots counted.
    Shrinkage (count below system) is Dr stock shrinkage / Cr inventory; overage is Dr inventory /
    Cr stock gains. Thin wrapper: builds those entries and delegates to the shared list-adjustment
    poster (kind="audit"), so audit and write-off share one posting core.
    """
    currency = await company_currency(session, company_id)
    lost = {code: round_money(v, currency) for code, v in sorted(shrinkage.items())}
    gained = {code: round_money(v, currency) for code, v in sorted(overage.items())}
    shrink_d, over_d = sum(lost.values(), _Dec(0)), sum(gained.values(), _Dec(0))
    if not (shrink_d > 0 or over_d > 0):
        return
    acc = await resolve_many(session, company_id, [
        *([R.STOCK_SHRINKAGE] if shrink_d > 0 else []), *([R.STOCK_GAIN] if over_d > 0 else [])])
    settings = await current_settings(session, company_id)
    entries: list[dict] = []
    if shrink_d > 0:
        entries.append(_line(acc[R.STOCK_SHRINKAGE], R.STOCK_SHRINKAGE, debit=to_stored_float(shrink_d)))
        entries += [_lot_line(settings, code, credit=to_stored_float(a))
                    for code, a in lost.items() if a]
    if over_d > 0:
        entries += [_lot_line(settings, code, debit=to_stored_float(a))
                    for code, a in gained.items() if a]
        entries.append(_line(acc[R.STOCK_GAIN], R.STOCK_GAIN, credit=to_stored_float(over_d)))
    await create_for_line_adjustment(
        session, company_id=company_id, user_id=user_id, list_id=list_id,
        kind="audit", entries=entries, cycle=cycle, recorded=recorded,
    )


async def void_for_audit_adjustment(session, *, company_id, user_id, list_id: str, cycle: int = 0) -> None:
    """Void the audit-adjustment JE (undo). No-op if it was never posted (zero-value adjustment)."""
    await void_for_list_adjustment(
        session, company_id=company_id, user_id=user_id, list_id=list_id, kind="audit", cycle=cycle,
    )


async def book_opening_inventory(
    session,
    *,
    company_id,
    user_id,
    in_production: _Dec,
) -> None:
    """Auto-post (or update) the opening inventory JE for pre-system stock.

    Computes gap = catalog_cost_total (stocked, non-consignment, non-archived, plus
    ``in_production``: what older releases issued to runs still open while the inventory
    accounts kept carrying it, lot_origin.in_production) minus the sum of all JE-backed balances on the inventory value accounts
    (excluding the OB JE itself). The gap is rounded once to the company currency; a positive representable
    amount emits/updates je:auto:opening-inventory:{company_id}, while zero voids the OB JE.

    The gap is debited to each account that has served as the opening inventory
    account, up to what the lots recording it hold beyond what is already posted
    there, so every such account carries exactly its lots' value after the opening
    account is changed; whatever is left goes to the current opening account.

    When the gap or its split changes, voids the old JE and posts a fresh one so
    it stays current. Idempotent. Raises when the entry cannot be written (posting
    accounts, a period lock on either half, an unreadable timezone), before anything
    changes.
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

    catalog_total = _Dec("0")
    by_lot_account: dict[str, _Dec] = {}  # catalog cost per recorded inventory account
    for row in item_rows:
        value = held_value(row)
        if value is None:
            continue
        catalog_total += value
        if row.state.get(LOT_ACCOUNT_FIELD):
            by_lot_account[row.state[LOT_ACCOUNT_FIELD]] = by_lot_account.get(row.state[LOT_ACCOUNT_FIELD], _Dec("0")) + value

    catalog_total += in_production

    # --- JE-backed inventory: every posted line that holds the value of goods on hand ---
    je_rows = (
        await session.execute(
            _sel(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "journal_entry",
            )
        )
    ).scalars().all()

    # Inventory value is every line posted for goods, their opening balance, or landed cost
    # waiting to be capitalised (INVENTORY_VALUE_ROLES). catalog cost_total includes capitalised
    # landed cost, so the JE-backed sum must cover the clearing roles too or the landed amount
    # would show up as a spurious opening-balance gap.
    settings = await current_settings(session, company_id)
    value_roles = {str(r) for r in INVENTORY_VALUE_ROLES}
    ob_je_id = f"je:auto:opening-inventory:{company_id}"
    je_backed = _Dec("0")
    backed_by_account: dict[str, _Dec] = {}
    ob_proj = None
    for row in je_rows:
        if row.entity_id == ob_je_id:
            ob_proj = row
            continue  # exclude OB JE itself from gap calculation
        s = row.state
        if s.get("status") != "posted":
            continue
        for entry in s.get("entries", []):
            if value_roles.intersection(line_roles(settings, entry)):
                net = _Dec(str(entry.get("debit") or 0)) - _Dec(str(entry.get("credit") or 0))
                je_backed += net
                code = entry.get("account")
                backed_by_account[code] = backed_by_account.get(code, _Dec("0")) + net

    gap = catalog_total - je_backed
    base_currency = settings.get("currency", "USD")

    # Current OB JE split (empty if not posted): its opening-inventory debits, per account
    current: dict[str, _Dec] = {}
    if ob_proj and ob_proj.state.get("status") == "posted":
        for entry in ob_proj.state.get("entries", []):
            if float(entry.get("debit") or 0) and line_has_role(settings, entry, R.INVENTORY_OPENING):
                current[entry["account"]] = round_money(entry["debit"], base_currency)

    needed_d = round_money(gap, base_currency)
    if needed_d < 0:
        needed_d = _Dec("0")
    needed = to_stored_float(needed_d)

    split: dict[str, _Dec] = {}
    if needed_d > 0:
        acc = await resolve_many(session, company_id, [R.INVENTORY_OPENING, R.RETAINED_EARNINGS])
        left = needed_d
        opening = scope_codes(settings, R.INVENTORY_OPENING)
        for code in sorted(c for c in {*by_lot_account, *backed_by_account} if c in opening):
            share = min(round_money(by_lot_account.get(code, _Dec("0")) - backed_by_account.get(code, _Dec("0")),
                                    base_currency), left)
            if share > 0:
                split[code] = share
                left -= share
        if left > 0:
            target = acc[R.INVENTORY_OPENING]
            split[target] = split.get(target, _Dec("0")) + left

    def _signature(amounts: dict[str, _Dec]) -> str:
        return ",".join(f"{code}={to_stored_float(v)}" for code, v in sorted(amounts.items()))

    if split == current:
        # Split is correct - but also void+repost if the JE is missing a ts (dateless legacy)
        if not (ob_proj and ob_proj.state.get("status") == "posted" and not ob_proj.state.get("ts")):
            return  # already correct and has a date, nothing to do

    # Both halves (void + repost) are lock-checked BEFORE anything mutates, so a lock
    # can never leave a half-done restatement behind. The entry is dated the
    # company's business day.
    from celerp.events.engine import _check_period_lock

    today = await entry_day(session, company_id)
    if ob_proj and ob_proj.state.get("status") == "posted":
        await _check_period_lock(session, company_id, je_void_data("", ob_proj.state))
    if needed_d > 0:
        await _check_period_lock(session, company_id, {"ts": today})
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
            idempotency_key=f"opening-inv:{company_id}:void:{_signature(current)}",
            metadata_={"trigger": "opening_inventory.auto"},
        )

    if needed_d <= 0:
        return  # gap closed, no new JE needed

    await _emit_auto_posted_je(
        session,
        company_id=company_id,
        user_id=user_id,
        je_id=ob_je_id,
        idem_create=f"opening-inv:{company_id}:c:{_signature(split)}:{today}",
        idem_posted=f"opening-inv:{company_id}:p:{_signature(split)}:{today}",
        memo="Opening inventory balance (pre-system stock)",
        entries=[
            *(_line(code, R.INVENTORY_OPENING, debit=to_stored_float(v)) for code, v in sorted(split.items())),
            _line(acc[R.RETAINED_EARNINGS], R.RETAINED_EARNINGS, credit=needed),
        ],
        metadata_={"trigger": "opening_inventory.auto"},
        ts=today,
    )
