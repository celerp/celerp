# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Correcting the cost of a lot after it was sold or merged.

The books after a late correction must equal the books of the same history with
the correction made just before the lot first left (sold, merged): every
account's total, with the opening inventory reconciled in both. Each test builds
both histories in two companies and compares them; where the second history
cannot be reproduced exactly, the correction is refused and nothing changes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from test_cost_restatement import (
    TZ, _cogs_adjustments, _doc_cogs, _fulfil, _invoice, _item, _merge, _set_cost, _state,
    company_auth,
)
from ui.i18n import t

EDIT = "edit"   # where the late correction falls in a history


async def _new_company(session) -> dict:
    return await company_auth(session, uuid.uuid4(), uuid.uuid4())


async def _trial_balance(session, auth) -> dict[str, float]:
    """Every account's posted total (debit positive), opening inventory reconciled."""
    await auto_je.upsert_opening_inventory_je(session, company_id=auth["company_id"], user_id=auth["user_id"])
    await session.commit()
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
    ))).scalars().all()
    totals: dict[str, float] = {}
    for row in rows:
        if (row.state or {}).get("status") != "posted":
            continue
        for e in row.state.get("entries", []):
            totals[e["account"]] = totals.get(e["account"], 0.0) + float(e.get("debit") or 0) - float(e.get("credit") or 0)
    return {acct: round(v, 2) for acct, v in sorted(totals.items()) if round(v, 2)}


async def _run(client, session, auth, steps, lot: str, new_cost, at: int) -> tuple[dict, object]:
    """Play the history with the correction inserted before step `at`."""
    ctx: dict = {}
    response = None
    for i, step in enumerate([*steps[:at], EDIT, *steps[at:]]):
        if step == EDIT:
            response = await _set_cost(client, auth, ctx[lot], new_cost)
        else:
            await step(client, session, auth, ctx)
    return ctx, response


async def _oracle(client, session, steps, lot: str, new_cost, *, before: int, late: int | None = None):
    """The correction made late (before step `late`, default after everything) books
    the same totals as the correction made before step `before`. Returns the late
    world's (auth, ctx, response)."""
    late = len(steps) if late is None else late
    early_auth, late_auth = await _new_company(session), await _new_company(session)
    _, early = await _run(client, session, early_auth, steps, lot, new_cost, before)
    assert early.status_code == 200, early.text
    ctx, response = await _run(client, session, late_auth, steps, lot, new_cost, late)
    assert response.status_code == 200, response.text
    assert await _trial_balance(session, late_auth) == await _trial_balance(session, early_auth)
    return late_auth, ctx, response


# -- History steps ---------------------------------------------------------

def make(name: str, cost, qty: float = 1, **extra):
    async def step(client, session, auth, ctx):
        if extra:
            data = {"sku": f"SC-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty,
                    "sell_by": "piece", "status": "available", **extra}
            if cost is not None:
                data["cost_total"] = cost
            r = await client.post("/items", headers=auth["headers"], json=data)
            assert r.status_code == 200, r.text
            ctx[name] = r.json()["id"]
        else:
            ctx[name] = await _item(client, auth, cost, qty)
    return step


def merge(name: str, *sources: str):
    async def step(client, session, auth, ctx):
        ctx[name] = await _merge(client, auth, [ctx[s] for s in sources])
    return step


def invoice(*names: str, doc: str = "doc"):
    async def step(client, session, auth, ctx):
        ctx[doc] = await _invoice(client, session, auth, *(ctx[n] for n in names))
    return step


def fulfil(*names: str, doc: str = "doc"):
    async def step(client, session, auth, ctx):
        await _fulfil(client, ctx[doc], auth, *(ctx[n] for n in names))
    return step


def sell(*names: str, doc: str = "doc"):
    async def step(client, session, auth, ctx):
        await invoice(*names, doc=doc)(client, session, auth, ctx)
        await fulfil(*names, doc=doc)(client, session, auth, ctx)
    return step


