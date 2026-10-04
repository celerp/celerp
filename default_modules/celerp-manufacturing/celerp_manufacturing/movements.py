# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Production run movements: the one implementation of Issue, Receive, Complete and Cancel,
and of undoing each: Return, Undo receipt and Reopen.

Every entry point (the run endpoints, one-tap build, Demand Planning, bulk actions and the
invoice finalize hook) moves stock and value through these functions, in the caller's
transaction.

The run owns its work in progress. Issue moves the stock value that actually left each
component, at that moment, from the inventory account the component records onto the run's
work in progress account, which is fixed at the first issue that carries value. Receive moves
a share of what the run holds onto the new lot's own inventory account. Completion trues the
lots up to the final cost through cost restatement, sends waste to cost of goods sold and
leaves the run holding nothing. Each undo is the exact reverse of its step, from the values
the step recorded, and is refused once what it would take back has changed. Each operation is dated once, takes the company lock before
the run and the items, and is identified by one key from which every event and journal entry
it writes is derived, so a retry finds what was written instead of moving anything again.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    INVENTORY_ORIGIN_KEY,
    INVENTORY_VALUE_ROLES,
    LOT_ACCOUNT_FIELD,
    SCHEMA_KEY,
    VALUED_FROM_KEY,
    AccountRole,
)
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.account_roles import (
    PostingRoleError,
    continue_role,
    current_settings,
    lot_account,
    new_lot_account,
    resolve,
    role_map,
    scope_codes,
)
from celerp.services.company_lock import lock_company, lock_projections
from celerp.services.document_lines import listing_record
from celerp.services.line_measures import splitting_allowed
from celerp.services.lot_origin import (
    RECORDED,
    account_room,
    account_rooms,
    books_from_elsewhere,
    consumed_values,
    held_value,
    is_stock_type,
    period_open,
)
from celerp.services.money import allocate_pro_rata, round_money

from . import run_events  # noqa: F401  (registers the run's own event types)
from .expansion import merge_inputs, output_line

# Namespace for produced-lot ids: a receipt retried with its key resolves to the same lot id.
MFG_LOT_NS = uuid.UUID("6f1d0c2a-7b3e-4a9c-8d5f-2e0a1b4c6d8e")
CLOSED_RUN_STATUSES = frozenset({"completed", "cancelled"})
_EPS = 1e-9
_ZERO = Decimal(0)
# Every stock event a run writes carries the run's id (older releases marked consumed components
# the same way), so a lot's history shows what the run did to it.
_ORDER_MARK = "manufacturing_order_id"
# Why an older run waits for reconciling when the books hold its value mixed with something else.
_DISAGREE = "books disagree"


def refuse(http_status: int, key: str, message: str, /, **params) -> HTTPException:
    """A refusal the UI can show in the user's language: ``message`` is the English text,
    ``message_key`` and ``params`` let a translation say the same thing."""
    return HTTPException(status_code=http_status, detail={
        "message": message, "message_key": f"mfg.{key}", "params": params})


def require_stock(state: dict, item_id: str, http_status: int = 422) -> None:
    """A run turns stock into stock: the product it makes and every component it uses are
    stocked items or components. Services and other costs belong in a recipe's labor and
    overhead, never among its materials."""
    if not is_stock_type(state):
        sku = state.get("sku") or item_id
        raise refuse(http_status, "not_stock",
                     f"{sku} is not a stocked item or component, so it cannot be made or used as a material in "
                     "production. Record services and other costs as labor or overhead.",
                     sku=sku, inventory_type=state.get("inventory_type"))


async def mfg_settings(session: AsyncSession, company_id) -> dict:
    company = await session.get(Company, company_id)
    return (company.settings or {}).get("manufacturing", {}) if company else {}


def outstanding_inputs(run_state: dict) -> list[dict]:
    """Per input, the quantity still to issue (required - already issued)."""
    out = []
    for inp in run_state.get("inputs", []):
        rem = float(inp.get("quantity") or 0) - float(inp.get("issued_qty") or 0)
        if rem > _EPS:
            out.append({"item_id": inp.get("item_id"), "quantity": round(rem, 6)})
    return out


def outstanding_output(run_state: dict) -> float:
    """Finished-goods quantity still to receive (expected - already received)."""
    expected = float((run_state.get("expected_outputs") or [{}])[0].get("quantity") or 0)
    return max(0.0, expected - float(run_state.get("received_qty") or 0))


def _money(value) -> Decimal:
    return Decimal(str(value or 0))


