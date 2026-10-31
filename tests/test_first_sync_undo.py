# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Undoing a first sync's links reverses only what is still the first sync's: a field
edited since keeps its newer value and is reported, a link the item no longer holds (detached,
relinked to another product, item gone) is reported and left, the files the first sync pulled
onto a linked item go unless changed since, a stop inside the undo changes nothing and its
repeat completes, and restoring a price needs the price permission.

These run on the real database: the first sync and each request commit on their own."""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from company_backup_support import token
from connector_test_support import add_item, item_state
from migration_support import real_client, real_engine  # noqa: F401 - fixtures
from test_first_sync_recovery import DIRECTIONS, _Crash, _execute, _first_sync, _lease_expired, _retry, _started, store  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("connector_modules_running")]

UNDO = "/connectors/woocommerce/first-sync/undo-links"


class _Failed(RuntimeError):
    """The undo's transaction fails part way (a dropped connection): the request ends in error."""


async def _linked(name: str, *, settings: dict | None = None, role: str = "owner") -> tuple[uuid.UUID, uuid.UUID]:
    """A first sync that took the store's values onto two items Celerp had: ``item:match``
    (MATCH-1) and ``item:match2`` (NEW-1, whose store image it pulled)."""
    from celerp.db import get_session_ctx

    cid = await _started(name, DIRECTIONS)
    async with get_session_ctx() as session:
        await add_item(session, cid, "item:match2", "NEW-1", name="Own second", sale_price=2.0)
        if settings:
            await session.execute(sa.text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c"),
                                  {"s": __import__("json").dumps({"currency": "USD", **settings}), "c": str(cid)})
        uid = await session.scalar(sa.text("UPDATE user_companies SET role = :r WHERE company_id = :c RETURNING user_id"),
                                   {"c": cid, "r": role})
        await session.commit()
    await _execute(cid)
    assert (await _first_sync(cid)).state == "completed"
    return cid, uid


async def _headers(real_engine, cid, uid, role: str = "owner") -> dict:
    return {"Authorization": f"Bearer {await token(real_engine, uid, cid, role)}"}


async def _item(cid, entity_id: str) -> dict:
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        return await item_state(session, cid, entity_id)


def _linked_to_store(state: dict) -> bool:
    from celerp.connectors.product_links import external_link_for_state

    return bool(external_link_for_state(state, "woocommerce"))


async def test_undo_keeps_a_field_edited_since_and_reports_it(real_engine, real_client, store):
    cid, uid = await _linked("UndoEdited")
    h = await _headers(real_engine, cid, uid)
    r = await real_client.patch("/items/item:match", headers=h,
                                json={"fields_changed": {"name": {"old": "Store name", "new": "Renamed since"}}})
    assert r.status_code == 200, r.text
    r = await real_client.post(UNDO, headers=h)
    assert r.status_code == 200, r.text
    state = await _item(cid, "item:match")
    assert (state["name"], state["sale_price"]) == ("Renamed since", 10.0)
    assert not _linked_to_store(state)
    kept = {k["entity_id"]: k["fields"] for k in r.json()["kept"]}
    assert kept == {"item:match": {"name": {"before": "Celerp name", "current": "Renamed since"}}}


async def test_undo_after_a_later_sale_restores_only_the_first_syncs_values(real_engine, real_client, store):
    cid, uid = await _linked("UndoSold")
    h = await _headers(real_engine, cid, uid)
    before = (await _item(cid, "item:match")).get("quantity")
    r = await real_client.patch("/items/item:match", headers=h,
                                json={"fields_changed": {"quantity": {"old": before, "new": (before or 0) + 5}}})
    assert r.status_code == 200, r.text
    r = await real_client.post(UNDO, headers=h)
    assert r.status_code == 200 and r.json()["kept"] == [], r.text
    state = await _item(cid, "item:match")
    assert (state["name"], state["sale_price"], state.get("quantity")) == ("Celerp name", 10.0, (before or 0) + 5)