def doc_action(action: str, doc: str = "doc", **body):
    async def step(client, session, auth, ctx):
        r = await client.post(f"/docs/{ctx[doc]}/{action}", headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    return step


def revert_lines(*names: str, doc: str = "doc"):
    async def step(client, session, auth, ctx):
        r = await client.post(f"/docs/{ctx[doc]}/revert-lines", headers=auth["headers"],
                              json={"line_entity_ids": [ctx[n] for n in names]})
        assert r.status_code == 200, r.text
    return step


def mark_sold(name: str):
    async def step(client, session, auth, ctx):
        r = await client.post(f"/items/{ctx[name]}/status", headers=auth["headers"], json={"new_status": "sold"})
        assert r.status_code == 200, r.text
    return step


def reconcile_opening_inventory():
    async def step(client, session, auth, ctx):
        await auto_je.upsert_opening_inventory_je(session, company_id=auth["company_id"], user_id=auth["user_id"])
        await session.commit()
    return step


def manual_inventory_je(amount: float):
    """A posted journal entry putting stock value on the books outside any item."""
    async def step(client, session, auth, ctx):
        r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
            "ts": "2026-01-01", "memo": "Stock count", "idempotency_token": uuid.uuid4().hex, "entries": [
                {"account": "1130-P", "debit": amount, "credit": 0},
                {"account": "3100", "debit": 0, "credit": amount}]})
        assert r.status_code == 200, r.text
    return step


# Invoices issued before invoices kept their cost allocation: the finalize entry
# carried no cost of goods sold, which was posted at fulfillment (or later, by
# the one-time backfill).

def legacy_invoice(*names: str, doc: str = "doc"):
    """An invoice finalized the way releases before COGS-at-finalize did."""
    async def step(client, session, auth, ctx):
        await invoice(*names, doc=doc)(client, session, auth, ctx)
        je_id = f"je:auto:{ctx[doc]}:fin"
        row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})
        cogs_free = [e for e in row.state["entries"] if e["account"] not in ("5100", auto_je._INVENTORY_ACCT)]
        row.state = {**row.state, "entries": cogs_free}
        created = (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == je_id,
            LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().one()
        created.data = {**created.data, "entries": cogs_free}
        created.metadata_ = {k: v for k, v in created.metadata_.items() if k != "cogs_allocations"}
        await session.commit()
        assert await auto_je.recognized_cogs(session, auth["company_id"], ctx[doc]) is None
    return step


def legacy_fulfil(*names: str, doc: str = "doc", cycle: int = 0):
    """Fulfilment that booked the lots' cost of sale on its own entry."""
    async def step(client, session, auth, ctx):
        await fulfil(*names, doc=doc)(client, session, auth, ctx)
        total = 0.0
        for n in names:
            total += auto_je.lot_cost_of_sale(await _state(session, auth, ctx[n]))
        await auto_je.create_for_doc_fulfilled(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                               doc_id=ctx[doc], total_cogs=total, cycle=cycle)
        await session.commit()
    return step


async def _clear_backfill_marker(session) -> None:
    from celerp.migrations._data_reconcile import get_meta
    from celerp.services.cogs_backfill import COGS_BACKFILL_KEY
    conn = await session.connection()
    await conn.run_sync(lambda c: get_meta(c, COGS_BACKFILL_KEY))
    await session.execute(text("DELETE FROM instance_meta WHERE key = :k"), {"k": COGS_BACKFILL_KEY})


async def _set_backfill_marker(session) -> None:
    from celerp.migrations._data_reconcile import set_meta
    from celerp.services.cogs_backfill import COGS_BACKFILL_KEY
    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, COGS_BACKFILL_KEY, "done"))
    await session.commit()