def _fingerprint(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()[:32]


@dataclass
class _Op:
    """One operation: who, on which run, and the business day everything it writes carries."""

    session: AsyncSession
    company_id: object
    user_id: object
    order_id: str
    day: str
    books: bool
    currency: str

    def round(self, value) -> Decimal:
        return round_money(value, self.currency)

    async def emit(self, entity_id: str, entity_type: str, event_type: str, data: dict, key: str,
                   location_id=None, metadata: dict | None = None):
        return await emit_event(
            self.session, company_id=self.company_id, entity_id=entity_id, entity_type=entity_type,
            event_type=event_type, data=data, actor_id=self.user_id, location_id=location_id,
            source="api", idempotency_key=key, metadata_=metadata or {})

    async def emit_run(self, event_type: str, data: dict, key: str):
        return await self.emit(self.order_id, "mfg_order", event_type, {**data, "ts": self.day}, key)

    async def post(self, movement: str, memo: str, wip_code: str | None, wip: Decimal,
                   lots: dict[str, Decimal], waste: Decimal = _ZERO, equity: Decimal = _ZERO) -> None:
        if not self.books:
            return
        await auto_je.create_for_mfg_movement(
            self.session, company_id=self.company_id, user_id=self.user_id, order_id=self.order_id,
            movement=movement, memo=memo, wip_code=wip_code, wip=wip, lots=lots, waste=waste, equity=equity,
            day=self.day)


async def _begin(session: AsyncSession, company_id, user_id, order_id: str, at: str) -> _Op:
    """Take the company lock (always first) and fix the operation's business day."""
    await lock_company(session, company_id)
    settings = await current_settings(session, company_id)
    return _Op(session=session, company_id=company_id, user_id=user_id, order_id=order_id,
               day=await auto_je.entry_day(session, company_id, at), books=SCHEMA_KEY in settings,
               currency=str(settings.get("currency") or "USD").upper())


async def _replayed(op: _Op, key: str, request: str):
    """The event an earlier attempt of this operation wrote, or None. A key reused for a
    different request is refused rather than answered with the first one's result."""
    stored = await find_event_by_idempotency(op.session, op.company_id, key)
    if stored is None:
        return None
    if stored.entity_id != op.order_id or (stored.data or {}).get("request") != request:
        raise refuse(409, "key_reused", "This request key was already used for a different action. "
                     "Send the action again without reusing the key.")
    return stored


async def _run(op: _Op) -> Projection:
    """The run, locked, as the last committed operation left it."""
    row = (await lock_projections(op.session, op.company_id, [op.order_id])).get(op.order_id)
    if row is None or row.entity_type != "mfg_order":
        raise HTTPException(status_code=404, detail="Order not found")
    return row


def _require_open(state: dict, action: str) -> None:
    if state.get("status") in CLOSED_RUN_STATUSES:
        raise refuse(409, "run_closed", f"This run is {state.get('status')}, so it cannot be {action}.",
                     status=state.get("status"), action=action)


async def _require_executable_shape(op: _Op, state: dict) -> None:
    """A run goes ahead (Issue, Receive, Complete) only as a run can be made today: every
    component at a quantity above zero, making a named product. Only an older release stored
    one otherwise; it can still be unwound (Return, Undo Receipt, Cancel)."""
    bad = [i.get("item_id") for i in state.get("inputs", []) if not float(i.get("quantity") or 0) > 0]
    if bad:
        rows = [await op.session.get(Projection, {"company_id": op.company_id, "entity_id": i}) for i in bad]
        names = ", ".join(((r.state or {}).get("sku") if r is not None else None) or i for r, i in zip(rows, bad))
        raise refuse(409, "run_shape", f"This run lists {names} at a quantity of zero or below, so it cannot go "
                     "ahead. Return what was issued to it and cancel it, then make it again with the quantities "
                     "it needs.", items=names)
    if not state.get("output_item_id"):
        raise refuse(409, "no_output", "This run does not name the product it makes, so it cannot go ahead. "
                     "Choose its product, or return what was issued to it and cancel it.")


def _wip(state: dict) -> Decimal:
    """What the run holds: issued value not yet moved to its lots or waste."""
    return _money(state.get("wip_issued")) - _money(state.get("wip_transferred")) - _money(state.get("wip_wasted"))


async def _wip_target(op: _Op) -> str:
    """The account a run's first valued issue starts its work in progress on."""
    try:
        return await resolve(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS)
    except PostingRoleError as exc:
        refusal = refuse(409, "wip_account_missing", exc.detail)
        refusal.headers = exc.headers
        raise refusal from exc


def _reconcile() -> HTTPException:
    return refuse(409, "reconciliation_required",
                  "The value of the materials in this run is not recorded in the books yet, or "
                  "cannot be worked out from its history. Reconcile it before changing its materials "
                  "or output.")


def _require_settled(op: _Op, state: dict) -> None:
    """A run whose work in progress is not known, or is known but kept nowhere in the books,
    cannot move: anything it moved would leave the books unable to say where its value is."""
    if state.get("wip_unresolved") or state.get("wip_untracked") or (
            op.books and _wip(state) and not state.get("wip_account_code")):
        raise _reconcile()


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------

def _requested(run_state: dict, items: list[dict] | None) -> list[dict]:
    """The components to issue: the request with each item once, or everything outstanding."""
    if not items:
        return outstanding_inputs(run_state)
    for line in items:
        if not line.get("item_id") or float(line.get("quantity") or 0) <= 0:
            raise refuse(422, "issue_quantity", "Each component issued needs a quantity greater than zero.")
    return [{"item_id": i["item_id"], "quantity": round(i["quantity"], 6)} for i in merge_inputs(items)]


async def issue(session: AsyncSession, company_id, user_id, order_id: str, items: list[dict] | None,
                key: str | None, *, at: str) -> dict:
    """Issue components into a run: the ``items`` given, or everything still outstanding."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"items": items or None})
    stored = await _replayed(op, f"mfg:{order_id}:issue:{rk}", request)
    if stored is not None:
        return {"issued": stored.data.get("items") or [], "value": stored.data.get("value")}
    run = await _run(op)
    _require_open(run.state, "issued to")
    await _require_executable_shape(op, run.state)
    _require_settled(op, run.state)
    return await _issue(op, run, _requested(run.state, items), rk, request)


async def _issue(op: _Op, run: Projection, wanted: list[dict], rk: str, request: str) -> dict:
    """Consume ``wanted`` from stock and move the value that left onto the run's work in
    progress. Every check runs under the locks before anything is written."""
    state = run.state
    remaining = {i["item_id"]: i["quantity"] for i in outstanding_inputs(state)}
    planned = {i.get("item_id") for i in state.get("inputs", [])}
    for line in wanted:
        if line["item_id"] not in planned:
            raise refuse(422, "not_an_input", f"{line['item_id']} is not a component of this run.",
                         item=line["item_id"])
        if line["quantity"] > remaining.get(line["item_id"], 0.0) + _EPS:
            raise refuse(409, "over_issue",
                         f"Only {remaining.get(line['item_id'], 0.0):g} of {line['item_id']} is still to be "
                         "issued to this run.", item=line["item_id"], remaining=remaining.get(line["item_id"], 0.0))
    if not wanted:
        return {"issued": [], "value": "0"}

    from celerp_inventory.projections import is_item_available

    rows = await lock_projections(op.session, op.company_id, [i["item_id"] for i in wanted])
    befores: dict[str, Decimal] = {}
    for line in wanted:
        item_id = line["item_id"]
        row = rows.get(item_id)
        if row is None or row.entity_type != "item":
            raise refuse(404, "item_missing", f"Component {item_id} was not found.", item=item_id)
        s = row.state or {}
        sku = s.get("sku") or item_id
        if str(s.get("status") or "").lower() == "draft":
            raise refuse(422, "item_draft", f"{sku} is a draft. Make it available before issuing it.", sku=sku)
        require_stock(s, item_id, 409)
        held = held_value(row)
        if not is_item_available(s) or s.get("status_doc_id") or held is None:
            raise refuse(409, "item_unavailable",
                         f"{sku} is not stock the company holds and can use (it is {s.get('status') or 'unknown'}).",
                         sku=sku, status=s.get("status"))
        free = float(s.get("quantity") or 0) - float(s.get("reserved_quantity") or 0)
        if free + _EPS < line["quantity"]:
            raise refuse(409, "insufficient_stock",
                         f"Only {max(free, 0.0):g} of {sku} is in stock and not reserved; {line['quantity']:g} is needed.",
                         sku=sku, available=max(free, 0.0), needed=line["quantity"])
        befores[item_id] = op.round(held)
        if op.books and befores[item_id]:
            lot_account(s)  # stock whose inventory account is not known cannot move

    wip_code = state.get("wip_account_code")
    if op.books and any(befores.values()):
        wip_code = (await continue_role(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS, wip_code)
                    if wip_code else await _wip_target(op))

    credits: dict[str, Decimal] = {}
    total = _ZERO
    issued = []
    for line in wanted:
        item_id = line["item_id"]
        await op.emit(item_id, "item", "item.consumed", {"quantity_consumed": line["quantity"]},
                      f"mfg:{op.order_id}:issue:{rk}:{item_id}",
                      metadata={_ORDER_MARK: op.order_id})
        after = await op.session.get(Projection, {"company_id": op.company_id, "entity_id": item_id},
                                     populate_existing=True)
        moved = befores[item_id] - op.round(held_value(after) or 0)
        if moved and op.books:
            code = lot_account(after.state or {})
            credits[code] = credits.get(code, _ZERO) - moved
        total += moved
        # Each component's own value, so a return gives back exactly what it took.
        issued.append({**line, "value": str(moved)})
    await op.post(f"issue:{rk}", f"Components issued to production run {op.order_id}", wip_code, total, credits)
    data = {"items": issued, "issued_by": str(op.user_id), "value": str(total), "request": request}
    if wip_code and total:
        data["wip_account_code"] = wip_code
    await op.emit_run("mfg.order.issued", data, f"mfg:{op.order_id}:issue:{rk}")
    return {"issued": issued, "value": str(total)}


# ---------------------------------------------------------------------------
# Return
# ---------------------------------------------------------------------------

def _to_return(run_state: dict, items: list[dict] | None) -> list[dict]:
    """The components to return: the request with each item once, or everything issued."""
    if not items:
        return [{"item_id": i.get("item_id"), "quantity": round(float(i.get("issued_qty") or 0), 6)}
                for i in run_state.get("inputs", []) if float(i.get("issued_qty") or 0) > _EPS]
    for line in items:
        if not line.get("item_id") or float(line.get("quantity") or 0) <= 0:
            raise refuse(422, "return_quantity", "Each component returned needs a quantity greater than zero.")
    return [{"item_id": i["item_id"], "quantity": round(i["quantity"], 6)} for i in merge_inputs(items)]


async def return_materials(session: AsyncSession, company_id, user_id, order_id: str, items: list[dict] | None,
                           key: str | None, *, at: str) -> dict:
    """Return issued components to the lots they came from: the ``items`` given, or everything
    issued. The undo of Issue, at the value the run recorded when they were issued."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"items": items or None})
    stored = await _replayed(op, f"mfg:{order_id}:return:{rk}", request)
    if stored is not None:
        return {"returned": stored.data.get("items") or [], "value": stored.data.get("value")}
    run = await _run(op)
    _require_open(run.state, "returned from")
    _require_settled(op, run.state)
    return await _return(op, run, _to_return(run.state, items), rk, request)


async def _return(op: _Op, run: Projection, wanted: list[dict], rk: str, request: str) -> dict:
    """Put ``wanted`` back on its lots and move the value each took when it was issued off the
    run's work in progress onto the lot's inventory account. Every check runs under the locks
    before anything is written."""
    state = run.state
    inputs = {i.get("item_id"): i for i in state.get("inputs", [])}
    for line in wanted:
        inp = inputs.get(line["item_id"])
        if inp is None:
            raise refuse(422, "not_an_input", f"{line['item_id']} is not a component of this run.",
                         item=line["item_id"])
        have = float(inp.get("issued_qty") or 0)
        if line["quantity"] > have + _EPS:
            lot = await op.session.get(Projection, {"company_id": op.company_id, "entity_id": line["item_id"]})
            sku = ((lot.state or {}).get("sku") if lot is not None else None) or line["item_id"]
            raise refuse(409, "over_return", f"Only {have:g} of {sku} was issued to this run, so no more can be "
                         "returned.", sku=sku, issued=have)
    if not wanted:
        return {"returned": [], "value": "0"}
    if float(state.get("received_qty") or 0) > _EPS or state.get("receipts"):
        # Every receipt took a share of every component, so what is left is no longer the
        # components alone.
        raise refuse(409, "return_after_receipt",
                     "Output was already received from this run, so its materials cannot be returned. "
                     "Undo the receipts first.")
    values: dict[str, Decimal] = {}
    for line in wanted:
        inp = inputs[line["item_id"]]
        if "issued_value" not in inp:
            raise _reconcile()
        have, recorded = float(inp.get("issued_qty") or 0), _money(inp["issued_value"])
        values[line["item_id"]] = (recorded if line["quantity"] >= have - _EPS
                                   else op.round(recorded * _money(line["quantity"]) / _money(have)))

    from celerp_inventory.projections import is_item_available

    rows = await lock_projections(op.session, op.company_id, list(values))
    befores: dict[str, Decimal] = {}
    for line in wanted:
        item_id = line["item_id"]
        row = rows.get(item_id)
        s = (row.state or {}) if row is not None and row.entity_type == "item" else {}
        held = held_value(row) if s else None
        if not is_item_available(s) or s.get("status_doc_id") or held is None:
            raise refuse(409, "return_lot_unavailable",
                         f"{s.get('sku') or item_id} is no longer stock the company holds (it is "
                         f"{s.get('status') or 'gone'}), so nothing can be returned to it.",
                         sku=s.get("sku") or item_id, status=s.get("status"))
        befores[item_id] = op.round(held)
        if op.books and values[item_id]:
            lot_account(s)

    total = sum(values.values(), _ZERO)
    wip_code = state.get("wip_account_code")
    if op.books and total:
        wip_code = await continue_role(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS, wip_code)

    debits: dict[str, Decimal] = {}
    returned = []
    for line in wanted:
        item_id, value = line["item_id"], values[line["item_id"]]
        s = rows[item_id].state or {}
        qty = float(s.get("quantity") or 0) + line["quantity"]
        landed = sum(float(v or 0) for v in (s.get("landed_contributions") or {}).values())
        target = befores[item_id] + value
        await op.emit(item_id, "item", "item.quantity.adjusted", {
            "new_qty": qty, "cost_base": float(target) - landed * qty, "reason": "production_return",
            "quantity_returned": line["quantity"]},
            f"mfg:{op.order_id}:return:{rk}:{item_id}", metadata={_ORDER_MARK: op.order_id})
        after = await op.session.get(Projection, {"company_id": op.company_id, "entity_id": item_id},
                                     populate_existing=True)
        if op.round(held_value(after) or 0) != target:
            raise refuse(409, "return_value", f"{s.get('sku') or item_id} cannot take back exactly the value "
                         "it was issued at, so nothing was returned.", sku=s.get("sku") or item_id)
        if value and op.books:
            code = lot_account(after.state or {})
            debits[code] = debits.get(code, _ZERO) + value
        returned.append({**line, "value": str(value)})
    await op.post(f"return:{rk}", f"Components returned from production run {op.order_id}", wip_code, -total,
                  debits)
    await op.emit_run("mfg.order.returned", {
        "items": returned, "returned_by": str(op.user_id), "value": str(total), "request": request},
        f"mfg:{op.order_id}:return:{rk}")
    return {"returned": returned, "value": str(total)}


# ---------------------------------------------------------------------------
# Receive
# ---------------------------------------------------------------------------

async def receive(session: AsyncSession, company_id, user_id, order_id: str, quantity: float | None,
                  key: str | None, *, at: str) -> dict:
    """Receive finished goods as a new lot: ``quantity``, or everything still outstanding. A run
    whose output is fully received completes."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"quantity": quantity})
    stored = await _replayed(op, f"mfg:{order_id}:receive:{rk}:received", request)
    if stored is not None:
        return {"received": stored.data.get("quantity"), "lot_item_id": stored.data.get("lot_item_id")}
    run = await _run(op)
    _require_open(run.state, "received into")
    await _require_executable_shape(op, run.state)
    _require_settled(op, run.state)
    qty = outstanding_output(run.state) if quantity is None else float(quantity)
    lot_id = await _receive(op, run, qty, rk, request)
    run = await _run(op)
    if outstanding_output(run.state) <= _EPS:
        await _close(op, run, {}, request, f"receive:{rk}")
    return {"received": qty, "lot_item_id": lot_id}


async def _receive(op: _Op, run: Projection, qty: float, rk: str, request: str) -> str:
    """Restock ``qty`` of the run's output as a new lot carrying its share of the run's work in
    progress: the share of the quantity still to come, so receiving 3 then 2 moves what
    receiving 5 would, and the last receipt takes everything left."""
    state = run.state
    outstanding = outstanding_output(state)
    if qty <= 0:
        raise refuse(422, "receive_quantity", "The quantity received must be greater than zero.")
    if qty > outstanding + _EPS:
        raise refuse(409, "over_receipt",
                     f"Only {outstanding:g} is still to be received from this run; {qty:g} cannot be received.",
                     remaining=outstanding, quantity=qty)
    if outstanding_inputs(state):
        raise refuse(409, "issue_first", "Issue every component to this run before receiving its output.")
    out_id = state.get("output_item_id")
    product = (await lock_projections(op.session, op.company_id, [out_id])).get(out_id) if out_id else None
    if product is None or product.entity_type != "item":
        raise refuse(409, "no_output", "This run has no product to receive its output into.")
    p = product.state or {}
    if str(p.get("status") or "").lower() == "draft":
        raise refuse(422, "output_draft", f"{p.get('sku') or out_id} is a draft. Make it available first.",
                     sku=p.get("sku") or out_id)
    require_stock(p, out_id, 409)

    wip = _wip(state)
    amount = wip if qty >= outstanding - _EPS else op.round(wip * _money(qty) / _money(outstanding))
    wip_code = state.get("wip_account_code")
    if op.books:
        code = await resolve(op.session, op.company_id, AccountRole.INVENTORY_PURCHASED)
        if amount:
            wip_code = await continue_role(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS, wip_code)
    else:
        code = await new_lot_account(op.session, op.company_id, AccountRole.INVENTORY_PURCHASED)

    from celerp_inventory.services import allocate_internal_codes

    lot_id = f"item:{uuid.uuid5(MFG_LOT_NS, f'{op.order_id}:{rk}')}"
    loc = p.get("location_id")
    mark = {_ORDER_MARK: op.order_id}
    await op.emit(lot_id, "item", "item.created", {
        "sku": p.get("sku"), "name": p.get("name"), "sell_by": p.get("sell_by"),
        "category": p.get("category"), "inventory_type": p.get("inventory_type"),
        "allow_splitting": splitting_allowed(p), "quantity": 0, "location_id": loc,
        "parent_item_id": out_id, "lot": True,
        # A produced lot is a new physical parcel with its own barcode.
        "barcode": (await allocate_internal_codes(op.session, op.company_id))[0],
        _ORDER_MARK: op.order_id, "cost_total": float(amount), LOT_ACCOUNT_FIELD: code,
    }, f"mfg:{op.order_id}:receive:{rk}:created", location_id=loc, metadata=mark)
    await op.emit(lot_id, "item", "item.produced", {"quantity_produced": qty},
                  f"mfg:{op.order_id}:receive:{rk}:produced", location_id=loc, metadata=mark)
    await op.post(f"receive:{rk}", f"Output received from production run {op.order_id}", wip_code, -amount,
                  {code: amount} if code else {})
    await op.emit_run("mfg.order.received", {
        "quantity": qty, "lot_item_id": lot_id, "received_by": str(op.user_id), "value": str(amount),
        "request": request}, f"mfg:{op.order_id}:receive:{rk}:received")
    return lot_id


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------

async def complete(session: AsyncSession, company_id, user_id, order_id: str, payload: dict,
                   key: str | None, *, at: str, quantity: float | None = None) -> dict:
    """Finish a run: issue what is outstanding, receive the output still to come (``quantity``
    of it when given, else all of it) and close. ``payload`` holds the closing details
    (waste_items, or the waste_quantity shorthand with waste_unit; waste_reason, labor_hours).
    What it made is what it received."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"payload": _closing_request(payload), "quantity": quantity})
    if await _replayed(op, f"mfg:{order_id}:complete:{rk}", request) is not None:
        return {"status": "completed"}
    run = await _run(op)
    _require_open(run.state, "completed")
    await _require_executable_shape(op, run.state)
    _require_settled(op, run.state)
    wasted, names = await _waste_request(op, run.state, payload)
    # Completing issues what is outstanding, so the waste is checked against that before anything moves.
    check_waste({**run.state, "inputs": [{**i, "issued_qty": max(float(i.get("issued_qty") or 0),
                                                                  float(i.get("quantity") or 0))}
                                         for i in run.state.get("inputs", [])]}, wasted, names)
    outstanding = outstanding_inputs(run.state)
    if outstanding:
        if (await mfg_settings(session, company_id)).get("require_issued_before_complete"):
            raise refuse(409, "issue_required",
                         "Issue all components before completing this run (required by your manufacturing settings).")
        await _issue(op, run, outstanding, f"{rk}:issue", request)
        run = await _run(op)
    qty = outstanding_output(run.state) if quantity is None else float(quantity)
    if qty > _EPS:
        await _receive(op, run, qty, f"{rk}:receive", request)
        run = await _run(op)
    await _close(op, run, payload, request, rk, wasted, names)
    return {"status": "completed"}


def merge_waste(items: list[dict] | None) -> list[dict]:
    """Each wasted component once, its quantities summed, in component order."""
    merged: dict[str, Decimal] = {}
    for line in items or []:
        merged[line["item_id"]] = merged.get(line["item_id"], _ZERO) + _money(line["quantity"])
    return [{"item_id": i, "quantity": float(q)} for i, q in sorted(merged.items())]


def _closing_request(payload: dict) -> dict:
    """The closing details as one request: the same waste listed in another order, or split over
    repeated lines, is the same request."""
    out = {k: v for k, v in payload.items() if k != "waste_items"}
    if payload.get("waste_items"):
        out["waste_items"] = merge_waste(payload["waste_items"])
    return out


async def _waste_request(op: _Op, state: dict, payload: dict) -> tuple[list[dict], dict[str, str]]:
    """The components a completion wastes, and every component's name for refusals. The
    waste_quantity shorthand names no component, so it is only taken on a run with one, and its
    unit must be that component's unit."""
    names: dict[str, str] = {}
    units: dict[str, str | None] = {}
    for inp in state.get("inputs", []):
        row = await op.session.get(Projection, {"company_id": op.company_id, "entity_id": inp.get("item_id")})
        s = (row.state or {}) if row is not None else {}
        names[inp.get("item_id")] = s.get("sku") or inp.get("item_id")
        units[inp.get("item_id")] = s.get("sell_by") or s.get("unit")
    items, qty = payload.get("waste_items"), payload.get("waste_quantity")
    if items and qty is not None:
        raise refuse(422, "waste_twice", "Give the waste either per component or as one quantity, not both.")
    if items:
        return merge_waste(items), names
    if not qty:
        return [], names
    if len(names) != 1:
        components = sorted(names.values())
        raise refuse(422, "waste_ambiguous",
                     f"This run uses {len(components)} components ({', '.join(components)}), so the waste "
                     "must say how much of each was wasted.", components=components)
    (item_id, unit), = units.items()
    given = payload.get("waste_unit")
    if given and unit and given.strip().lower() != unit.strip().lower():
        raise refuse(422, "waste_unit", f"{names[item_id]} is counted in {unit}, not {given}.",
                     sku=names[item_id], unit=unit, given=given)
    return [{"item_id": item_id, "quantity": float(qty)}], names


def check_waste(run_state: dict, waste_items: list[dict], names: dict[str, str] | None = None) -> list[dict]:
    """The waste, each component once in component order, after checking every component is one
    of the run's and none is wasted beyond what was issued to it."""
    inputs = {i.get("item_id"): i for i in run_state.get("inputs", [])}
    names = names or {}
    wasted = merge_waste(waste_items)
    for line in wasted:
        item_id = line["item_id"]
        if item_id not in inputs:
            raise refuse(422, "not_an_input", f"{item_id} is not a component of this run.", item=item_id)
        have, sku = float(inputs[item_id].get("issued_qty") or 0), names.get(item_id) or item_id
        if line["quantity"] > have + _EPS:
            raise refuse(422, "over_waste", f"Only {have:g} of {sku} was issued to this run, so no more "
                         "can be wasted.", sku=sku, issued=have)
    return wasted


def value_waste(run_state: dict, waste_items: list[dict], currency: str,
                names: dict[str, str] | None = None) -> tuple[list[dict], Decimal]:
    """What the waste cost: each component wasted at its share of the value it carried into the
    run when it was issued, all of it when everything issued was wasted. Returns one line per
    component (in component order, with its value) and the total. A component whose issued
    value is not known needs the run reconciled first."""
    inputs = {i.get("item_id"): i for i in run_state.get("inputs", [])}
    lines, total = [], _ZERO
    for line in check_waste(run_state, waste_items, names):
        item_id, qty = line["item_id"], line["quantity"]
        inp = inputs[item_id]
        if inp.get("issued_value") is None:
            raise _reconcile()
        have, recorded = float(inp.get("issued_qty") or 0), _money(inp["issued_value"])
        value = recorded if qty >= have - _EPS else round_money(recorded * _money(qty) / _money(have), currency)
        lines.append({"item_id": item_id, "quantity": qty, "value": str(value)})
        total += value
    return lines, total


async def _close(op: _Op, run: Projection, payload: dict, request: str, ck: str,
                 wasted: list[dict] | None = None, names: dict[str, str] | None = None) -> None:
    """Close a run, leaving it holding nothing: waste to cost of goods sold, and the rest
    shared over its lots by quantity, each lot restated by the difference from what it took
    when it was received. ``ck`` keys this completion, so a reopened run completes afresh.
    The completion records what it moved, so reopening reverses exactly that."""
    state = run.state
    issued, held = _money(state.get("wip_issued")), _wip(state)
    # Waste is what the components wasted carried in, whether or not the output was already
    # received: the lots then give back what they took for it.
    waste_lines, waste = value_waste(state, wasted or [], op.currency, names)
    receipts = [r for r in state.get("receipts") or [] if float(r.get("quantity") or 0) > 0]
    # Output an older release took off along a path no re-cost can follow keeps what it was
    # given (reconcile); the finished value it does not carry is shared over the rest.
    kept = sum((_money(r.get("value")) for r in receipts if r.get("fixed")), _ZERO)
    live = [r for r in receipts if not r.get("fixed")]
    if waste > issued - kept:
        raise refuse(422, "waste_kept", "Output an older release received from this run keeps its cost, "
                     "so the rest of what was issued is all that can be recorded as waste.",
                     wastable=str(issued - kept))
    if receipts and not live:
        # Every lot keeps its cost: what the run holds beyond it has no output left to go to.
        waste = issued - kept
    finished = issued - waste
    if finished and not receipts:
        raise refuse(409, "unaccounted_value",
                     "Nothing was received from this run, so the materials issued to it must be recorded "
                     "as waste before it can be completed.")
    shares = allocate_pro_rata(finished - kept, [_money(r["quantity"]) for r in live], op.currency) if live else []

    debits: dict[str, Decimal] = {}
    restated = []
    for receipt, share in zip(live, shares):
        delta = share - _money(receipt.get("value"))
        if not delta:
            continue
        lot_id = receipt["lot_item_id"]
        code = await _restate(op, lot_id, delta, f"mfg:{op.order_id}:complete:{ck}:recost:{lot_id}", "completed")
        restated.append({"lot_item_id": lot_id, "delta": str(delta)})
        if code:
            debits[code] = debits.get(code, _ZERO) + delta

    wip_code = state.get("wip_account_code")
    if op.books and held:
        wip_code = await continue_role(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS, wip_code)
    await op.post(f"complete:{ck}", f"Production run {op.order_id} completed", wip_code, -held, debits, waste)

    expected = (state.get("expected_outputs") or [{}])[0]
    actual_outputs = [{**expected, "quantity": float(state.get("received_qty") or 0)}] if expected else []
    await op.emit_run("mfg.order.completed", {
        "completed_by": str(op.user_id), "actual_outputs": actual_outputs,
        "waste": ({"items": waste_lines, "unit": payload.get("waste_unit"),
                   "reason": payload.get("waste_reason")} if waste_lines else None),
        "labor_hours": payload.get("labor_hours"),
        "transferred": str(finished), "wasted": str(waste), "request": request,
        "closing": {"held": str(held), "wasted": str(waste), "lots": restated, "booked": op.books},
    }, f"mfg:{op.order_id}:complete:{ck}")


async def _restate(op: _Op, lot_id: str, delta: Decimal, key: str, action: str) -> str | None:
    """Change the cost of a lot this run received by ``delta``, wherever its value is now
    (_lineage_end), and return the account that carries the change (None with Accounting off).
    The run books its side: this only moves the lot's cost."""
    from celerp_inventory.services import CostRestatementConflict, goods_basis, restate_item_cost

    end = await _lineage_end(op, lot_id)
    lot = await op.session.get(Projection, {"company_id": op.company_id, "entity_id": lot_id})
    try:
        await restate_item_cost(
            op.session, op.company_id, lot_id, event_type="item.cost_adjusted",
            data={"cost_total": float(_money(goods_basis(lot.state or {})) + delta), _ORDER_MARK: op.order_id},
            actor_id=op.user_id, source="api", idempotency_key=key, day=op.day)
    except CostRestatementConflict as exc:
        raise refuse(409, "recost_conflict", f"This run cannot be {action}: {exc}.", reason=str(exc)) from exc
    return lot_account(end.state or {}) if op.books else None


async def _lineage_end(op: _Op, lot_id: str) -> Projection:
    """Where a received lot's value is now: the lot, or the lot it was merged into, followed to
    the end. Its account takes the lot's final cost, so that end must still hold the stock or
    have sold it."""
    seen: set[str] = set()
    current = lot_id
    while True:
        row = await op.session.get(Projection, {"company_id": op.company_id, "entity_id": current})
        if row is None or row.entity_type != "item" or current in seen:
            raise refuse(409, "output_gone", "A lot received from this run can no longer be found, so its final "
                         "cost cannot be recorded.", lot=current)
        seen.add(current)
        s = row.state or {}
        status = str(s.get("status") or "").lower()
        if status == "merged" and s.get("merged_into"):
            current = s["merged_into"]
            continue
        if status == "sold" or held_value(row) is not None:
            return row
        raise refuse(409, "output_gone",
                     f"{s.get('sku') or current}, received from this run, is {status or 'no longer held'}, so its "
                     "final cost cannot be recorded.", lot=s.get("sku") or current, status=status)


async def _keeps_cost(session: AsyncSession, row: Projection) -> bool:
    """Whether a lot on a received lot's lineage can still take its final cost: it holds the
    stock, sold it whole or was merged whole (_lineage_end), and all of its cost is still
    where the restater can follow it."""
    from celerp_inventory.services import cost_can_be_restated

    s = row.state or {}
    status = str(s.get("status") or "").lower()
    return ((status in ("sold", "merged") or held_value(row) is not None)
            and await cost_can_be_restated(session, row.company_id, row.entity_id, s))


async def _cost_reachable(session: AsyncSession, company_id, lot_id: str) -> bool:
    """Whether completion can still record a received lot's final cost: the lot and every lot
    its value was merged into keep it (_keeps_cost)."""
    seen: set[str] = set()
    current = lot_id
    while current not in seen:
        seen.add(current)
        row = await session.get(Projection, {"company_id": company_id, "entity_id": current})
        if row is None or row.entity_type != "item" or not await _keeps_cost(session, row):
            return False
        s = row.state or {}
        if str(s.get("status") or "").lower() != "merged":
            return True
        current = s.get("merged_into")
    return False


async def _pending_output(session: AsyncSession, company_id) -> dict[str, str]:
    """Every lot holding output of a run still open whose final cost completion is still to
    record, and every lot it was merged into, each with that run."""
    pending: dict[str, str] = {}
    for run in await _open_runs(session, company_id):
        state = run.state or {}
        kept = {r.get("lot_item_id") for r in state.get("receipts") or [] if r.get("fixed")}
        for lot in state.get("received_lots") or []:
            current = None if lot in kept else lot
            while current and current not in pending:
                pending[current] = run.entity_id
                row = await session.get(Projection, {"company_id": company_id, "entity_id": current})
                s = (row.state or {}) if row is not None else {}
                current = s.get("merged_into") if str(s.get("status") or "").lower() == "merged" else None
    return pending


def _may_move_cost(event_type: str, before: dict, after: dict) -> bool:
    """Whether an item event can take cost off a lot or give up the stock: everything else
    leaves the lot holding all of it."""
    return (event_type in ("item.split", "item.transform", "item.consumed")
            or str(before.get("status") or "") != str(after.get("status") or "")
            or float(after.get("quantity") or 0) < float(before.get("quantity") or 0)
            or before.get("children") != after.get("children")
            or before.get("transformed_into") != after.get("transformed_into"))


async def guard_output_lineage(*, session: AsyncSession, entry: LedgerEntry, transition) -> None:
    """item_lineage_guard: output of a run still open takes its final cost when the run
    completes, so until then it may only go where that cost can still reach it (held, sold
    whole on an invoice line, or merged). Anything else done to the lot or a lot it was merged
    into (a split, a partial sale, a sale no invoice line shipped, a transform, use in another
    run, a count lowering it, a write-off) is refused, as is a new lot taking its cost from such
    a lot (goods back on a credit note). The run's own events are its business: they carry its mark."""
    before, after = transition.before, transition.after or {}
    if before is None:
        source = (entry.metadata_ or {}).get(VALUED_FROM_KEY)
        order = (await _pending_output(session, entry.company_id)).get(source) if source else None
        if order is not None:
            sku = after.get("sku") or source
            raise refuse(409, "output_cost_pending",
                         f"{sku} comes back at the cost of stock from production run {order}, which is still open, "
                         "so that cost is not final yet. Complete the run before receiving this return.",
                         sku=sku, order=order)
        return
    if not _may_move_cost(entry.event_type, before, after):
        return
    order = (await _pending_output(session, entry.company_id)).get(entry.entity_id)
    if order is None or order in ((entry.data or {}).get(_ORDER_MARK), (entry.metadata_ or {}).get(_ORDER_MARK)):
        return
    row = await session.get(Projection, {"company_id": entry.company_id, "entity_id": entry.entity_id})
    if await _keeps_cost(session, row):
        return
    sku = after.get("sku") or entry.entity_id
    if (str(before.get("status") or ""), str(after.get("status") or "")) == ("memo_out", "sold"):
        # Billed from out on memo, the sale has no invoice line a later cost can adjust.
        raise refuse(409, "output_memo_conversion",
                     "Complete this production run before converting this memo to an invoice. Its "
                     "finished-goods cost is not final yet.", sku=sku, order=order)
    raise refuse(409, "output_in_production",
                 f"{sku} came from production run {order}, which is still open, and its cost is final only when "
                 "the run is completed. Complete the run before splitting, transforming, using, counting down "
                 "or writing off this stock; until then it can be sold whole or merged.",
                 sku=sku, order=order)


# ---------------------------------------------------------------------------
# Undo a receipt, reopen a completed run
# ---------------------------------------------------------------------------

async def _require_untouched(op: _Op, row: Projection | None, lot_id: str, quantity: float, value: Decimal) -> None:
    """A lot this run produced, exactly as the run left it: holding ``quantity`` at ``value``,
    free, and changed by nothing but this run. Anything else (a sale, a move, a split, an
    adjustment, a reservation, a document) has made it something the run can no longer take back."""
    from celerp_inventory.projections import is_item_available

    s = (row.state or {}) if row is not None and row.entity_type == "item" else {}
    # Recording which account carries the lot (as turning Accounting on does) changes neither
    # its stock nor its value.
    marks = (await op.session.execute(select(LedgerEntry.data, LedgerEntry.metadata_).where(
        LedgerEntry.company_id == op.company_id, LedgerEntry.entity_id == lot_id,
        LedgerEntry.event_type != RECORDED))).all()
    held = held_value(row) if s else None
    if (not s or await listing_record(op.session, op.company_id, lot_id) is not None or not marks or any(op.order_id not in ((d or {}).get(_ORDER_MARK), (m or {}).get(_ORDER_MARK)) for d, m in marks)
            or abs(float(s.get("quantity") or 0) - quantity) > _EPS or float(s.get("reserved_quantity") or 0) > _EPS
            or s.get("status_doc_id") or not is_item_available(s) or held is None or op.round(held) != value):
        sku = s.get("sku") or lot_id
        raise refuse(409, "output_changed",
                     f"{sku} has changed since this run produced it (it was sold, moved, split, adjusted, "
                     "reserved or put on a document), so the run cannot take it back.", lot=sku)


async def undo_receipt(session: AsyncSession, company_id, user_id, order_id: str, lot_id: str, key: str | None,
                       *, at: str) -> dict:
    """Undo a receipt: the lot it made leaves stock and its value goes back to the run, as if
    it had never been received. Only while the lot is exactly as the run left it."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"lot_item_id": lot_id})
    stored = await _replayed(op, f"mfg:{order_id}:unreceive:{rk}", request)
    if stored is not None:
        return {k: stored.data.get(k) for k in ("lot_item_id", "quantity", "value")}
    run = await _run(op)
    state = run.state
    if state.get("status") == "completed":
        raise refuse(409, "reopen_first", "This run is completed. Reopen it before undoing a receipt.")
    _require_open(state, "changed")
    _require_settled(op, state)
    receipt = next((r for r in state.get("receipts") or [] if r.get("lot_item_id") == lot_id), None)
    if receipt is None:
        raise refuse(422, "not_a_receipt", "That lot was not received from this run.")
    qty, value = float(receipt["quantity"]), _money(receipt["value"])
    row = (await lock_projections(op.session, op.company_id, [lot_id])).get(lot_id)
    await _require_untouched(op, row, lot_id, qty, value)
    code = lot_account(row.state or {}) if op.books and value else None
    wip_code = state.get("wip_account_code")
    if op.books and value:
        wip_code = await continue_role(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS, wip_code)

    mark = {_ORDER_MARK: op.order_id}
    loc = (row.state or {}).get("location_id")
    # Emptied first, then archived: an archived lot brought back holds nothing.
    await op.emit(lot_id, "item", "item.quantity.adjusted",
                  {"new_qty": 0.0, "cost_base": 0.0, "reason": "production_receipt_undone"},
                  f"mfg:{order_id}:unreceive:{rk}:emptied", location_id=loc, metadata=mark)
    await op.emit(lot_id, "item", "item.status.set", {"new_status": "archived", "reason": "production_receipt_undone"},
                  f"mfg:{order_id}:unreceive:{rk}:archived", location_id=loc, metadata=mark)
    await op.post(f"unreceive:{rk}", f"Output receipt undone on production run {order_id}", wip_code, value,
                  {code: -value} if code else {})
    data = {"lot_item_id": lot_id, "quantity": qty, "value": str(value)}
    await op.emit_run("mfg.order.receipt_undone", {**data, "undone_by": str(op.user_id), "request": request},
                      f"mfg:{order_id}:unreceive:{rk}")
    return data


async def reopen(session: AsyncSession, company_id, user_id, order_id: str, key: str | None, *, at: str) -> dict:
    """Reopen a completed run: completion's own entries (the lots' final cost and the waste) are
    reversed from the values completion recorded, and the run holds what it held before.
    Only while every lot it produced is exactly as completion left it."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({})
    if await _replayed(op, f"mfg:{order_id}:reopen:{rk}", request) is not None:
        return {"status": "reopened"}
    run = await _run(op)
    state = run.state
    if state.get("status") != "completed":
        raise refuse(409, "not_completed", "Only a completed run can be reopened.")
    closing = state.get("closing")
    # A run completed by an older release, or with Accounting in another state, recorded nothing
    # this can reverse exactly.
    if closing is None or bool(closing.get("booked")) != op.books:
        raise _reconcile()
    _require_settled(op, state)
    deltas = {lot["lot_item_id"]: _money(lot["delta"]) for lot in closing.get("lots") or []}
    # Completion re-costed only the lots whose cost it could still reach; a lot that keeps what
    # it was given (reconcile) is none of its business.
    receipts = [r for r in state.get("receipts") or [] if float(r.get("quantity") or 0) > 0 and not r.get("fixed")]
    rows = await lock_projections(op.session, op.company_id, [r["lot_item_id"] for r in receipts])
    for r in receipts:
        lot_id = r["lot_item_id"]
        await _require_untouched(op, rows.get(lot_id), lot_id, float(r["quantity"]),
                                 _money(r["value"]) + deltas.get(lot_id, _ZERO))

    credits: dict[str, Decimal] = {}
    for lot_id, delta in deltas.items():
        code = await _restate(op, lot_id, -delta, f"mfg:{order_id}:reopen:{rk}:recost:{lot_id}", "reopened")
        if code:
            credits[code] = credits.get(code, _ZERO) - delta
    held, waste = _money(closing.get("held")), _money(closing.get("wasted"))
    wip_code = state.get("wip_account_code")
    if op.books and held:
        wip_code = await continue_role(op.session, op.company_id, AccountRole.WORK_IN_PROGRESS, wip_code)
    await op.post(f"reopen:{rk}", f"Production run {order_id} reopened", wip_code, held, credits, -waste)
    await op.emit_run("mfg.order.reopened", {"reopened_by": str(op.user_id), "request": request},
                      f"mfg:{order_id}:reopen:{rk}")
    return {"status": "reopened"}


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Start, hold, resume, reschedule
# ---------------------------------------------------------------------------

