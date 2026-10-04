# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Archived and expired stock the user keeps stays on the books.

Archive and Expire retire a lot from the catalog, but the company still owns it, so
its value stays on its inventory account and no entry is posted; restoring it makes
it available again, also with no entry. Write off stock is the one way to take
stock off the books without a sale. Rows a split, transform, merge undo, receipt
undo or return undo leaves archived carry nothing: their value went elsewhere with
the movement that archived them.

A plain status change cannot fake a sale, a merge, a write-off or a draft, and cannot
bring back stock that left the books that way.

Companies that archived or expired stock in an older release have those lots
recognized once on upgrade only where the books still carry their value, never from
the status alone; no entry is ever posted to invent it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.business_time import business_date_at
from celerp.services.company_lock import locked_company
from celerp.services.lot_origin import held_value
from stock_books import assert_books_carry_stock
from test_cost_restatement import TZ, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_helpers import sell_item
from test_posting_roles_merge import _merged
from test_posting_roles_older_stock import (
    _accounts, _custom_opening_account, _lot, _marked, _net, _older_release, _opening_entry, _reclassification,
    _settings,
)
from test_posting_roles_rollout import _startup
from test_receipt_accounting import _doc, _finalize, _receive

pytestmark = pytest.mark.asyncio

_FLAG = "inventory_on_books"


# --- helpers ---------------------------------------------------------------------------

async def _ok(client, auth, method: str, path: str, body: dict | None = None) -> dict:
    r = await client.request(method, path, headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _available(client, auth, cost: float, qty: float = 1) -> str:
    """A lot entered as a draft and made available, so its value is on the books."""
    lot = (await _ok(client, auth, "POST", "/items", {
        "sku": f"KS-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty, "sell_by": "piece",
        "cost_total": cost, "allow_splitting": True}))["id"]
    await _ok(client, auth, "POST", "/items/bulk/make-available", {"entity_ids": [lot]})
    return lot


async def _held(session, auth, lot: str):
    session.expire_all()
    return held_value(await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot}))