def cogs_backfill():
    """The one-time startup posting of COGS for invoices that never had it."""
    async def step(client, session, auth, ctx):
        from celerp.services.cogs_backfill import run_cogs_backfill
        await _clear_backfill_marker(session)
        await run_cogs_backfill(session)
        await session.commit()
    return step


async def _snapshot(session, auth) -> tuple:
    """Everything a company has written: ledger rows and every projection's state."""
    session.expire_all()
    ledger = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar()
    rows = (await session.execute(select(Projection.entity_id, Projection.state, Projection.version).where(
        Projection.company_id == auth["company_id"]))).all()
    return ledger, sorted((eid, repr(state), version) for eid, state, version in rows)


async def _refused(client, session, auth, item_id: str, new_cost, *fragments: str):
    before = await _snapshot(session, auth)
    r = await _set_cost(client, auth, item_id, new_cost)
    assert r.status_code == 409, r.text
    for fragment in fragments:
        assert fragment in r.json()["detail"], r.json()["detail"]
    assert await _snapshot(session, auth) == before
    return r.json()["detail"]


# -- 1-3: a sold lot's cost is added, cleared, or changed -------------------

@pytest.mark.asyncio
async def test_customer_case_cost_added_to_a_lot_sold_without_one(client, session):
    auth, ctx, response = await _oracle(client, session, [make("a", None), sell("a")], "a", 100.0, before=1)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 100.0
    doc_number = (await _state(session, auth, ctx["doc"]))["doc_number"]
    assert response.json()["cost_correction"] == {
        "cogs_adjusted": [{"doc_number": doc_number, "amount": 100.0}], "cogs_unposted": []}


@pytest.mark.asyncio
async def test_cost_cleared_on_a_sold_lot(client, session):
    auth, ctx, _ = await _oracle(client, session, [make("a", 100.0), sell("a")], "a", None, before=1)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 0.0


@pytest.mark.asyncio
async def test_cost_changed_on_a_sold_lot(client, session):
    auth, ctx, _ = await _oracle(client, session, [make("a", 100.0), sell("a")], "a", 130.0, before=1)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 130.0


@pytest.mark.asyncio
async def test_cost_added_through_the_price_endpoint(client, session):
    auth = await _new_company(session)
    item = await _item(client, auth, None)
    ctx: dict = {"a": item}
    await sell("a")(client, session, auth, ctx)
    r = await client.post(f"/items/{item}/price", headers=auth["headers"],
                          json={"price_type": "cost_total", "new_price": 80.0})
    assert r.status_code == 200, r.text
    assert r.json()["cost_correction"]["cogs_adjusted"][0]["amount"] == 80.0
    assert await _doc_cogs(session, auth, ctx["doc"]) == 80.0


# -- 4: merged without a cost ----------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("b_cost", [None, 50.0])
@pytest.mark.parametrize("result_sold", [False, True])
async def test_cost_added_to_a_merged_source(client, session, b_cost, result_sold):
    steps = [make("a", None), make("b", b_cost), merge("c", "a", "b")]
    if result_sold:
        steps.append(sell("c"))
    auth, ctx, _ = await _oracle(client, session, steps, "a", 30.0, before=2)
    # A merge result costs the sum of its parts only when every part has a cost.
    expected = None if b_cost is None else 30.0 + b_cost
    assert (await _state(session, auth, ctx["c"])).get("cost_total") == expected


@pytest.mark.asyncio
async def test_cost_cleared_on_a_merged_source_refuses(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 100.0), make("b", 50.0), merge("c", "a", "b")):
        await step(client, session, auth, ctx)
    sku = (await _state(session, auth, ctx["a"]))["sku"]
    await _refused(client, session, auth, ctx["a"], None, sku, "sum of its parts", "merge result's cost instead")


@pytest.mark.asyncio
async def test_correction_never_drives_a_merge_result_negative(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 100.0), make("b", 50.0), merge("c", "a", "b")):
        await step(client, session, auth, ctx)
    assert (await _set_cost(client, auth, ctx["c"], 20.0)).status_code == 200
    await _refused(client, session, auth, ctx["a"], 10.0, "cannot absorb")