_TRANSITIONS = {"start": ("mfg.order.started", "started"), "hold": ("mfg.order.on_hold", "put on hold"),
                "resume": ("mfg.order.resumed", "resumed"), "schedule": ("mfg.order.scheduled", "rescheduled")}


async def transition(session: AsyncSession, company_id, user_id, order_id: str, action: str, data: dict,
                     key: str | None, *, at: str):
    """Start, hold, resume or reschedule a run, serialized with every movement on it: the run
    is read under the company lock, so a run completed or cancelled meanwhile is refused rather
    than brought back. Completed and cancelled runs stay closed; a completed run leaves only
    through reopen. Only a planned run starts, a run on hold is not held again, and only a
    run on hold resumes."""
    event_type, done = _TRANSITIONS[action]
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"action": action, **data})
    stored = await _replayed(op, f"mfg:{order_id}:{action}:{rk}", request)
    if stored is not None:
        return stored
    run = await _run(op)
    _require_open(run.state, done)
    status = run.state.get("status")
    if action == "start" and status != "planned":
        raise refuse(409, "not_planned", "Only a planned run can be started. A run on hold carries on through Resume.")
    if action == "hold" and status == "on_hold":
        raise refuse(409, "already_on_hold", "This run is already on hold.")
    if action == "resume" and status != "on_hold":
        raise refuse(409, "not_on_hold", "Only a run on hold can be resumed.")
    return await op.emit_run(event_type, {**data, "request": request}, f"mfg:{order_id}:{action}:{rk}")