async def _entries(session, auth) -> set[str]:
    session.expire_all()
    return set((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars())


async def _books(session, client, auth) -> dict:
    """The balance sheet is viewed (the older-release path that recomputed opening
    inventory), then the books must carry exactly the stock recorded on them."""
    return await assert_books_carry_stock(session, auth["company_id"])


async def _bulk_status(client, auth, lot: str, status: str):
    return await client.post("/items/bulk/status", headers=auth["headers"],
                             json={"entity_ids": [lot], "status": status})


async def _single_status(client, auth, lot: str, status: str):
    return await client.post(f"/items/{lot}/status", headers=auth["headers"], json={"new_status": status})


async def _patch_status(client, auth, lot: str, status: str):
    return await client.patch(f"/items/{lot}", headers=auth["headers"],
                              json={"fields_changed": {"status": {"new": status}}})


_ROUTES = {"single": _single_status, "bulk": _bulk_status, "patch": _patch_status}


async def _write_off(client, auth, lot: str, qty: float) -> str:
    wo = (await _ok(client, auth, "POST", "/lists/writeoff", {"entity_ids": [lot]}))["id"]
    await _ok(client, auth, "POST", f"/lists/{wo}/writeoff-line", {"item_id": lot, "qty_out": qty, "account": "6950"})
    await _ok(client, auth, "POST", f"/lists/{wo}/write-off")
    return wo


# --- Archive, Expire and Restore keep the value on the books -----------------------------

@pytest.mark.parametrize("route", ["bulk", "single"])
async def test_archive_keeps_the_stock_on_the_books_with_no_entry(session, client, auth, route):
    lot = await _available(client, auth, 100.0)
    account = (await _state(session, auth, lot))["inventory_account_code"]
    before = await _entries(session, auth)

    r = await _ROUTES[route](client, auth, lot, "archived")
    assert r.status_code == 200, r.text

    state = await _state(session, auth, lot)
    assert state["status"] == "archived"
    assert state[_FLAG] is True
    assert await _held(session, auth, lot) == 100
    assert await _entries(session, auth) == before
    books = await _books(session, client, auth)
    assert (books[account], sum(books.values())) == (100, 100)


@pytest.mark.parametrize("route", ["bulk", "single"])
async def test_expire_keeps_the_stock_on_the_books_with_no_entry(session, client, auth, route):
    lot = await _available(client, auth, 100.0)
    before = await _entries(session, auth)

    if route == "bulk":
        await _ok(client, auth, "POST", "/items/bulk/expire", {"entity_ids": [lot]})
    else:
        await _ok(client, auth, "POST", f"/items/{lot}/expire")

    state = await _state(session, auth, lot)
    assert state["status"] == "expired"
    assert state[_FLAG] is True
    assert await _held(session, auth, lot) == 100
    assert await _entries(session, auth) == before
    assert sum((await _books(session, client, auth)).values()) == 100


@pytest.mark.parametrize("retire", ["archive", "expire"])
async def test_restoring_kept_stock_makes_it_available_on_the_same_account_with_no_entry(
        session, client, auth, retire):
    lot = await _available(client, auth, 100.0)
    account = (await _state(session, auth, lot))["inventory_account_code"]
    if retire == "archive":
        assert (await _bulk_status(client, auth, lot, "archived")).status_code == 200
    else:
        await _ok(client, auth, "POST", "/items/bulk/expire", {"entity_ids": [lot]})
    assert (await _state(session, auth, lot))[_FLAG] is True
    before = await _entries(session, auth)

    r = await _bulk_status(client, auth, lot, "available")
    assert r.status_code == 200, r.text

    state = await _state(session, auth, lot)
    assert state["status"] == "available"
    assert _FLAG not in state
    assert state["inventory_account_code"] == account
    assert await _held(session, auth, lot) == 100
    assert await _entries(session, auth) == before
    assert sum((await _books(session, client, auth)).values()) == 100


async def test_archive_then_expire_keeps_the_stock_once(session, client, auth):
    lot = await _available(client, auth, 100.0)
    assert (await _bulk_status(client, auth, lot, "archived")).status_code == 200
    await _ok(client, auth, "POST", "/items/bulk/expire", {"entity_ids": [lot]})
    state = await _state(session, auth, lot)
    assert (state["status"], state[_FLAG]) == ("expired", True)
    assert sum((await _books(session, client, auth)).values()) == 100


async def test_the_dashboard_counts_kept_stock_in_inventory_value(session, client, auth):
    lot = await _available(client, auth, 100.0)
    before = (await _ok(client, auth, "GET", "/dashboard/kpis"))["inventory"]["total_value_cost"]
    assert (await _bulk_status(client, auth, lot, "archived")).status_code == 200
    after = (await _ok(client, auth, "GET", "/dashboard/kpis"))["inventory"]["total_value_cost"]
    assert after == before


# --- Write off stock is the one non-sale exit -------------------------------------------

async def test_write_off_takes_the_value_off_once_and_undo_returns_it_once(session, client, auth):
    lot = await _available(client, auth, 100.0)
    wo = await _write_off(client, auth, lot, 1)
    state = await _state(session, auth, lot)
    assert state["status"] == "disposed"
    assert _FLAG not in state
    assert sum((await _books(session, client, auth)).values()) == 0

    await _ok(client, auth, "POST", f"/lists/{wo}/undo-write-off")
    assert (await _state(session, auth, lot))["status"] == "available"
    assert sum((await _books(session, client, auth)).values()) == 100


# --- Rows archived by a movement carry nothing ------------------------------------------

async def test_a_lot_used_up_by_a_split_stays_off_the_books(session, client, auth):
    lot = await _available(client, auth, 100.0, qty=2)
    for _ in range(2):
        await _ok(client, auth, "POST", f"/items/{lot}/split", {"children": [{"quantity": 1}]})
    state = await _state(session, auth, lot)
    assert state["status"] == "archived"
    assert _FLAG not in state
    assert await _held(session, auth, lot) is None
    assert sum((await _books(session, client, auth)).values()) == 100


async def test_a_lot_used_up_by_a_transform_stays_off_the_books(session, client, auth):
    lot = await _available(client, auth, 100.0)
    out = await _ok(client, auth, "POST", f"/items/{lot}/transform", {
        "child_sku": f"TF-{uuid.uuid4().hex[:6]}", "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 1.0, "child_cost_total": 100.0})
    state = await _state(session, auth, lot)
    assert state["status"] == "archived"
    assert _FLAG not in state
    assert await _held(session, auth, lot) is None
    assert await _held(session, auth, out["child_id"]) == 100
    await _books(session, client, auth)


async def test_an_undone_merge_result_stays_off_the_books(session, client, auth):
    a, b = await _available(client, auth, 30.0), await _available(client, auth, 70.0)
    merged = (await _merged(client, auth, [a, b]))["id"]
    await _ok(client, auth, "POST", f"/items/{merged}/undo-merge")
    state = await _state(session, auth, merged)
    assert state["status"] == "archived"
    assert _FLAG not in state
    assert await _held(session, auth, merged) is None
    assert sum((await _books(session, client, auth)).values()) == 100


async def test_goods_whose_receipt_is_undone_stay_off_the_books(session, client, auth):
    """A finalized bill books its goods whether or not they have arrived, so the books
    are compared with the bill's own entry: undoing the receipt leaves them exactly as
    they were before the goods came in, with the parcels carrying nothing."""
    bill = await _doc(client, auth, "bill", [{"sku": "GOODS", "name": "Goods", "quantity": 2, "unit_price": 15.0}])
    await _finalize(client, auth, bill)
    books = await _net(session, auth, "1130-P", "1130-OB")
    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "GOODS", "name": "Goods",
                                            "quantity_received": 2})
    assert r.status_code == 200, r.text
    parcels = (await _state(session, auth, bill))["received_item_ids"]
    await _ok(client, auth, "DELETE", f"/docs/{bill}/receive")
    for parcel in parcels:
        state = await _state(session, auth, parcel)
        assert state["status"] == "archived"
        assert _FLAG not in state
        assert await _held(session, auth, parcel) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == books


async def test_goods_whose_return_is_undone_stay_off_the_books(session, client, auth):
    lot = await _available(client, auth, 100.0)
    inv = await sell_item(client, auth["headers"], lot)
    cn = (await _ok(client, auth, "POST", "/docs", {
        "doc_type": "credit_note", "original_doc_id": inv, "total": 150.0, "line_items": [
            {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, auth, "POST", f"/docs/{cn}/finalize")
    sku = (await _state(session, auth, lot))["sku"]
    back = (await _ok(client, auth, "POST", f"/docs/{cn}/receive-return",
                      {"items": [{"sku": sku, "quantity": 1}]}))["received_items"][0]["item_id"]
    await _ok(client, auth, "DELETE", f"/docs/{cn}/receive-return")
    state = await _state(session, auth, back)
    assert state["status"] == "archived"
    assert _FLAG not in state
    assert sum((await _books(session, client, auth)).values()) == 0


# --- A status change cannot fake a sale, merge, write-off or draft ----------------------

@pytest.mark.parametrize("route", ["single", "bulk", "patch"])
@pytest.mark.parametrize("status", ["sold", "merged", "disposed", "draft", "expired"])
async def test_a_status_change_cannot_reach_an_outcome_another_action_owns(session, client, auth, route, status):
    lot = await _available(client, auth, 100.0)
    before = await _entries(session, auth)
    r = await _ROUTES[route](client, auth, lot, status)
    assert r.status_code == 422, r.text
    assert (await _state(session, auth, lot))["status"] == "available"
    assert await _entries(session, auth) == before
    assert sum((await _books(session, client, auth)).values()) == 100


async def _gone(how: str, session, client, auth) -> str:
    """A lot whose stock left the books through ``how``."""
    if how == "written off":
        lot = await _available(client, auth, 100.0)
        await _write_off(client, auth, lot, 1)
        return lot
    if how == "sold":
        lot = await _available(client, auth, 100.0)
        await sell_item(client, auth["headers"], lot)
        return lot
    if how == "merged":
        a, b = await _available(client, auth, 30.0), await _available(client, auth, 70.0)
        await _merged(client, auth, [a, b])
        return b
    lot = await _available(client, auth, 100.0)
    await _ok(client, auth, "POST", f"/items/{lot}/split", {"children": [{"quantity": 1}]})
    return lot


@pytest.mark.parametrize("route", ["single", "bulk", "patch"])
@pytest.mark.parametrize("how", ["written off", "sold", "merged", "split"])
async def test_stock_that_left_the_books_cannot_be_restored_by_a_status_change(
        session, client, auth, route, how):
    lot = await _gone(how, session, client, auth)
    status = (await _state(session, auth, lot))["status"]
    books = await _books(session, client, auth)
    before = await _entries(session, auth)

    r = await _ROUTES[route](client, auth, lot, "available")
    assert r.status_code == 422, r.text
    assert (await _state(session, auth, lot))["status"] == status
    assert await _entries(session, auth) == before
    assert await _books(session, client, auth) == books


@pytest.mark.parametrize("route", ["single", "bulk", "patch"])
async def test_a_sold_item_archived_to_tidy_the_catalog_stays_off_the_books(session, client, auth, route):
    lot = await _available(client, auth, 100.0)
    await sell_item(client, auth["headers"], lot)
    before = await _entries(session, auth)

    r = await _ROUTES[route](client, auth, lot, "archived")
    assert r.status_code == 200, r.text
    state = await _state(session, auth, lot)
    assert state["status"] == "archived"
    assert _FLAG not in state
    assert await _entries(session, auth) == before
    assert sum((await _books(session, client, auth)).values()) == 0
    # And it cannot come back as stock through a status edit.
    r = await _ROUTES[route](client, auth, lot, "available")
    assert r.status_code == 422, r.text
    assert sum((await _books(session, client, auth)).values()) == 0


@pytest.mark.parametrize("route", ["bulk", "single"])
async def test_sold_stock_cannot_be_expired(session, client, auth, route):
    lot = await _available(client, auth, 100.0)
    await sell_item(client, auth["headers"], lot)
    if route == "bulk":
        r = await client.post("/items/bulk/expire", headers=auth["headers"], json={"entity_ids": [lot]})
    else:
        r = await client.post(f"/items/{lot}/expire", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert (await _state(session, auth, lot))["status"] == "sold"
    assert sum((await _books(session, client, auth)).values()) == 0


async def test_kept_stock_is_recorded_by_the_system_never_entered(session, client, auth):
    lot = await _available(client, auth, 100.0)
    r = await client.patch(f"/items/{lot}", headers=auth["headers"],
                           json={"fields_changed": {_FLAG: {"old": None, "new": True}}})
    assert r.status_code == 422, r.text
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"KS-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
        "cost_total": 10.0, _FLAG: True})
    assert r.status_code == 422, r.text
    with pytest.raises(Exception) as exc:
        await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                         event_type="item.patched", data={"status": "archived", _FLAG: True},
                         actor_id=auth["user_id"], location_id=None, source="import",
                         idempotency_key=str(uuid.uuid4()), metadata_={})
    assert getattr(exc.value, "status_code", None) == 422
    await session.rollback()


# --- Upgrading a company that archived or expired stock in an older release -------------

async def _older(session, auth, lot: str, event_type: str, data: dict, *, metadata=None, source="api") -> None:
    """An event exactly as an older release wrote it."""
    await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                     event_type=event_type, data=data, actor_id=auth["user_id"], location_id=None,
                     source=source, idempotency_key=str(uuid.uuid4()), metadata_=metadata or {})
    await session.commit()


async def _older_archive(session, auth, lot: str) -> None:
    await _older(session, auth, lot, "item.status.set", {"new_status": "archived"})


async def _kept_stock_entry(session, auth) -> dict | None:
    return (await _state(session, auth, f"je:auto:kept-stock:{auth['company_id']}")) or None


async def test_older_archived_stock_the_books_still_carry_needs_no_entry(session, client, auth):
    await _older_release(session, auth)
    kept, other = await _lot(client, auth, 40.0), await _lot(client, auth, 30.0)
    await _opening_entry(session, auth, 70.0)
    await _older_archive(session, auth, kept)

    await _startup(session)

    assert (await _state(session, auth, kept))[_FLAG] is True
    assert await _kept_stock_entry(session, auth) is None
    assert await _accounts(session, auth, kept, other) == ["1130-P", "1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (70.0, 0.0)
    assert await _marked(session, auth)
    await _books(session, client, auth)

    events = await _event_count(session, auth)
    await _startup(session)
    assert await _event_count(session, auth) == events


async def test_older_expired_and_edited_to_archived_stock_is_kept(session, client, auth):
    await _older_release(session, auth)
    expired, archived_then_expired, edited = (await _lot(client, auth, 10.0), await _lot(client, auth, 20.0),
                                              await _lot(client, auth, 30.0))
    await _older(session, auth, expired, "item.expired", {})
    await _older_archive(session, auth, archived_then_expired)
    await _older(session, auth, archived_then_expired, "item.expired", {})
    await _older(session, auth, edited, "item.updated",
                 {"fields_changed": {"status": {"old": "available", "new": "archived"}}})
    await _opening_entry(session, auth, 60.0)

    await _startup(session)

    for lot in (expired, archived_then_expired, edited):
        assert (await _state(session, auth, lot))[_FLAG] is True
    assert await _kept_stock_entry(session, auth) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (60.0, 0.0)
    await _books(session, client, auth)


async def test_older_rows_archived_by_a_movement_or_an_import_are_not_kept(session, client, auth):
    await _older_release(session, auth)
    held = await _lot(client, auth, 30.0)
    split, received, imported = (await _lot(client, auth, 20.0), await _lot(client, auth, 25.0),
                                 f"item:{uuid.uuid4()}")
    await _older(session, auth, split, "item.status.set", {"new_status": "archived"},
                 metadata={"reason": "consumed_by_split"})
    await _older(session, auth, received, "item.status.set",
                 {"new_status": "archived", "reason": "undo receive on doc:x"}, source="receive_undo")
    await _older(session, auth, imported, "item.created", {
        "sku": f"IMP-{uuid.uuid4().hex[:6]}", "name": "Imported", "quantity": 1, "sell_by": "piece",
        "status": "archived", "cost_total": 15.0}, source="import")
    await _opening_entry(session, auth, 30.0)

    await _startup(session)

    for lot in (split, received, imported):
        assert _FLAG not in await _state(session, auth, lot)
    assert await _kept_stock_entry(session, auth) is None
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)
    await _books(session, client, auth)


async def test_a_rebuild_reproduces_kept_stock(session, client, auth):
    from celerp.projections.engine import ProjectionEngine

    await _older_release(session, auth)
    older = await _lot(client, auth, 40.0)
    await _older_archive(session, auth, older)
    await _opening_entry(session, auth, 40.0)
    await _startup(session)
    current = await _available(client, auth, 25.0)
    assert (await _bulk_status(client, auth, current, "archived")).status_code == 200
    split = await _available(client, auth, 10.0)
    await _ok(client, auth, "POST", f"/items/{split}/split", {"children": [{"quantity": 1}]})

    await ProjectionEngine.rebuild(session, auth["company_id"])
    await session.commit()

    assert (await _state(session, auth, older))[_FLAG] is True
    assert (await _state(session, auth, current))[_FLAG] is True
    assert _FLAG not in await _state(session, auth, split)
    await _books(session, client, auth)


async def _event_count(session, auth) -> int:
    from celerp.models.ledger import LedgerEntry

    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))