# -- 5, 6: consignment-in and non-stock lots -------------------------------

@pytest.mark.asyncio
async def test_cost_added_to_a_sold_consignment_lot(client, session):
    await _oracle(client, session, [make("a", None, consignment_flag="in"), sell("a")], "a", 70.0, before=1)


@pytest.mark.asyncio
async def test_cost_added_to_a_sold_service_line(client, session):
    await _oracle(client, session, [make("a", None, inventory_type="service"), sell("a")], "a", 40.0, before=1)


# -- 7: sold by hand, with no sale document --------------------------------

@pytest.mark.asyncio
async def test_lot_marked_sold_by_hand_saves_the_cost_and_posts_nothing(client, session):
    auth, ctx, response = await _oracle(client, session, [make("a", None), mark_sold("a")], "a", 60.0, before=1)
    assert (await _state(session, auth, ctx["a"]))["cost_total"] == 60.0
    sku = (await _state(session, auth, ctx["a"]))["sku"]
    assert response.json()["cost_correction"] == {"cogs_adjusted": [], "cogs_unposted": [sku]}


# -- 8, 19: invoices issued before COGS-at-finalize ------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [None, 100.0])
async def test_legacy_invoice_whose_fulfilment_booked_cogs(client, session, cost):
    steps = [make("a", cost), make("b", 20.0), legacy_invoice("a", "b"), legacy_fulfil("a", "b")]
    auth, ctx, _ = await _oracle(client, session, steps, "a", 130.0, before=2)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 150.0


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [None, 100.0])
async def test_legacy_invoice_whose_cogs_the_backfill_posted_is_not_posted_again(client, session, cost):
    steps = [make("a", cost), legacy_invoice("a"), fulfil("a"), cogs_backfill(), cogs_backfill()]
    auth, ctx, _ = await _oracle(client, session, steps, "a", 130.0, before=1, late=4)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 130.0


@pytest.mark.asyncio
async def test_legacy_invoice_waiting_for_its_cogs_refuses(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 100.0), legacy_invoice("a"), fulfil("a")):
        await step(client, session, auth, ctx)
    await _clear_backfill_marker(session)
    await session.commit()
    try:
        await _refused(client, session, auth, ctx["a"], 120.0, "has not posted yet", "next time Celerp starts")
    finally:
        await _set_backfill_marker(session)


@pytest.mark.asyncio
async def test_legacy_revert_to_draft_books_cogs_once(client, session):
    """An invoice whose fulfilment booked its COGS, reverted to draft and issued again,
    recognizes its goods once."""
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 100.0), legacy_invoice("a"), legacy_fulfil("a"), revert_lines("a"),
                 doc_action("revert-to-draft"), doc_action("finalize"), fulfil("a")):
        await step(client, session, auth, ctx)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 100.0


@pytest.mark.asyncio
async def test_legacy_void_reverses_cogs_booked_at_fulfilment(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 100.0), legacy_invoice("a"), legacy_fulfil("a"), revert_lines("a"),
                 doc_action("void")):
        await step(client, session, auth, ctx)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 0.0
    await doc_action("unvoid")(client, session, auth, ctx)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 100.0


# -- 9: several lines, one shipped -----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", ["a", "b"])
async def test_invoice_with_one_of_two_lines_shipped(client, session, corrected):
    steps = [make("a", None), make("b", None), invoice("a", "b"), fulfil("a")]
    await _oracle(client, session, steps, corrected, 45.0, before=2)


# -- 10-12: the invoice changes after the correction -----------------------

@pytest.mark.asyncio
async def test_revert_and_reissue_after_the_correction(client, session):
    steps = [make("a", None), sell("a"), revert_lines("a"), doc_action("revert-to-draft"),
             doc_action("finalize"), fulfil("a")]
    await _oracle(client, session, steps, "a", 100.0, before=1, late=2)


