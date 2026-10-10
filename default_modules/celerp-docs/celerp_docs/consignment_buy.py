# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Converting a consignment to a vendor bill: buying the consigned goods.

The bill buys what the company kept of the consignment: every unit received on it that
was not returned to the consignor, whether still held or sold. Its lines are cut to
that quantity, and it books Dr inventory / Cr accounts payable as any bill does.

Every lot that came from the consignment then becomes the company's own, valued at
the bill's cost per unit on the account the bill debited (``item.consignment.bought``),
so the inventory the bill booked is exactly what those lots hold:

- goods still held stay in inventory at that cost;
- goods sold were costed at their recorded consignment cost against the consignor
  payable, and each invoice that sold or holds them is trued up once
  (``auto_je.reconcile_doc_cogs``): the payable clears, the bill's cost leaves
  inventory and the difference goes to cost of goods sold;
- goods a customer returned went back onto the consignor payable, so the cost they
  now carry moves into inventory against that payable
  (``auto_je.create_for_consigned_return_bought``);
- goods a customer returned that then went back to the consignor were never bought:
  the lot they were sold from is costed only for the units the customer kept, and
  what the return put back onto the payable goes to cost of goods sold, so the
  payable ends at what is owed for the goods kept.

Anything the bill cannot follow without guessing is refused before anything is written.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import CONSIGNOR_PAYABLE_FIELD, LOT_ACCOUNT_FIELD, ON_BOOKS_FIELD, AccountRole, refusal
from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.account_roles import consignor_of, is_consigned, lineage, new_lot_account, party_key
from celerp.services.company_lock import lock_projections
from celerp.services.lot_origin import RETIRED
from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp_docs.doc_money import UnratedTaxError, document_money

_EPS = Decimal("1e-9")


def _changed(sku: str) -> HTTPException:
    return HTTPException(status_code=409, detail=refusal(
        "consignment.buy.changed",
        f"Stock {sku} received on this consignment has changed in a way the vendor bill cannot follow "
        "(merged, transformed, or carrying landed cost), so buying it cannot be booked without guessing.",
        sku=sku))


def _untraced(sku: str) -> HTTPException:
    return HTTPException(status_code=409, detail=refusal(
        "consignment.buy.untraced",
        f"Stock {sku} from this consignment is sold, but not on an invoice that records its cost, so what "
        "is owed for it cannot be settled without guessing.",
        sku=sku))


@dataclass
class _Group:
    """The lots that hold what one received lot brought in: the lot and its parts, or
    the goods a customer returned from them (``returned``) and their parts."""

    line: int
    basis: Decimal  # bill units per stock unit of the received lot
    received: Decimal  # stock units the received lot came in with
    returned: str | None = None
    source: str | None = None  # the lot the customer return named
    gone: Decimal = Decimal(0)  # stock units of them that went back to the consignor
    members: list[Projection] = field(default_factory=list)


@dataclass
class _Plan:
    groups: list[_Group]
    kept: dict[int, Decimal]  # line -> bill units kept
    back: dict[str, Decimal]  # sold lot -> its stock units that went back to the consignor
    allocations: dict[str, dict[tuple[str, str, int], float]]  # lot -> current allocations
    docs: set[str]

    @property
    def lots(self) -> set[str]:
        return {m.entity_id for g in self.groups for m in g.members}


def _sku(state: dict) -> str:
    return str(state.get("sku") or "item")


async def _created(session, company_id, lot_ids) -> dict[str, dict]:
    rows = (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.data).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(list(lot_ids)),
        LedgerEntry.event_type == "item.created"))).all()
    return {entity_id: data or {} for entity_id, data in rows}


async def _sale_doc(session, company_id, lot_id: str) -> str | None:
    """The invoice that shipped a sold lot, from its latest sale event."""
    sale = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == lot_id,
        LedgerEntry.event_type.in_(("item.fulfilled", "item.status.set")),
    ).order_by(LedgerEntry.id.desc()).limit(1))).scalars().first()
    if sale is None or sale.event_type != "item.fulfilled":
        return None
    return (sale.data or {}).get("source_doc_id")


async def _sent_back(session, company_id, lot_ids) -> set[str]:
    """Of these disposed lots, the ones a return to the supplier disposed: the movement that
    last took them off the books is ``item.returned_to_supplier``, not a write-off. A lot
    keeps its quantity when it goes back, as the record of what left, so the quantity alone
    cannot tell goods returned to the consignor from goods lost while held."""
    if not lot_ids:
        return set()
    rows = (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.event_type).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(sorted(lot_ids)),
        LedgerEntry.event_type.in_(("item.returned_to_supplier", "item.written_off")),
    ).order_by(LedgerEntry.id))).all()
    last = {entity_id: event_type for entity_id, event_type in rows}
    return {lot for lot, event_type in last.items() if event_type == "item.returned_to_supplier"}