async def cancel(session: AsyncSession, company_id, user_id, order_id: str, reason: str | None,
                 key: str | None, *, at: str):
    """Cancel a run that holds nothing. A run still holding issued materials or received output
    holds their value, which cancelling would lose: they are returned and undone first."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"reason": reason})
    stored = await _replayed(op, f"mfg:{order_id}:cancel:{rk}", request)
    if stored is not None:
        return stored
    run = await _run(op)
    _require_open(run.state, "cancelled")
    # Its materials cannot be returned until it is reconciled, so that comes first.
    _require_settled(op, run.state)
    if float(run.state.get("received_qty") or 0) > _EPS or _wip(run.state) or any(
            float(i.get("issued_qty") or 0) > _EPS for i in run.state.get("inputs", [])):
        raise refuse(409, "cancel_moved",
                     "This run still holds materials or output, so it cannot be cancelled. Return its "
                     "materials and undo its receipts first.")
    data = {"request": request}
    if reason:
        data["reason"] = reason
    return await op.emit_run("mfg.order.cancelled", data, f"mfg:{order_id}:cancel:{rk}")


# ---------------------------------------------------------------------------
# Runs whose work in progress the books do not hold yet
# ---------------------------------------------------------------------------

class _Retry(Exception):
    """Something the settlement needs is not there yet (a posting account, an open period):
    roll the company back and retry on a later start."""


async def _open_runs(session: AsyncSession, company_id) -> list[Projection]:
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "mfg_order"))).scalars()
    return sorted((r for r in rows if (r.state or {}).get("status") not in CLOSED_RUN_STATUSES),
                  key=lambda r: r.entity_id)


async def legacy_in_production(*, session: AsyncSession, company_id) -> Decimal:
    """inventory_in_production: the value components left the shelf with for runs an older
    release started and that are still open. Those releases booked it only on completion, so
    the inventory accounts still carry it (lot_origin.normalize_legacy_inventory_origins).
    Output they received from such a run is a lot of its own, carrying the cost it was given
    then: that much of the value is stock (on hand, or sold off the books), not production."""
    runs = {r.entity_id: r.state for r in await _open_runs(session, company_id) if (r.state or {}).get("wip_untracked")}
    values = await consumed_values(session, company_id, _ORDER_MARK, set(runs)) if runs else {}
    total = sum((v for per in values.values() for v in per.values()), _ZERO)
    for order, state in sorted(runs.items()):
        total -= sum((r["value"] for r in await legacy_output(session, company_id, order, state) or []), _ZERO)
    return total


async def settle_open_runs(session: AsyncSession, company_id) -> None:
    """Give every open run the work in progress its history proves, in one savepoint per
    company. Never priced from today's costs.

    A run an older release started records what it issued but not its value: that value is
    replayed from each component's own events (lot_origin.consumed_values). With Accounting
    on, the books carry it on the components' inventory accounts, so it moves onto the work in
    progress account when each of those accounts holds exactly that value beyond its stock on
    hand; when none holds any of it, the books never recognized it and it is opened against
    retained earnings, as opening stock is. A run that received output before value was
    tracked, a component with no inventory account, books from elsewhere, or accounts that
    hold anything else leave the run needing reconciliation: it refuses every movement
    (``_require_settled``) rather than move a guessed value.

    A run issued while Accounting was off holds value no account carries; when Accounting is
    on it is opened against retained earnings, unless the books came from elsewhere.

    Nothing runs before Accounting has placed the company's stock (it retries on the next
    start). A missing work in progress account or a locked period leaves the company as it
    was until a later start. Running it again changes nothing."""
    try:
        async with session.begin_nested():
            await _settle(session, company_id)
    except _Retry:
        pass


async def _settle(session: AsyncSession, company_id) -> None:
    from celerp.notifications import service as notification_service

    await lock_company(session, company_id)
    settings = await current_settings(session, company_id)
    books = SCHEMA_KEY in settings
    if books and INVENTORY_ORIGIN_KEY not in settings:
        return
    runs = await _open_runs(session, company_id)
    # A run the books disagreed on is weighed again: another run's value they held may since
    # have been settled, reconciled or repaired.
    older = [r for r in runs if r.state.get("wip_untracked") and r.state.get("wip_unresolved") in (None, _DISAGREE)]
    unbooked = [r for r in runs if books and not r.state.get("wip_untracked") and not r.state.get("wip_unresolved")
                and _wip(r.state) and not r.state.get("wip_account_code")]
    if not older and not unbooked:
        return
    rows = await lock_projections(session, company_id, [r.entity_id for r in older + unbooked])
    base = _Op(session=session, company_id=company_id, user_id=None, order_id="",
               day=await auto_je.entry_day(session, company_id), books=books,
               currency=str(settings.get("currency") or "USD").upper())
    native = not books or not await books_from_elsewhere(session, company_id, settings)

    values = await consumed_values(session, company_id, _ORDER_MARK, {r.entity_id for r in older})
    plans: dict[str, dict[str | None, Decimal]] = {}
    unresolved: dict[str, str] = {}
    for run in older:
        order = run.entity_id
        if float(run.state.get("received_qty") or 0) > 0 or run.state.get("received_lots"):
            unresolved[order] = "received before tracking"
            continue
        per: dict[str | None, Decimal] = {}
        for lot_id, value in sorted(values[order].items()):
            lot = await session.get(Projection, {"company_id": company_id, "entity_id": lot_id})
            code = ((lot.state or {}) if lot is not None else {}).get(LOT_ACCOUNT_FIELD) if books else None
            if books and value and not code:
                unresolved[order] = "component without an inventory account"
                break
            per[code] = per.get(code, _ZERO) + value
        else:
            plans[order] = per

    source = "lots"
    if books and plans:
        need: dict[str, Decimal] = {}
        for per in plans.values():
            for code, value in per.items():
                if value:
                    need[code] = need.get(code, _ZERO) + value
        room = {code: await account_room(session, company_id, code) for code in need}
        if any(room[code] != need[code] for code in need):
            source = "equity" if native and not any(room.values()) else ""
        if not source:
            unresolved.update(dict.fromkeys(plans, _DISAGREE))
            plans = {}
    if not native:
        unresolved.update(dict.fromkeys((r.entity_id for r in unbooked), "books from elsewhere"))
        unbooked = []

    writes = any(sum(per.values(), _ZERO) for per in plans.values()) or unbooked
    if books and writes and not await period_open(session, company_id, base.day):
        raise _Retry
    wip_code = None
    if books and writes:
        try:
            wip_code = await resolve(session, company_id, AccountRole.WORK_IN_PROGRESS)
        except HTTPException as exc:
            raise _Retry from exc

    for order, per in plans.items():
        op = dataclasses.replace(base, order_id=order)
        total = sum(per.values(), _ZERO)
        if source == "lots":
            await op.post("wip-opened", f"Materials already in production run {order} when Celerp began tracking "
                          "their value", wip_code, total, {code: -v for code, v in per.items() if code})
        else:
            await op.post("wip-opened", f"Materials in production run {order} when Accounting was turned on",
                          wip_code, total, {}, equity=-total)
        await op.emit_run("mfg.order.wip_opened", {
            "issued": str(total), "transferred": "0", "receipts": [],
            "components": [{"item_id": lot, "value": str(v)} for lot, v in sorted(values[order].items())],
            "wip_account_code": wip_code if books and total else None}, f"mfg:{order}:wip-opened")
    for run in unbooked:
        state = rows[run.entity_id].state
        op = dataclasses.replace(base, order_id=run.entity_id)
        held = _wip(state)
        await op.post("wip-booked", f"Materials in production run {run.entity_id} when Accounting was turned on",
                      wip_code, held, {}, equity=-held)
        await op.emit_run("mfg.order.wip_opened", {
            "issued": str(_money(state.get("wip_issued"))), "transferred": str(_money(state.get("wip_transferred"))),
            "receipts": list(state.get("receipts") or []), "wip_account_code": wip_code}, f"mfg:{run.entity_id}:wip-booked")
    for order, reason in sorted(unresolved.items()):
        if rows[order].state.get("wip_unresolved") == reason:
            continue  # already waiting for this, and the user already told
        await dataclasses.replace(base, order_id=order).emit_run(
            "mfg.order.wip_unresolved", {"reason": reason}, f"mfg:{order}:wip-unresolved:{reason}")
        await notification_service.create(
            session, company_id, category="manufacturing", title="Production run needs reconciling",
            body=(f"The value of the materials in production run {order} cannot be worked out from its history "
                  f"({reason}). It cannot issue, return, receive or complete until it is reconciled: "
                  "open it from here and record what its materials are worth."),
            action_url=f"/manufacturing/runs/{order}/reconcile", priority="high")
    booked = sorted([*(o for o, per in plans.items() if sum(per.values(), _ZERO)), *(r.entity_id for r in unbooked)])
    if books and booked:
        await notification_service.create(
            session, company_id, category="accounting", title="Materials in production recorded",
            body=(f"The materials in production run(s) {', '.join(booked)} now carry their value on work in "
                  f"progress account {wip_code}, in entries dated {base.day}."))


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

async def still_held(session: AsyncSession, company_id, order_id: str, state: dict) -> set[str]:
    """The components a run still holds, whose value reconciling it records: every input with
    an issued quantity, and every lot the run's history consumed."""
    issued = {i.get("item_id") for i in state.get("inputs", []) if float(i.get("issued_qty") or 0) > _EPS}
    return issued | set((await consumed_values(session, company_id, _ORDER_MARK, {order_id}))[order_id])