@pytest.mark.asyncio
async def test_correct_revert_correct_again_reissue(client, session):
    """Two corrections across a revert book what the last cost says."""
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", None), sell("a")):
        await step(client, session, auth, ctx)
    assert (await _set_cost(client, auth, ctx["a"], 100.0)).status_code == 200
    for step in (revert_lines("a"), doc_action("revert-to-draft")):
        await step(client, session, auth, ctx)
    assert (await _set_cost(client, auth, ctx["a"], 70.0)).status_code == 200
    for step in (doc_action("finalize"), fulfil("a")):
        await step(client, session, auth, ctx)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 70.0
    early = await _new_company(session)
    ectx: dict = {}
    for step in (make("a", 70.0), sell("a")):
        await step(client, session, early, ectx)
    assert await _trial_balance(session, auth) == await _trial_balance(session, early)


@pytest.mark.asyncio
async def test_void_and_restore_after_the_correction(client, session):
    steps = [make("a", None), sell("a"), revert_lines("a"), doc_action("void"), doc_action("unvoid"), fulfil("a")]
    await _oracle(client, session, steps, "a", 100.0, before=1, late=2)


def credit_note_return(name: str, doc: str = "doc"):
    async def step(client, session, auth, ctx):
        lot = await _state(session, auth, ctx[name])
        r = await client.post("/docs", headers=auth["headers"], json={
            "doc_type": "credit_note", "original_doc_id": ctx[doc], "ref_id": f"CN-{uuid.uuid4().hex[:6]}",
            "line_items": [{"sku": lot["sku"], "name": "Lot", "quantity": 1, "unit_price": 500.0}],
            "total": 500.0})
        assert r.status_code == 200, r.text
        ctx["cn"] = r.json()["id"]
        assert (await client.post(f"/docs/{ctx['cn']}/finalize", headers=auth["headers"])).status_code == 200
        r = await client.post(f"/docs/{ctx['cn']}/receive-return", headers=auth["headers"],
                              json={"items": [{"sku": lot["sku"], "quantity": 1}]})
        assert r.status_code == 200, r.text
    return step


@pytest.mark.asyncio
async def test_return_after_the_correction(client, session):
    steps = [make("a", None), sell("a"), credit_note_return("a")]
    await _oracle(client, session, steps, "a", 100.0, before=1, late=2)


@pytest.mark.asyncio
async def test_correction_after_a_return_refuses(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 100.0), sell("a"), credit_note_return("a")):
        await step(client, session, auth, ctx)
    sku = (await _state(session, auth, ctx["a"]))["sku"]
    await _refused(client, session, auth, ctx["a"], 120.0, sku, "later returned", "journal entry")


# -- 13: replay ------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_same_correction_sent_twice_posts_once(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", None), sell("a")):
        await step(client, session, auth, ctx)
    key = f"restate-{uuid.uuid4().hex}"
    first = await _set_cost(client, auth, ctx["a"], 100.0, key=key)
    assert first.status_code == 200, first.text
    before = await _snapshot(session, auth)
    again = await _set_cost(client, auth, ctx["a"], 100.0, key=key)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await _snapshot(session, auth) == before
    assert await _doc_cogs(session, auth, ctx["doc"]) == 100.0


# -- 14: the current period is locked ---------------------------------------

@pytest.mark.asyncio
async def test_locked_current_period_refuses_and_writes_nothing(client, session):
    from celerp.services.business_time import business_date_at

    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", None), sell("a")):
        await step(client, session, auth, ctx)
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, auth["company_id"])
    lock_date = business_date_at(datetime.now(timezone.utc), TZ)
    company.settings = {**company.settings, "lock_date": lock_date}
    await session.commit()
    before = await _snapshot(session, auth)
    r = await _set_cost(client, auth, ctx["a"], 100.0)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("error.period_locked", date=lock_date)
    assert await _snapshot(session, auth) == before