def _gave_up_value(state: dict) -> bool:
    """A lot retired from the catalog (RETIRED) that a split, transform, merge or undone
    receipt left behind holds no goods; one the user archived or expired keeps them on hand."""
    return str(state.get("status") or "").lower() in RETIRED and state.get(ON_BOOKS_FIELD) is not True


async def _plan(session, company_id, state: dict) -> _Plan:
    lines = state.get("line_items") or []
    receipts = [x for x in state.get("received_items") or [] if (x.get("receive_as") or "stock") == "stock"]
    roots = list(state.get("received_item_ids") or [])

    def line_sku(idx: int) -> str:
        return _sku(lines[idx]) if 0 <= idx < len(lines) else "item"

    if len(receipts) != len(roots):
        raise _changed(line_sku(0))
    stock_lines = {i for i, li in enumerate(lines) if auto_je.bill_line_kind(li) == "stock"}
    for x in state.get("received_items") or []:
        idx = int(x.get("po_line_index", -1))
        if ((x.get("receive_as") or "stock") == "stock") != (idx in stock_lines):
            raise _changed(line_sku(idx))
    created = await _created(session, company_id, roots)
    groups: list[_Group] = []
    of: dict[str, _Group] = {}
    for receipt, root in zip(receipts, roots):
        received = to_decimal((created.get(root) or {}).get("quantity") or 0)
        if received <= 0:
            raise _changed(line_sku(int(receipt.get("po_line_index", -1))))
        group = _Group(line=int(receipt["po_line_index"]), received=received,
                       basis=to_decimal(receipt.get("quantity_received") or 0) / received)
        groups.append(group)
        of[root] = group

    for row, parent, link in await lineage(session, company_id, of):
        if link in ("transformed_from", "merged_into"):
            raise _changed(_sku(row.state or {}))
        if link == "split_from":
            of[row.entity_id] = of[parent]
        elif link == "returned_from":
            of[row.entity_id] = _Group(line=of[parent].line, basis=of[parent].basis,
                                       received=of[parent].received, returned=row.entity_id, source=parent)
            groups.append(of[row.entity_id])
        of[row.entity_id].members.append(row)

    kept: dict[int, Decimal] = {}
    allocations: dict[str, dict[tuple[str, str, int], float]] = {}
    docs: set[str] = set()
    # What the bill buys is told apart by the movement that moved the units: goods on hand
    # and goods sold (historical sales, still owed to the consignor) are kept; goods a
    # return sent back to the consignor or the supplier are not.
    sent_back = await _sent_back(session, company_id, {
        m.entity_id for g in groups for m in g.members
        if str((m.state or {}).get("status") or "").lower() == "disposed"})
    for group in groups:
        members = []
        for row in group.members:
            ls = row.state or {}
            status = str(ls.get("status") or "").lower()
            qty = to_decimal(ls.get("quantity") or 0)
            if status == "merged":
                raise _changed(_sku(ls))
            if qty <= 0 or _gave_up_value(ls) or row.entity_id in sent_back:
                continue
            if not is_consigned(ls) or ls.get("landed_costs"):
                raise _changed(_sku(ls))
            members.append(row)
            allocations[row.entity_id] = await auto_je.allocations_naming_lot(session, company_id, row.entity_id)
            docs |= {doc for doc, _cycle, _line in allocations[row.entity_id]}
            if status == "sold":
                sale = await _sale_doc(session, company_id, row.entity_id)
                traced = {doc for doc, _c, _l in allocations[row.entity_id]} | ({sale} if sale else set())
                if not [d for d in traced if await auto_je.recognized_cogs(session, company_id, d) is not None]:
                    raise _untraced(_sku(ls))
                docs |= traced
        group.members = members
        if group.returned is None:
            held = sum((to_decimal(m.state.get("quantity") or 0) for m in members), Decimal(0))
            if held > group.received + _EPS:
                raise _changed(line_sku(group.line))
            kept[group.line] = kept.get(group.line, Decimal(0)) + held * group.basis
    # A received lot's sold units count as kept; any a customer brought back and that then
    # went back to the consignor were not kept after all.
    for x in state.get("returned_items") or []:
        group = of.get(x.get("item_id"))
        if group is not None and group.returned is not None:
            group.gone += to_decimal(x.get("quantity_returned") or 0)
            kept[group.line] -= to_decimal(x.get("quantity_returned") or 0) * group.basis
            if kept[group.line] < -_EPS:
                raise _changed(line_sku(group.line))
    return _Plan(groups=groups, kept=kept, back=_back(groups, of, line_sku), allocations=allocations, docs=docs)


