# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Delete never dead-ends on a draft that was a mistake.

A draft nothing else refers to is erased without a trace. A draft another record
refers to, or one holding files, moves to Deleted instead: hidden from every list,
picker and search, shown as "<SKU> [Deleted]" wherever a record names it, refused by
every action that would use it, and put back as a draft by Restore. An import never
blocks Delete: the deleted draft leaves the import, and undoing the rest still works.
The response says what happened to each item.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

import celerp.db
from celerp.models.import_batch import ImportBatch
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from mfg_runs import issue, product, run
from stock_books import assert_books_carry_stock
from test_cost_restatement import _state
from test_posting_roles_delete import _delete, _draft, _events, _exists, _imported, _undo
from test_posting_roles_ingress import _items_by_sku, _row, main_location
from test_posting_roles_kept_stock import _ok

pytestmark = pytest.mark.asyncio


# --- helpers ---------------------------------------------------------------------------

async def _sku(session, auth, lot: str) -> str:
    return (await _state(session, auth, lot))["sku"]


async def _legacy_doc(session, auth, lot: str, doc_type: str = "quotation") -> str:
    """A document an older release wrote naming ``lot`` on a line, without today's
    checks on new lines (no document may newly take a draft)."""
    doc_id = f"doc:{uuid.uuid4()}"
    number = f"Q-{uuid.uuid4().hex[:5]}"
    legacy = LedgerEntry(company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
                         event_type="doc.created",
                         data={"doc_type": doc_type, "doc_number": number, "status": "draft", "line_items": [
                             {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0}]},
                         actor_id=auth["user_id"], location_id=None, source="api",
                         idempotency_key=str(uuid.uuid4()), metadata_={})
    session.add(legacy)
    await session.flush()
    await ProjectionEngine.apply_event(session, legacy)
    await session.commit()
    return doc_id


async def _moved(client, auth, lot: str, sku: str, *referrers: str, files: bool = False) -> dict:
    """Delete ``lot``: it moves to Deleted, and the answer says so, naming what still
    refers to it."""
    r = await _delete(client, auth, lot)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["deleted"], body["moved_to_deleted"]) == (0, 1), body
    [entry] = body["items"]
    assert (entry["entity_id"], entry["sku"], entry["outcome"]) == (lot, sku, "moved_to_deleted"), entry
    for referrer in referrers:
        assert referrer in entry["referenced_by"], entry
    assert entry["has_files"] is files, entry
    return entry


async def _is_deleted(session, auth, lot: str) -> None:
    state = await _state(session, auth, lot)
    assert state["status"] == "deleted", state


async def _listed(client, auth, lot: str, **params) -> bool:
    r = await client.get("/items", headers=auth["headers"], params={"limit": 1000, **params})
    assert r.status_code == 200, r.text
    return lot in {i.get("entity_id") or i.get("id") for i in r.json()["items"]}


async def _searched(session, auth, q: str) -> set[str]:
    """The item ids the global search bar's inventory provider finds for ``q``."""
    from celerp_inventory.search import global_search
    found = await global_search(session, auth["company_id"], "owner", q, 50)
    return {i.get("entity_id") or i.get("id") for i in found["items"]}


async def _restore(client, auth, *lots: str):
    return await client.post("/items/bulk/restore-deleted", headers=auth["headers"], json={"entity_ids": list(lots)})


def _deleted_refusal(r, sku: str, *, status: int = 409, key: str = "item.deleted") -> None:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    text = detail if isinstance(detail, str) else (detail.get("message") or "")
    if isinstance(detail, dict) and detail.get("message_key"):
        assert detail["message_key"] == key, detail
    assert sku in text and "deleted" in text and "restore" in text.lower(), detail


# --- Nothing refers to it: erased ------------------------------------------------------