async def test_undo_reports_a_link_it_could_not_reverse_and_marks_only_the_rest(real_engine, real_client, store):
    """item:match was relinked to another store product since: its link and values stay and
    it is reported with each store value and file still on it; item:match2 is undone; the record keeps an outcome for each link."""
    from celerp.db import get_session_ctx
    from celerp.events.engine import emit_event

    cid, uid = await _linked("UndoRelinked")
    async with get_session_ctx() as session:
        state = await item_state(session, cid, "item:match")
        links = {**state["external_links"], "woocommerce": {**state["external_links"]["woocommerce"], "product_id": 77}}
        await emit_event(session, company_id=cid, entity_id="item:match", entity_type="item",
                         event_type="item.updated", data={"fields_changed": {"external_links": {
                             "old": state["external_links"], "new": links}}},
                         actor_id=None, location_id=None, source="connector", idempotency_key=f"relink:{cid}",
                         metadata_={})
        await session.commit()
    r = await real_client.post(UNDO, headers=await _headers(real_engine, cid, uid))
    assert r.status_code == 200, r.text
    assert r.json()["undone"] == 1
    assert [k["entity_id"] for k in r.json()["kept"]] == ["item:match"]
    record = {link["entity_id"]: link for link in (await _first_sync(cid)).undo["links"]}["item:match"]
    left = r.json()["kept"][0]
    assert left["fields"]["name"] == {"before": record["restore"]["name"], "current": "Store name"}, left
    assert left["files"] == list(record.get("files") or []), left
    relinked, undone = await _item(cid, "item:match"), await _item(cid, "item:match2")
    assert relinked["name"] == "Store name" and relinked["external_links"]["woocommerce"]["product_id"] == 77
    assert undone["name"] == "Own second" and not _linked_to_store(undone)
    outcomes = {link["entity_id"]: link.get("outcome") for link in (await _first_sync(cid)).undo["links"]}
    assert outcomes == {"item:match": "relinked", "item:match2": "undone"}, outcomes


async def test_undo_removes_the_files_the_first_sync_pulled_onto_a_linked_item_unless_changed_since(
        real_engine, real_client, store):
    cid, uid = await _linked("UndoFiles")
    files = (await _item(cid, "item:match2")).get("files") or []
    assert [f["id"] for f in files] == ["pulled1"], files
    links = {link["entity_id"]: link for link in (await _first_sync(cid)).undo["links"]}
    assert links["item:match2"].get("files") == ["pulled1"], links
    r = await real_client.post(UNDO, headers=await _headers(real_engine, cid, uid))
    assert r.status_code == 200, r.text
    assert (await _item(cid, "item:match2")).get("files") in (None, [])


async def test_undo_removes_only_the_files_its_own_first_sync_pulled(real_engine, real_client, store):
    """A file an earlier connection pulled under the same platform and item stays: the undo
    removes only files whose attachment names this first sync."""
    from celerp.db import get_session_ctx
    from celerp.services.attachments import attach_file

    cid = await _started("UndoOwnFiles", DIRECTIONS)
    async with get_session_ctx() as session:
        await add_item(session, cid, "item:match2", "NEW-1", name="Own second", sale_price=2.0)
        await attach_file(session, cid, "item", "item:match2", {
            "id": "old1", "filename": "old", "mime": "image/jpeg", "size": 3, "url": "/old1.jpg"}, None,
            source="connector", idempotency_key="connector-file:woocommerce:item:match2:earlierstore",
            document_tag="product_images", remote={"woocommerce": {"id": "9", "src": "x", "store": "earlier"}})
        uid = await session.scalar(sa.text("SELECT user_id FROM user_companies WHERE company_id = :c"), {"c": cid})
        await session.commit()
    await _execute(cid)
    links = {link["entity_id"]: link for link in (await _first_sync(cid)).undo["links"]}
    assert links["item:match2"].get("files") == ["pulled1"], links
    r = await real_client.post(UNDO, headers=await _headers(real_engine, cid, uid))
    assert r.status_code == 200, r.text
    assert [f["id"] for f in (await _item(cid, "item:match2")).get("files") or []] == ["old1"]


async def test_undo_keeps_a_pulled_file_changed_since(real_engine, real_client, store):
    cid, uid = await _linked("UndoFileTagged")
    h = await _headers(real_engine, cid, uid)
    r = await real_client.post("/items/item:match2/files/pulled1/tag", headers=h, data={"document_tag": "cert"})
    assert r.status_code == 200, r.text
    r = await real_client.post(UNDO, headers=h)
    assert r.status_code == 200, r.text
    assert [f["id"] for f in (await _item(cid, "item:match2")).get("files") or []] == ["pulled1"]
    kept = {k["entity_id"]: k for k in r.json()["kept"]}
    assert kept["item:match2"].get("files") == ["pulled1"], r.json()