def _back(groups: list[_Group], of: dict[str, _Group], line_sku) -> dict[str, Decimal]:
    """The sold units each sold lot no longer stands for: goods a customer returned and
    that went back to the consignor. A return names the lot on the invoice line, which
    may be the part still held when the sold part was split off, so the units are taken
    from the lot named if it was sold, then from the other sold parts of the same
    received lot, in a fixed order. More units gone back than were sold is refused."""
    back: dict[str, Decimal] = {}
    for group in groups:
        gone = group.gone
        if gone <= 0 or group.source not in of:
            continue
        sold = [m for m in of[group.source].members
                if str((m.state or {}).get("status") or "").lower() == "sold"]
        sold.sort(key=lambda m: (m.entity_id != group.source, m.entity_id))
        for row in sold:
            take = min(gone, to_decimal(row.state.get("quantity") or 0) - back.get(row.entity_id, Decimal(0)))
            if take > 0:
                back[row.entity_id] = back.get(row.entity_id, Decimal(0)) + take
                gone -= take
        if gone > _EPS:
            raise _changed(line_sku(group.line))
    return back


def _bill_lines(state: dict, kept: dict[int, Decimal], currency: str) -> tuple[list[dict], list[int], bool]:
    """The bill's lines for what was kept, the consignment line each came from, and
    whether any differs from the consignment's."""
    out: list[dict] = []
    source: list[int] = []
    changed = False
    for idx, li in enumerate(state.get("line_items") or []):
        line = dict(li)
        if line.get("entity_id") and line.get("item_id") and line["item_id"] != line["entity_id"]:
            line.pop("item_id")  # the bill buys the lot received, not the catalog item it came from
        if auto_je.bill_line_kind(li) == "stock":
            ordered = to_decimal(li.get("quantity") or 0)
            qty = kept.get(idx, Decimal(0))
            if abs(qty - ordered) > _EPS:
                changed = True
                if qty <= _EPS:
                    continue
                total = to_decimal(li.get("line_total") or 0) or ordered * to_decimal(li.get("unit_price") or 0)
                line["quantity"] = to_stored_float(qty)
                line["line_total"] = to_stored_float(round_money(total * qty / ordered, currency)
                                                     if ordered > 0 else Decimal(0))
        out.append(line)
        source.append(idx)
    return out, source, changed


async def buy_consignment(session, *, company_id, user_id, consignment_id: str, state: dict, ref: str,
                          base_currency: str) -> str:
    """Create the vendor bill for an issued consignment and make every lot it bought the
    company's own (module docstring). Returns the bill's id."""
    plan = await _plan(session, company_id, state)
    await lock_projections(session, company_id, plan.docs)
    await lock_projections(session, company_id, plan.lots)
    locked = await _plan(session, company_id, state)
    if locked.docs != plan.docs or locked.lots != plan.lots:
        raise HTTPException(status_code=409, detail="The consignment's goods changed while it was being converted; try again")
    plan = locked

    currency = state.get("currency") or base_currency
    lines, source, changed = _bill_lines(state, plan.kept, currency)
    if not lines:
        raise HTTPException(status_code=409, detail=refusal(
            "consignment.buy.nothing_kept",
            "Nothing received on this consignment is still held or sold, so there is nothing to buy. "
            "Receive the goods first."))
    bill_id = f"doc:{ref}"
    data = {k: v for k, v in state.items() if k not in {"status", "entity_type", "amount_paid", "amount_outstanding"}}
    data["line_items"] = lines
    if changed:
        if to_decimal(state.get("discount") or 0) > 0 and state.get("discount_type") != "percentage":
            raise _cannot_recompute()
        try:
            data.update(document_money(data, lines, currency, keep_unrated_tax=False))
        except UnratedTaxError as exc:
            raise _cannot_recompute() from exc
    data.update({"doc_type": "bill", "ref_id": ref, "source_consignment_id": consignment_id,
                 "status": "awaiting_payment"})
    await emit_event(
        session, company_id=company_id, entity_id=bill_id, entity_type="doc", event_type="doc.created",
        data=data, actor_id=user_id, location_id=None, source="api",
        idempotency_key=f"consignment-buy:{bill_id}", metadata_={"source_doc": consignment_id},
    )
    total = data.get("total") or sum(
        float(li.get("quantity", 0) or 0) * float(li.get("unit_price", 0) or 0) for li in lines)
    debits = await auto_je.create_for_bill_conversion(
        session, company_id=company_id, user_id=user_id, doc_id=bill_id, doc={**data, "total": total},
        base_currency=base_currency)
    by_line = {source[j]: debit for j, debit in debits.items()}
    fallback = None
    if any(idx not in by_line for idx in plan.kept):
        fallback = await new_lot_account(session, company_id, AccountRole.INVENTORY_PURCHASED)

    costs = _lot_costs(plan, by_line, base_currency)
    day = await auto_je.entry_day(session, company_id, None)
    for group in plan.groups:
        account = by_line[group.line][0] if group.line in by_line else fallback
        if not account:
            raise HTTPException(status_code=409, detail=refusal(
                "posting.role_missing", "Choose an inventory account for purchased stock first.",
                role=AccountRole.INVENTORY_PURCHASED.value))
        for row in group.members:
            await _bought(session, company_id, user_id, row, costs[row.entity_id], account,
                          consignment_id, bill_id, plan.allocations.get(row.entity_id, {}))
        if group.returned and (group.members or group.gone > 0):
            await _rehome_returned(session, company_id, user_id, bill_id, group, costs, account, day)
    for doc_id in sorted(plan.docs):
        try:
            await auto_je.reconcile_doc_cogs(
                session=session, company_id=company_id, user_id=user_id, doc_id=doc_id,
                cycle_tag=f"bought-{hashlib.sha256(f'{bill_id}:{doc_id}'.encode()).hexdigest()[:16]}",
                ts=day, trigger="doc.converted_to_bill", memo=f"COGS adjustment: consigned goods bought on {ref}",
                context={"bill_id": bill_id})
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return bill_id