def _carried(entry: dict | None) -> dict:
    return {e["account"]: (e["debit"], e["credit"]) for e in (entry or {}).get("entries") or []}


async def test_older_archived_stock_the_books_do_not_carry_stays_off_them(session, client, auth):
    """The books carry exactly the stock on hand without the archived lot: nothing says
    it is still held, so it stays off the books and no entry invents its value."""
    await _older_release(session, auth)
    archived, other = await _lot(client, auth, 40.0), await _lot(client, auth, 30.0)
    await _older_archive(session, auth, archived)
    await _opening_entry(session, auth, 30.0)

    await _startup(session)

    assert _FLAG not in await _state(session, auth, archived)
    assert await _kept_stock_entry(session, auth) is None
    assert await _accounts(session, auth, archived, other) == ["1130-P", "1130-P"]
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)
    assert await _marked(session, auth)
    await _books(session, client, auth)


async def test_older_archived_stock_the_books_cannot_account_for_stays_off_them(session, client, auth):
    await _older_release(session, auth)
    archived, other = await _lot(client, auth, 40.0), await _lot(client, auth, 30.0)
    await _older_archive(session, auth, archived)
    await _opening_entry(session, auth, 50.0)

    await _startup(session)

    assert _FLAG not in await _state(session, auth, archived)
    assert await _kept_stock_entry(session, auth) is None
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, archived, other) == [None, None]
    assert await _marked(session, auth)