async def test_an_interrupted_undo_changes_nothing_and_its_repeat_completes(real_engine, real_client, store, monkeypatch):
    from celerp_inventory import services

    cid, uid = await _linked("UndoStopped")
    h = await _headers(real_engine, cid, uid)
    original, seen = services.detach_external_link, []

    async def _stop_on_second(*args, **kwargs):
        seen.append(args[2])
        if len(seen) == 2:
            raise _Failed("inside the undo")
        return await original(*args, **kwargs)

    monkeypatch.setattr(services, "detach_external_link", _stop_on_second)
    with pytest.raises(_Failed):
        await real_client.post(UNDO, headers=h)
    assert len(seen) == 2
    for entity_id in ("item:match", "item:match2"):
        assert _linked_to_store(await _item(cid, entity_id)), entity_id
    assert all("outcome" not in link for link in (await _first_sync(cid)).undo["links"])
    monkeypatch.setattr(services, "detach_external_link", original)
    r = await real_client.post(UNDO, headers=h)
    assert (r.status_code, r.json()["undone"]) == (200, 2), r.text
    assert all(link.get("outcome") for link in (await _first_sync(cid)).undo["links"])
    r = await real_client.post(UNDO, headers=h)
    assert r.status_code == 409, r.text


async def test_a_retry_after_an_undo_records_its_links_and_their_undo_restores_them(real_engine, real_client, store):
    """An undo on a failed first sync settles its links; Retry links the items again with the
    store's values, each a new undo entry; the next undo brings Celerp's values back and the
    one after it has nothing left."""
    from celerp.db import get_session_ctx

    cid, uid = await _linked("UndoThenRetry")
    async with get_session_ctx() as session:
        await session.execute(sa.text(
            "UPDATE connector_first_syncs SET state = 'failed', progress = CAST(:p AS json) WHERE company_id = :c"),
            {"c": str(cid), "p": '{"products": {"state": "failed"}}'})
        await session.commit()
    h = await _headers(real_engine, cid, uid)
    r = await real_client.post(UNDO, headers=h)
    assert (r.status_code, r.json()["undone"]) == (200, 2), r.text
    assert (await _item(cid, "item:match"))["name"] == "Celerp name"
    await _retry(cid)
    await _execute(cid)
    assert (await _item(cid, "item:match"))["name"] == "Store name"
    links = (await _first_sync(cid)).undo["links"]
    assert sorted(((link["entity_id"], link.get("outcome")) for link in links), key=lambda p: (p[0], p[1] or "")) == [
        ("item:match", None), ("item:match", "undone"), ("item:match2", None), ("item:match2", "undone")], links
    assert (await _first_sync(cid)).links_holding_store_values == 2
    r = await real_client.post(UNDO, headers=h)
    assert (r.status_code, r.json()["undone"]) == (200, 2), r.text
    assert (await _item(cid, "item:match"))["name"] == "Celerp name"
    assert not _linked_to_store(await _item(cid, "item:match"))
    r = await real_client.post(UNDO, headers=h)
    assert r.status_code == 409, r.text


async def test_undo_of_a_price_needs_the_price_permission(real_engine, real_client, store):
    """An admin may manage the connection; here admins may not set prices, so restoring the
    item's own price is refused and nothing changes."""
    cid, uid = await _linked("UndoPrice", settings={"role_grants": {"set_inventory_prices": ["owner"]}},
                             role="admin")
    r = await real_client.post(UNDO, headers=await _headers(real_engine, cid, uid, "admin"))
    assert r.status_code == 403, r.text
    state = await _item(cid, "item:match")
    assert (state["name"], state["sale_price"]) == ("Store name", 99.0) and _linked_to_store(state)


SUMMARY = "/connectors/woocommerce/first-sync/summary"


def _order(order_id: int, status: str = "processing") -> dict:
    """A paid store order for one STOCK-1 (see ``_selling``)."""
    return {"id": order_id, "number": str(order_id), "status": status, "currency": "USD",
            "total": "15.00", "total_tax": "0",
            "line_items": [{"product_id": 950, "variation_id": 0, "sku": "STOCK-1", "name": "Sold in the store",
                            "quantity": 1, "total": "15.00", "total_tax": "0"}],
            "shipping_lines": [], "fee_lines": []}


async def _summary_invoices(real_client, real_engine, cid, uid) -> dict:
    r = await real_client.get(SUMMARY, headers=await _headers(real_engine, cid, uid))
    assert r.status_code == 200, r.text
    return {invoice["entity_id"]: invoice for invoice in r.json()["invoices"]}


async def _selling(name: str) -> uuid.UUID:
    """A first sync started (orders in) for a company with STOCK-1 on its shelf to sell."""
    from celerp.db import get_session_ctx

    cid = await _started(name, DIRECTIONS)
    async with get_session_ctx() as session:
        await add_item(session, cid, "item:stock", "STOCK-1", name="On the shelf", sale_price=15.0, quantity=5)
    return cid


async def _owner(cid) -> uuid.UUID:
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        return await session.scalar(sa.text("SELECT user_id FROM user_companies WHERE company_id = :c"), {"c": cid})