def _untracked_qty(state: dict) -> float:
    """What a run received before value was tracked: everything received but its valued receipts."""
    return float(state.get("received_qty") or 0) - sum(float(r.get("quantity") or 0)
                                                       for r in state.get("receipts") or [])


async def _untracked_lots(session: AsyncSession, company_id, order_id: str, state: dict) -> list[dict] | None:
    """Each lot an older release received from a run before value was tracked, with the
    quantity it was produced with and the cost it was given then, read from the lot's own
    events marked with the run. None when a lot's history does not show both."""
    tracked = {r.get("lot_item_id") for r in state.get("receipts") or []}
    lots = [lot for lot in state.get("received_lots") or [] if lot not in tracked]
    found = []
    for lot in lots:
        events = (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == lot,
            LedgerEntry.event_type.in_(("item.created", "item.produced"))).order_by(LedgerEntry.id))).scalars().all()
        mine = [e for e in events if order_id in ((e.data or {}).get(_ORDER_MARK), (e.metadata_ or {}).get(_ORDER_MARK))]
        created = [e for e in mine if e.event_type == "item.created"]
        quantity = sum(float((e.data or {}).get("quantity_produced") or 0) for e in mine if e.event_type == "item.produced")
        if len(created) != 1 or quantity <= _EPS:
            return None
        found.append({"lot_item_id": lot, "quantity": quantity, "value": _money(created[0].data.get("cost_total"))})
    return found