async def test_a_locked_day_leaves_older_archived_stock_untouched_until_a_later_start(session, client, auth):
    """Recognizing the archived lot and moving opening stock into purchased inventory
    happen together: the locked day refuses the entry, so the lot is not recognized
    either, and the next start after the lock is lifted does both."""
    await _older_release(session, auth)
    archived, other = await _lot(client, auth, 40.0), await _lot(client, auth, 30.0)
    await _older_archive(session, auth, archived)
    await _opening_entry(session, auth, 70.0)
    await _settings(session, auth, lock_date=business_date_at(datetime.now(timezone.utc), TZ))
    events = await _event_count(session, auth)

    await _startup(session)

    assert await _event_count(session, auth) == events
    assert _FLAG not in await _state(session, auth, archived)
    assert await _reclassification(session, auth) is None
    assert not await _marked(session, auth)

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()
    await _startup(session)

    assert (await _state(session, auth, archived))[_FLAG] is True
    assert _carried(await _reclassification(session, auth)) == {"1130-P": (70.0, 0.0), "1130-OB": (0.0, 70.0)}
    assert await _accounts(session, auth, archived, other) == ["1130-P", "1130-P"]
    assert await _marked(session, auth)
    await _books(session, client, auth)
    events = await _event_count(session, auth)
    await _startup(session)
    assert await _event_count(session, auth) == events