# -- 15: opening inventory clamp -------------------------------------------

@pytest.mark.asyncio
async def test_books_carrying_more_stock_than_the_catalog(client, session):
    steps = [manual_inventory_je(1000.0), make("a", None), make("b", 10.0), reconcile_opening_inventory(), sell("a")]
    await _oracle(client, session, steps, "a", 100.0, before=4)


# -- 16: invalid and extreme costs -----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-5.0, "abc"])
async def test_invalid_cost_on_a_sold_lot_writes_nothing(client, session, bad):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", None), sell("a")):
        await step(client, session, auth, ctx)
    before = await _snapshot(session, auth)
    r = await _set_cost(client, auth, ctx["a"], bad)
    assert r.status_code in (409, 422), r.text
    assert await _snapshot(session, auth) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [0.005, 100.004, 123456789.13])
async def test_rounded_and_large_costs(client, session, cost):
    await _oracle(client, session, [make("a", None), sell("a")], "a", cost, before=1)


# -- 17: a sold lot still allocated on another invoice ---------------------

@pytest.mark.asyncio
async def test_sold_lot_also_allocated_on_another_open_invoice(client, session):
    steps = [make("a", None), invoice("a", doc="waiting"), sell("a")]
    auth, ctx, _ = await _oracle(client, session, steps, "a", 100.0, before=1)
    assert await _doc_cogs(session, auth, ctx["waiting"]) == 100.0


# -- 18: who may correct a cost --------------------------------------------

@pytest.mark.asyncio
async def test_correction_on_a_sold_lot_needs_the_cost_permission(client, session):
    from test_helpers import perm_setup

    ctx_p = await perm_setup(client, session)
    admin, operator = ctx_p["admin_h"], ctx_p["operator_h"]
    auth = {"headers": admin}
    item = await _item(client, auth, None)
    r = await client.post("/docs", headers=admin, json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "total": 500.0,
        "line_items": [{"sku": (await client.get(f"/items/{item}", headers=admin)).json()["sku"],
                        "name": "Lot", "quantity": 1, "unit_price": 500.0, "entity_id": item}]})
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=admin)).status_code == 200
    await _fulfil(client, doc, auth, item)
    events = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    body = {"fields_changed": {"cost_total": {"old": None, "new": 100.0}}}
    assert (await client.patch(f"/items/{item}", headers=operator, json=body)).status_code == 403
    session.expire_all()
    assert (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar() == events


# -- Disposed lots stay refused ---------------------------------------------

@pytest.mark.asyncio
async def test_written_off_lot_refuses_and_writes_nothing(client, session):
    auth = await _new_company(session)
    item = await _item(client, auth, None)
    await emit_event(session, company_id=auth["company_id"], entity_id=item, entity_type="item",
                     event_type="item.status.set", data={"new_status": "disposed"},
                     actor_id=auth["user_id"], location_id=None, source="test",
                     idempotency_key=f"dispose-{uuid.uuid4().hex}", metadata_={})
    await session.commit()
    await _refused(client, session, auth, item, 100.0, "written off")


# -- Hardening at the same seam --------------------------------------------

@pytest.mark.asyncio
async def test_negative_cost_is_refused_on_any_lot(client, session):
    auth = await _new_company(session)
    item = await _item(client, auth, 10.0)
    detail = await _refused(client, session, auth, item, -1.0, "cannot be negative")
    assert (await _state(session, auth, item))["sku"] in detail


@pytest.mark.asyncio
async def test_half_a_cent_recognized_at_finalize_is_kept_at_fulfilment(client, session):
    auth = await _new_company(session)
    ctx: dict = {}
    for step in (make("a", 0.005), sell("a")):
        await step(client, session, auth, ctx)
    assert await _doc_cogs(session, auth, ctx["doc"]) == 0.01
    assert await _cogs_adjustments(session, auth, ctx["doc"]) == {}