def _cannot_recompute() -> HTTPException:
    return HTTPException(status_code=409, detail=refusal(
        "consignment.buy.recompute",
        "Only part of this consignment is being bought, and its flat discount or tax amount cannot be split "
        "between the goods kept and the goods returned without guessing. Set the discount as a percentage "
        "and the tax as a rate on the consignment, then convert again."))


def _lot_costs(plan: _Plan, by_line: dict[int, tuple[str, Decimal]], base_currency: str) -> dict[str, Decimal]:
    """Each lot's cost at the bill's price per unit, for the units the company kept: a
    sold lot's units that a customer returned and that went back to the consignor are
    not. The lots received on a line share exactly what the bill debits for it, the
    rounding remainder going to the largest."""
    costs: dict[str, Decimal] = {}
    on_line: dict[int, list[str]] = {}
    for group in plan.groups:
        debit = by_line[group.line][1] if group.line in by_line else Decimal(0)
        kept = plan.kept.get(group.line, Decimal(0))
        unit = debit * group.basis / kept if kept > 0 else Decimal(0)
        for row in group.members:
            qty = to_decimal(row.state.get("quantity") or 0) - plan.back.get(row.entity_id, Decimal(0))
            costs[row.entity_id] = round_money(unit * max(qty, Decimal(0)), base_currency)
            if group.returned is None:
                on_line.setdefault(group.line, []).append(row.entity_id)
    for line, lots in on_line.items():
        debit = by_line[line][1] if line in by_line else Decimal(0)
        largest = max(lots, key=lambda lot: (costs[lot], lot))
        costs[largest] += debit - sum((costs[lot] for lot in lots), Decimal(0))
    return costs


async def _bought(session, company_id, user_id, row: Projection, cost: Decimal, account: str,
                  consignment_id: str, bill_id: str, allocations: dict) -> None:
    """Make one lot the company's own at ``cost``; every invoice line that has it
    allocated and not shipped is repriced by the change."""
    state = row.state or {}
    qty = float(state.get("quantity") or 0)
    change = float(cost) / qty - auto_je.lot_unit_cost(state)
    repriced = [{"doc_id": doc_id, "cycle": cycle, "line": line, "amount": allocated * change}
                for (doc_id, cycle, line), allocated in sorted(allocations.items()) if allocated * change]
    await emit_event(
        session, company_id=company_id, entity_id=row.entity_id, entity_type="item",
        event_type="item.consignment.bought",
        data={"cost_total": to_stored_float(cost), LOT_ACCOUNT_FIELD: account,
              "consignment_doc_id": consignment_id, "bill_doc_id": bill_id},
        actor_id=user_id, location_id=None, source="api",
        idempotency_key=f"consignment-buy:{bill_id}:{row.entity_id}",
        metadata_={"source_doc": bill_id, **({"cogs_repriced": repriced} if repriced else {})},
    )


async def _rehome_returned(session, company_id, user_id, bill_id: str, group: _Group,
                           costs: dict[str, Decimal], account: str, day: str) -> None:
    created = (await _created(session, company_id, [group.returned])).get(group.returned) or {}
    payable = created.get(CONSIGNOR_PAYABLE_FIELD)
    if not payable:
        raise _changed(_sku(created))
    owed = round_money(to_decimal(created.get("cost_price") or 0) * to_decimal(created.get("quantity") or 0),
                       await auto_je.company_currency(session, company_id))
    await auto_je.create_for_consigned_return_bought(
        session, company_id=company_id, user_id=user_id, bill_id=bill_id, lot_id=group.returned,
        account=account, value=to_stored_float(sum((costs[m.entity_id] for m in group.members), Decimal(0))),
        payable=party_key(payable, await consignor_of(session, company_id, group.returned, created)),
        owed=to_stored_float(owed), ts=day)