async def legacy_output(session: AsyncSession, company_id, order_id: str, state: dict) -> list[dict] | None:
    """The output an older release received from a run before value was tracked
    (_untracked_lots). None when that history does not account for everything the run received."""
    found = await _untracked_lots(session, company_id, order_id, state)
    if found is None or abs(sum(r["quantity"] for r in found) - _untracked_qty(state)) > _EPS:
        return None
    return found


# ---------------------------------------------------------------------------
# Repair an older run's output
# ---------------------------------------------------------------------------

async def repair_output(session: AsyncSession, company_id, user_id, order_id: str, output_item_id: str | None,
                        key: str | None, *, at: str) -> dict:
    """Put right what an older release recorded about a run's output: the quantity it marked
    received without making a lot for it is discarded (no lot is ever made from that record),
    and the product it makes is set when the run names none and the user chooses one. The
    run's value is then worked out again from its corrected history (settle_open_runs)."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"output_item_id": output_item_id})
    stored = await _replayed(op, f"mfg:{order_id}:repair:{rk}", request)
    if stored is not None:
        return {k: stored.data.get(k) for k in ("discarded", "output_item_id")}
    run = await _run(op)
    _require_open(run.state, "repaired")
    state = run.state
    lots = await _untracked_lots(session, company_id, order_id, state)
    discarded = round(_untracked_qty(state) - sum(r["quantity"] for r in lots or []), 9)
    if lots is None or discarded < -_EPS:
        raise refuse(409, "output_unknown", "Output was received from this run, but its history does not show "
                     "which lots it went into, so what it received cannot be put right.")
    data: dict = {"discarded": max(discarded, 0.0), "repaired_by": str(op.user_id), "request": request}
    if output_item_id:
        if state.get("output_item_id"):
            raise refuse(409, "output_named", "This run already names the product it makes.")
        product = (await lock_projections(session, company_id, [output_item_id])).get(output_item_id)
        if product is None or product.entity_type != "item":
            raise refuse(422, "no_product", f"{output_item_id} is not an item of this company.", item=output_item_id)
        p = product.state or {}
        require_stock(p, output_item_id)
        if str(p.get("status") or "").lower() == "draft":
            raise refuse(422, "output_draft", f"{p.get('sku') or output_item_id} is a draft. Make it available first.",
                         sku=p.get("sku") or output_item_id)
        expected = float((state.get("expected_outputs") or [{}])[0].get("quantity") or 0)
        data |= {"output_item_id": output_item_id, "expected_outputs": [output_line(p, expected)]}
    elif not data["discarded"]:
        raise refuse(409, "nothing_to_repair", "Everything this run received is in its lots. Choose the product "
                     "it makes to put anything else right.")
    await op.emit_run("mfg.order.output_repaired", data, f"mfg:{order_id}:repair:{rk}")
    await settle_open_runs(session, company_id)
    return {"discarded": data["discarded"], "output_item_id": data.get("output_item_id")}


def _inventory_codes(settings: dict) -> set[str]:
    return {code for role in INVENTORY_VALUE_ROLES for code in scope_codes(settings, role.value)}


async def _awaiting_reconciliation(session: AsyncSession, company_id, order_id: str) -> bool:
    """Whether another open run's value is still unknown: the books may hold it anywhere."""
    return any(r.entity_id != order_id and (r.state.get("wip_unresolved") or r.state.get("wip_untracked"))
               for r in await _open_runs(session, company_id))