async def test_an_opening_account_that_cannot_take_entries_leaves_older_archived_stock_untouched(
        session, client, auth):
    await _older_release(session, auth)
    archived = await _lot(client, auth, 40.0)
    await _older_archive(session, auth, archived)
    await _opening_entry(session, auth, 40.0)
    await _custom_opening_account(session, client, auth)

    await _startup(session)

    assert _FLAG not in await _state(session, auth, archived)
    assert await _kept_stock_entry(session, auth) is None
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, archived) == [None]


async def test_an_upgrade_that_fails_part_way_keeps_none_of_its_work_and_retries(session, client, auth, monkeypatch):
    """Recognizing the archived lot, moving opening stock and marking the company are one
    unit: a failure after the first of them leaves the company as it was for the next start."""
    from celerp.notifications import service as notification_service

    await _older_release(session, auth)
    archived, other = await _lot(client, auth, 40.0), await _lot(client, auth, 30.0)
    await _older_archive(session, auth, archived)
    await _opening_entry(session, auth, 70.0)
    events = await _event_count(session, auth)

    async def refused(*args, **kwargs):
        raise RuntimeError("notification store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(notification_service, "create", refused)
        await _startup(session)

    assert await _event_count(session, auth) == events
    assert _FLAG not in await _state(session, auth, archived)
    assert await _reclassification(session, auth) is None
    assert await _accounts(session, auth, archived, other) == [None, None]
    assert not await _marked(session, auth)

    await _startup(session)

    assert (await _state(session, auth, archived))[_FLAG] is True
    assert await _accounts(session, auth, archived, other) == ["1130-P", "1130-P"]
    assert await _marked(session, auth)
    await _books(session, client, auth)