async def test_a_draft_nothing_refers_to_is_erased_and_the_answer_says_so(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    r = await _delete(client, auth, lot)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1 and r.json()["moved_to_deleted"] == 0, r.text
    assert r.json()["items"] == [{"entity_id": lot, "sku": sku, "outcome": "deleted",
                                  "referenced_by": [], "has_files": False}], r.text
    assert not await _exists(session, auth, lot)
    assert await _events(session, auth, lot) == 0


# --- An import never blocks Delete ------------------------------------------------------

async def _undoable_import_listing(session, client, auth, *lots: str) -> tuple[str, str]:
    """An import that can be undone whose entry also lists ``lots``, as an older release
    wrote it (today's importers bring rows in as stock, so none lists a draft that never
    was). Returns the import and the stock item it brought in."""
    sku = f"IMP-{uuid.uuid4().hex[:5]}"
    batch = await _imported(client, auth, _row(sku, 30.0))
    [imported] = [row.entity_id for row in await _items_by_sku(session, auth["company_id"], sku)]
    stored = await session.get(ImportBatch, uuid.UUID(batch))
    assert stored.reversible and stored.status == "active"
    stored.entity_ids = [*stored.entity_ids, *lots]
    stored.row_count += len(lots)
    await session.commit()
    return batch, imported


async def test_a_draft_from_an_import_that_can_be_undone_is_deleted_and_the_rest_still_undoes(session, client, auth):
    lot = await _draft(client, auth)
    batch, imported = await _undoable_import_listing(session, client, auth, lot)

    r = await _delete(client, auth, lot)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    assert not await _exists(session, auth, lot)
    session.expire_all()
    stored = await session.get(ImportBatch, uuid.UUID(batch))
    assert stored.entity_ids == [imported] and stored.status == "active"

    r = await _undo(client, auth, batch)
    assert r.status_code == 200, r.text
    assert not await _exists(session, auth, imported)


async def test_an_imported_draft_a_document_names_moves_to_deleted_and_leaves_the_import(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    batch, imported = await _undoable_import_listing(session, client, auth, lot)
    doc = await _legacy_doc(session, auth, lot)
    doc_number = (await _state(session, auth, doc))["doc_number"]
    await _moved(client, auth, lot, sku, doc_number)
    session.expire_all()
    assert (await session.get(ImportBatch, uuid.UUID(batch))).entity_ids == [imported]
    assert (await _undo(client, auth, batch)).status_code == 200
    await _is_deleted(session, auth, lot)


# --- Referred to: moved to Deleted, one test per kind of record that names an item -------

async def test_a_draft_on_a_document_line_moves_to_deleted(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    doc = await _legacy_doc(session, auth, lot)
    number = (await _state(session, auth, doc))["doc_number"]
    entry = await _moved(client, auth, lot, sku, number)
    await _is_deleted(session, auth, lot)
    # The document is never edited and still reads.
    r = await client.get(f"/docs/{doc}", headers=auth["headers"])
    assert r.status_code == 200 and r.json()["line_items"][0]["entity_id"] == lot, r.text
    assert entry["referenced_by"] == [number]
    # Using it on a new document is refused, naming the fix.
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "quotation", "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    _deleted_refusal(r, sku, status=422)


@pytest.mark.parametrize("doc_type", ["purchase_order", "memo"])
async def test_a_draft_on_a_purchase_order_or_memo_line_moves_to_deleted(session, client, auth, doc_type):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    doc = await _legacy_doc(session, auth, lot, doc_type)
    await _moved(client, auth, lot, sku, (await _state(session, auth, doc))["doc_number"])
    await _is_deleted(session, auth, lot)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": doc_type, "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    _deleted_refusal(r, sku, status=422)


async def test_a_recipe_component_moves_to_deleted_and_the_recipe_refuses_it(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    made = await product(client, auth, [(lot, 1)])
    made_sku = await _sku(session, auth, made)
    await _moved(client, auth, lot, sku, made_sku)
    await _is_deleted(session, auth, lot)
    # The recipe naming it is never edited.
    assert [c["item_id"] for c in (await _state(session, auth, made))["recipe"]["components"]] == [lot]
    # Saving a recipe that uses it, or building from one, is refused, naming the fix.
    r = await client.put(f"/manufacturing/items/{made}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": lot, "quantity": 1}], "labor": [], "overhead": []})
    _deleted_refusal(r, sku, status=422, key="mfg.component_deleted")
    assert f"Component {sku} was deleted; replace it in the recipe or restore it." in r.text
    r = await client.post(f"/manufacturing/items/{made}/build", headers=auth["headers"], json={"quantity": 1})
    _deleted_refusal(r, sku, status=422, key="mfg.component_deleted")


async def test_a_production_run_input_moves_to_deleted_and_issuing_it_is_refused(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    made = await product(client, auth, [(lot, 1)])
    order = await run(client, auth, made, 1)
    await _moved(client, auth, lot, sku, await _sku(session, auth, made))
    await _is_deleted(session, auth, lot)
    assert [i["item_id"] for i in (await _state(session, auth, order))["inputs"]] == [lot]
    r = await issue(client, auth, order, [(lot, 1)])
    _deleted_refusal(r, sku)


async def test_a_variant_parent_moves_to_deleted(session, client, auth):
    parent = await _draft(client, auth)
    sku = await _sku(session, auth, parent)
    child = (await _ok(client, auth, "POST", "/items", {
        "sku": f"VAR-{uuid.uuid4().hex[:6]}", "name": "Variant", "quantity": 1, "sell_by": "piece",
        "parent_item_id": parent}))["id"]
    await _moved(client, auth, parent, sku, await _sku(session, auth, child))
    await _is_deleted(session, auth, parent)
    assert (await _state(session, auth, child))["parent_item_id"] == parent


async def test_a_draft_holding_a_file_moves_to_deleted(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    r = await client.post(f"/items/{lot}/files", headers=auth["headers"],
                          files={"file": ("spec.txt", b"carat 1.02", "text/plain")})
    assert r.status_code == 200, r.text
    entry = await _moved(client, auth, lot, sku, files=True)
    assert entry["referenced_by"] == []
    await _is_deleted(session, auth, lot)


async def test_one_delete_erases_what_it_can_and_moves_the_rest(session, client, auth):
    free, named = await _draft(client, auth), await _draft(client, auth)
    await _legacy_doc(session, auth, named)
    r = await _delete(client, auth, free, named)
    assert r.status_code == 200, r.text
    assert (r.json()["deleted"], r.json()["moved_to_deleted"]) == (1, 1), r.text
    assert {e["entity_id"]: e["outcome"] for e in r.json()["items"]} == {free: "deleted", named: "moved_to_deleted"}
    assert not await _exists(session, auth, free)
    await _is_deleted(session, auth, named)


# --- Deleted is hidden, restorable, and books nothing -------------------------------------

async def test_a_deleted_item_is_hidden_from_lists_and_search_but_shown_by_its_filter(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    await _legacy_doc(session, auth, lot)
    assert lot in await _searched(session, auth, sku)
    await _moved(client, auth, lot, sku)
    assert not await _listed(client, auth, lot)
    assert not await _listed(client, auth, lot, status="all")
    assert not await _listed(client, auth, lot, status="draft")
    assert await _listed(client, auth, lot, status="deleted")
    assert lot not in await _searched(session, auth, sku)


async def test_the_deleted_view_counts_a_deleted_item_but_values_it_at_nothing(session, client, auth):
    lot = await _draft(client, auth, cost=40.0)
    sku = await _sku(session, auth, lot)
    await _legacy_doc(session, auth, lot)
    await _moved(client, auth, lot, sku)
    r = await client.get("/items/valuation", headers=auth["headers"], params={"status": "deleted"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_scoped_count"] == 1, body
    assert body["active_item_count"] == 0, body
    assert body["cost_total"] == 0, body
    assert not any(body["price_totals"].values()), body


async def test_a_deleted_item_is_never_low_stock_even_in_the_deleted_view(session, client, auth):
    lot = (await _ok(client, auth, "POST", "/items", {
        "sku": f"DEL-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
        "cost_total": 40.0, "reorder_point": 5}))["id"]
    sku = await _sku(session, auth, lot)
    assert (await _state(session, auth, lot))["reorder_point"] == 5
    await _legacy_doc(session, auth, lot)
    await _moved(client, auth, lot, sku)
    assert await _listed(client, auth, lot, status="deleted")
    assert not await _listed(client, auth, lot, status="deleted", filter="low_stock")


async def test_restore_puts_a_deleted_item_back_as_a_usable_draft(session, client, auth):
    lot = await _draft(client, auth)
    sku = await _sku(session, auth, lot)
    made = await product(client, auth, [(lot, 1)])
    await _moved(client, auth, lot, sku)
    r = await _restore(client, auth, lot)
    assert r.status_code == 200 and r.json()["restored"] == 1, r.text
    assert (await _state(session, auth, lot))["status"] == "draft"
    assert await _listed(client, auth, lot, status="draft")
    # Usable again: the recipe saves, and a draft that never became stock stays deletable.
    r = await client.put(f"/manufacturing/items/{made}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": lot, "quantity": 1}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    await _ok(client, auth, "POST", "/items/bulk/make-available", {"entity_ids": [lot]})
    assert (await _state(session, auth, lot))["status"] == "available"
    await assert_books_carry_stock(session, auth["company_id"])


async def test_restore_takes_only_deleted_items(session, client, auth):
    lot = await _draft(client, auth)
    r = await _restore(client, auth, lot)
    assert r.status_code == 409 and "not deleted" in r.text, r.text


async def test_moving_to_deleted_and_back_books_nothing(session, client, auth):
    lot = await _draft(client, auth, 40.0)
    await _legacy_doc(session, auth, lot)
    books = await assert_books_carry_stock(session, auth["company_id"])
    await _moved(client, auth, lot, await _sku(session, auth, lot))
    assert await assert_books_carry_stock(session, auth["company_id"]) == books
    assert (await _restore(client, auth, lot)).status_code == 200
    assert await assert_books_carry_stock(session, auth["company_id"]) == books


async def test_a_deleted_item_takes_no_status_edit(session, client, auth):
    lot = await _draft(client, auth)
    await _legacy_doc(session, auth, lot)
    await _moved(client, auth, lot, await _sku(session, auth, lot))
    for status in ("available", "archived"):
        r = await client.post("/items/bulk/status", headers=auth["headers"],
                              json={"entity_ids": [lot], "status": status})
        assert r.status_code == 422 and "Restore" in r.text, r.text
    r = await client.post("/items/bulk/status", headers=auth["headers"],
                          json={"entity_ids": [await _draft(client, auth)], "status": "deleted"})
    assert r.status_code == 422 and "Delete" in r.text, r.text


async def test_a_deleted_item_selected_again_is_erased_once_nothing_refers_to_it(session, client, auth):
    lot = await _draft(client, auth)
    doc = await _legacy_doc(session, auth, lot)
    await _moved(client, auth, lot, await _sku(session, auth, lot))
    r = await client.delete("/docs/bulk-draft", headers=auth["headers"], params={"doc_ids": doc})
    assert r.status_code == 200, r.text
    r = await _delete(client, auth, lot)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    assert not await _exists(session, auth, lot)


# --- Stock keeps today's rules ----------------------------------------------------------

async def test_stock_is_still_refused_whole_and_nothing_moves(session, client, auth):
    from test_posting_roles_kept_stock import _available
    draft, stock = await _draft(client, auth), await _available(client, auth, 100.0)
    await _legacy_doc(session, auth, draft)
    r = await _delete(client, auth, draft, stock)
    assert r.status_code == 409, r.text
    await session.rollback()
    assert (await _state(session, auth, draft))["status"] == "draft"


# --- A connector sync never brings it back -------------------------------------------------

async def test_a_connector_sync_does_not_bring_a_deleted_item_back(session, client, auth):
    from celerp_inventory import services

    class _Lent:
        def __init__(self, s):
            self.s = s

        async def __aenter__(self):
            return self.s

        async def __aexit__(self, *exc):
            return False

    async def _sync(**fields):
        record = types.SimpleNamespace(sku="SYNC-1", name="Ring", description=None, sale_price=10.0,
                                       quantity=None, cost_price=4.0, idempotency_key="xero:item:del")
        for key, value in fields.items():
            setattr(record, key, value)
        with patch.object(celerp.db, "SessionLocal", lambda: _Lent(session)):
            return await services.upsert_from_connector(str(auth["company_id"]), record)

    await main_location(client, auth["headers"])
    assert await _sync() == "created"
    lot = "item:xero:item:del"
    assert (await _state(session, auth, lot))["status"] == "draft"
    await _legacy_doc(session, auth, lot)
    await _moved(client, auth, lot, "SYNC-1")
    events = await _events(session, auth, lot)
    assert await _sync(name="Ring renamed") == "noop"
    await _is_deleted(session, auth, lot)
    assert (await _state(session, auth, lot))["name"] == "Ring"
    assert await _events(session, auth, lot) == events
    assert (await session.execute(select(LedgerEntry.id).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == lot,
        LedgerEntry.event_type == "item.updated"))).first() is None