async def reconcile(session: AsyncSession, company_id, user_id, order_id: str, components: list[dict],
                    account: str | None, key: str | None, *, at: str) -> dict:
    """Record what a run needing reconciliation holds, as someone allowed to keep the books
    states it: the value of each component issued to it, and the account that value comes
    off. Nothing is worked out for the user, and nothing is left for the books to disagree with.

    Output an older release already received from the run (legacy_output) took its share of
    that value: each such lot is restated to the share a receipt takes today (its quantity
    over the run's output), so the run holds issued value less its lots, exactly as if it
    had been received on this release. Only the value the lots did not already carry comes
    off the account: an inventory account holding it beyond its stock on hand, or retained
    earnings when the books carry no such value anywhere (they never recognized it). A
    reconciliation that would leave the account holding anything beyond its stock is
    refused while no other run waits for reconciling, so the books carry the stock and the
    work in progress afterwards just as after any live movement."""
    rk = key or uuid.uuid4().hex
    op = await _begin(session, company_id, user_id, order_id, at)
    request = _fingerprint({"components": components, "account": account})
    stored = await _replayed(op, f"mfg:{order_id}:reconcile:{rk}", request)
    if stored is not None:
        return {k: stored.data.get(k) for k in ("issued", "components", "receipts")} | {
            "reconciled": stored.data.get("issued")}
    run = await _run(op)
    _require_open(run.state, "reconciled")
    if not run.state.get("wip_unresolved"):
        raise refuse(409, "not_unresolved", "This run does not need reconciling.")

    needed = await still_held(session, company_id, order_id, run.state)
    values: dict[str, Decimal] = {}
    for line in components:
        item_id, value = line.get("item_id"), op.round(_money(line.get("value")))
        if item_id not in needed or item_id in values or value < 0:
            raise refuse(422, "reconcile_values", f"{item_id} is not a component still in this run, is named "
                         "twice, or has a negative value.", item=item_id)
        values[item_id] = value
    if set(values) != needed:
        missing = sorted(needed - set(values))
        raise refuse(422, "reconcile_missing", f"Give the value of every component still in this run: "
                     f"{', '.join(missing)} has none.", items=", ".join(missing))
    total = sum(values.values(), _ZERO)

    legacy = await legacy_output(session, company_id, order_id, run.state)
    if legacy is None:
        raise refuse(409, "output_unknown", "Output was received from this run, but its history does not show "
                     "which lots it went into or what they cost, so it cannot be reconciled.")
    expected = float((run.state.get("expected_outputs") or [{}])[0].get("quantity") or 0)
    made = max(expected, float(run.state.get("received_qty") or 0))
    receipts, transferred, carried = [], _ZERO, _ZERO
    if legacy:
        # A lot whose value has since gone where no re-cost can follow (split, sold in part,
        # transformed, used, written off) keeps what it was given, for good; the rest of the
        # value is shared over the output whose final cost can still be recorded.
        kept = [not await _cost_reachable(session, company_id, r["lot_item_id"]) for r in legacy]
        fixed = sum((r["value"] for r, k in zip(legacy, kept) if k), _ZERO)
        if fixed > total:
            names = []
            for r, k in zip(legacy, kept):
                row = await session.get(Projection, {"company_id": company_id, "entity_id": r["lot_item_id"]})
                if k:
                    names.append(((row.state or {}) if row is not None else {}).get("sku") or r["lot_item_id"])
            names = ", ".join(sorted(names))
            raise refuse(422, "reconcile_kept",
                         f"{names}, received from this run, cannot be re-costed (it was split, sold in part, "
                         f"transformed, used or written off before this release) and keeps the {op.round(fixed)} it "
                         f"was given, more than the {op.round(total)} stated. Give the value that was issued to "
                         "this run, at least that much.", lots=names, kept=str(op.round(fixed)),
                         issued=str(op.round(total)))
        weights = [_money(r["quantity"]) for r, k in zip(legacy, kept) if not k]
        rest = _money(made) - sum((_money(r["quantity"]) for r in legacy), _ZERO)
        weights += [rest] if rest > 0 else []
        shares = iter(allocate_pro_rata(total - fixed, weights, op.currency) if weights else [])
        for r, k in zip(legacy, kept):
            value = r["value"] if k else next(shares)
            receipts.append({"lot_item_id": r["lot_item_id"], "quantity": r["quantity"], "value": str(value),
                             **({"fixed": True} if k else {})})
            transferred += value
            carried += r["value"]
    amount = total - carried  # what the books get from the account: the lots already carry the rest

    settings = await current_settings(session, company_id)
    lots: dict[str, Decimal] = {}
    equity = _ZERO
    wip_code = None
    if op.books:
        inventory = _inventory_codes(settings)
        retained = account and account == role_map(settings).get(AccountRole.RETAINED_EARNINGS.value)
        if amount and not retained and account not in inventory:
            raise refuse(422, "reconcile_account",
                         "Choose the account this value comes off: an inventory account that holds it beyond its "
                         "stock on hand, or retained earnings for value the books never carried.",
                         total=str(amount), account=account)
        if not amount or retained:
            # Value from nowhere the books show: only while no inventory account holds more than
            # its stock, for an account that does is where this run's value (or its lots' excess)
            # already sits. One holding less (goods invoiced before they are on hand) holds none of it.
            holding = sorted(c for c, v in (await account_rooms(session, company_id, inventory)).items() if v > 0)
            if amount < 0:
                # The lots already carry more than the value stated: that is stock value the
                # books hold, never something retained earnings gives up.
                raise refuse(422, "reconcile_excess",
                             f"The lots this run already received carry {carried}, more than the {total} stated. "
                             "Give the value that was issued, or take the difference off the inventory account "
                             "that carries them.", carried=str(carried), issued=str(total))
            if holding:
                raise refuse(422, "reconcile_held",
                             f"{', '.join(holding)} holds more than its stock on hand, so the value of this "
                             "run's materials is already on the books: take it off that account.",
                             total=str(amount), account=account, holding=", ".join(holding))
            equity = -amount
        else:
            room = await account_room(session, company_id, account)
            left = room - amount
            if left < 0 or (left and not await _awaiting_reconciliation(session, company_id, order_id)):
                raise refuse(422, "reconcile_left",
                             f"{account} holds {room} beyond its stock on hand; taking {amount} off it would leave "
                             f"{left}, so the books would still disagree with the stock. Give the value it holds.",
                             total=str(amount), account=account, room=str(room), left=str(left))
            lots = {account: -amount}
        if (amount or transferred != carried or total) and not await period_open(session, company_id, op.day):
            raise refuse(422, "period_locked", f"The books are locked for {op.day}, so this run cannot be "
                         "reconciled until that period is open.", day=op.day)
        if total - transferred:
            wip_code = await _wip_target(op)
    for r, old in zip(receipts, legacy):
        delta = _money(r["value"]) - old["value"]
        if delta:
            code = await _restate(op, r["lot_item_id"], delta,
                                  f"mfg:{order_id}:reconcile:{rk}:recost:{r['lot_item_id']}", "reconciled")
            if code:
                lots[code] = lots.get(code, _ZERO) + delta
    await op.post(f"reconcile:{rk}", f"Materials in production run {order_id} reconciled", wip_code,
                  total - transferred, lots, equity=equity)
    recorded = [{"item_id": i, "value": str(v)} for i, v in sorted(values.items())]
    data = {"issued": str(total), "transferred": str(transferred), "receipts": receipts, "components": recorded,
            "reconciled_by": str(op.user_id), "request": request, "wip_account_code": wip_code}
    if op.books and amount:
        data["account"] = account
    await op.emit_run("mfg.order.wip_reconciled", data, f"mfg:{order_id}:reconcile:{rk}")
    return {"reconciled": str(total), "issued": str(total), "components": recorded, "receipts": receipts}