async def test_the_summary_lists_the_invoices_the_first_sync_imported(real_engine, real_client, store):
    """The orders the first sync imported are on its summary; one a later sync imports is not."""
    import httpx

    from celerp.connectors.sync_runner import run_connector_sync
    from celerp import connectors

    store.orders.mock(return_value=httpx.Response(200, json=[_order(501), _order(502)]))
    cid = await _selling("InvoicesListed")
    await _execute(cid)
    assert (await _first_sync(cid)).state == "completed"
    uid = await _owner(cid)
    listed = await _summary_invoices(real_client, real_engine, cid, uid)
    assert set(listed) == {"doc:woocommerce:order:501", "doc:woocommerce:order:502"}, listed
    assert all(invoice["number"] for invoice in listed.values()), listed

    store.orders.mock(return_value=httpx.Response(200, json=[_order(503)]))
    await run_connector_sync(connectors.get("woocommerce"), str(cid), purpose="manual_sync")
    assert set(await _summary_invoices(real_client, real_engine, cid, uid)) == set(listed)


async def test_an_invoice_imported_before_a_stop_stays_on_the_summary(real_engine, real_client, store, monkeypatch):
    """The process stops after the first order's document commits: that invoice is already
    on the first sync's record, and the run that resumes adds the second without a copy."""
    import httpx

    import celerp.connectors.upsert as upsert

    store.orders.mock(return_value=httpx.Response(200, json=[_order(511), _order(512)]))
    cid = await _selling("InvoiceStop")
    original, seen = upsert.upsert_order_from_woocommerce, []

    async def _stop_on_second(company_id, order, *args, **kwargs):
        seen.append(order["id"])
        if len(seen) == 2:
            raise _Crash("between two orders")
        return await original(company_id, order, *args, **kwargs)

    monkeypatch.setattr(upsert, "upsert_order_from_woocommerce", _stop_on_second)
    with pytest.raises(_Crash):
        await _execute(cid)
    uid = await _owner(cid)
    assert set(await _summary_invoices(real_client, real_engine, cid, uid)) == {"doc:woocommerce:order:511"}

    monkeypatch.setattr(upsert, "upsert_order_from_woocommerce", original)
    await _lease_expired(cid)
    for _ in range(3):
        if (await _first_sync(cid)).state == "completed":
            break
        await _execute(cid)
    assert set(await _summary_invoices(real_client, real_engine, cid, uid)) == {
        "doc:woocommerce:order:511", "doc:woocommerce:order:512"}


async def test_an_imported_invoice_is_voided_through_its_document(real_engine, real_client, store):
    """An imported invoice is reversed by voiding its document: the summary shows it voided,
    its posting stays in the books with the reversal beside it."""
    import httpx

    from celerp.db import get_session_ctx

    store.orders.mock(return_value=httpx.Response(200, json=[_order(521)]))
    cid = await _selling("InvoiceVoided")
    await _execute(cid)
    uid = await _owner(cid)
    h = await _headers(real_engine, cid, uid)
    doc_id = "doc:woocommerce:order:521"
    assert (await _summary_invoices(real_client, real_engine, cid, uid))[doc_id]["status"] == "final"
    r = await real_client.post(f"/docs/{doc_id}/void", headers=h, json={"reason": "Not ours"})
    assert r.status_code == 200, r.text
    assert (await _summary_invoices(real_client, real_engine, cid, uid))[doc_id]["status"] == "void"
    async with get_session_ctx() as session:
        events = (await session.execute(sa.text(
            "SELECT event_type FROM ledger WHERE company_id = :c AND entity_id = :d ORDER BY id"),
            {"c": cid, "d": doc_id})).scalars().all()
    assert events[0] == "doc.created" and "doc.voided" in events, events


async def test_disconnect_says_which_items_keep_the_stores_values(real_engine, real_client, store, monkeypatch):
    """Disconnecting forgets the first sync's undo record: its count of items still holding
    values taken from the store is on the connection and in the disconnect's answer; an item
    whose link was undone no longer counts."""
    from unittest.mock import AsyncMock

    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    monkeypatch.setattr("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock())
    cid, uid = await _linked("UndoDisconnect")
    h = await _headers(real_engine, cid, uid)
    async with get_session_ctx() as session:
        config = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(cid), ConnectorConfig.connector == "woocommerce"))
        assert config.links_holding_store_values == 2
    r = await real_client.delete("/connectors/woocommerce/credentials", headers=h)
    assert r.status_code == 200 and r.json() == {"ok": True, "kept_store_values": 2}, r.text
