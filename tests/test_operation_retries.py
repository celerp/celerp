# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Sending the same request again does not do it twice.

A client that loses the response and sends the same request with the same key gets
the first answer back, and nothing is booked, paid or moved a second time. The same
key sent with a different request is refused.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import (
    _account_net,
    _cleanup,
    _seed_chart,
    _seed_company,
    _seed_user,
)

DATE = "2026-03-02"


async def _final(client, auth, doc_type: str, total: float = 100.0, **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "line_items": [{"name": "Service", "quantity": 1, "unit_price": total}],
        "total": total, **extra,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc_id


async def _pay(client, auth, doc_id: str, amount: float) -> None:
    r = await client.post(f"/docs/{doc_id}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": DATE, "bank_account": "1111"})
    assert r.status_code == 200, r.text


async def _footprint(session, auth) -> tuple[int, int]:
    """How many events and records the company has."""
    session.expire_all()
    events = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar()
    records = (await session.execute(select(func.count()).select_from(Projection).where(
        Projection.company_id == auth["company_id"]))).scalar()
    return events, records


# Each case sets up a document and returns the request to send: method, path, the
# body, and a different body for the same key.

async def _payment(client, session, auth):
    inv = await _final(client, auth, "invoice")
    return "POST", f"/docs/{inv}/payment", \
        {"amount": 40.0, "payment_date": DATE, "bank_account": "1111"}, {"amount": 30.0}


async def _refund(client, session, auth):
    inv = await _final(client, auth, "invoice")
    await _pay(client, auth, inv, 100.0)
    return "POST", f"/docs/{inv}/refund", \
        {"amount": 30.0, "payment_date": DATE, "bank_account": "1111"}, {"amount": 20.0}


async def _void_payment(client, session, auth):
    inv = await _final(client, auth, "invoice")
    await _pay(client, auth, inv, 100.0)
    index = (await _state(session, auth, inv))["payments"][0]["index"]
    return "POST", f"/docs/{inv}/void-payment", \
        {"payment_index": index, "void_reason": "Bounced"}, {"void_reason": "Wrong invoice"}


async def _delete_payment(client, session, auth):
    inv = await _final(client, auth, "invoice")
    await _pay(client, auth, inv, 100.0)
    index = (await _state(session, auth, inv))["payments"][0]["index"]
    return "DELETE", f"/docs/{inv}/payments/{index}", \
        {"delete_reason": "Entered twice"}, {"delete_reason": "Wrong invoice"}


async def _void(client, session, auth):
    inv = await _final(client, auth, "invoice")
    return "POST", f"/docs/{inv}/void", {"reason": "Cancelled"}, {"reason": "Duplicate"}


async def _revert(client, session, auth):
    inv = await _final(client, auth, "invoice")
    return "POST", f"/docs/{inv}/revert-to-draft", {"reason": "Typo"}, {"reason": "Wrong price"}


async def _unvoid(client, session, auth):
    inv = await _final(client, auth, "invoice")
    r = await client.post(f"/docs/{inv}/void", headers=auth["headers"], json={"reason": "Cancelled"})
    assert r.status_code == 200, r.text
    return "POST", f"/docs/{inv}/unvoid", {"reason": "Voided by mistake"}, {"reason": "Customer came back"}


async def _apply_credit(client, session, auth):
    inv = await _final(client, auth, "invoice")
    cn = await _final(client, auth, "credit_note", 30.0)
    return "POST", f"/docs/{cn}/apply-to-invoice", \
        {"target_doc_id": inv, "amount": 20.0, "date": DATE}, {"amount": 10.0}


async def _credit_refund(client, session, auth):
    cn = await _final(client, auth, "credit_note", 30.0)
    return "POST", f"/docs/{cn}/cn-refund", \
        {"amount": 10.0, "date": DATE, "bank_account": "1111"}, {"amount": 5.0}


async def _bulk_payment(client, session, auth):
    docs = [await _final(client, auth, "invoice", 60.0) for _ in range(2)]
    return "POST", "/docs/bulk-payment", \
        {"doc_ids": docs, "amount": 90.0, "payment_date": DATE, "bank_account": "1111"}, {"amount": 80.0}


async def _receive(client, session, auth):
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "purchase_order", "line_items": [
            {"sku": f"RT-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 14.0}]})
    assert r.status_code == 200, r.text
    po = r.json()["id"]
    sku = (await _state(session, auth, po))["line_items"][0]["sku"]
    line = {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 4, "receive_as": "stock"}
    return "POST", f"/docs/{po}/receive", \
        {"location_id": "loc:1", "received_items": [line]}, \
        {"received_items": [{**line, "quantity_received": 3}]}


async def _receive_into_stock(client, session, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": f"RS-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 10,
        "cost_total": 100.0, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "purchase_order", "line_items": [
            {"item_id": item_id, "name": "Lot", "quantity": 10, "unit_price": 14.0}]})
    assert r.status_code == 200, r.text
    line = {"po_line_index": 0, "item_id": item_id, "quantity_received": 4, "receive_as": "stock"}
    return "POST", f"/docs/{r.json()['id']}/receive", \
        {"location_id": "loc:1", "received_items": [line]}, \
        {"received_items": [{**line, "quantity_received": 3}]}


async def _return_items(client, session, auth):
    sku = f"CIN-{uuid.uuid4().hex[:6]}"
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "consignment_in", "contact_id": "contact:consignor",
        "line_items": [{"sku": sku, "name": sku, "quantity": 3, "unit_price": 50.0}]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/docs/{doc}/receive", headers=auth["headers"], json={
        "location_id": "loc:1", "received_items": [
            {"po_line_index": 0, "sku": sku, "name": sku, "quantity_received": 3, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    [item_id] = (await _state(session, auth, doc))["received_item_ids"]
    return "POST", f"/docs/{doc}/return-items", \
        {"items": [{"item_id": item_id, "quantity_returned": 1}]}, \
        {"items": [{"item_id": item_id, "quantity_returned": 2}]}


async def _receive_return(client, session, auth):
    sku = f"RR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": sku, "name": "Widget", "quantity": 2, "cost_price": 40.0,
        "sell_by": "piece"})
    assert r.status_code == 200, r.text
    await client.post(f"/items/{r.json()['id']}/status", headers=auth["headers"], json={"new_status": "sold"})
    line = {"name": "Widget", "sku": sku, "quantity": 2, "unit_price": 50.0}
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [line]})
    inv = r.json()["id"]
    await client.post(f"/docs/{inv}/finalize", headers=auth["headers"])
    cn = await _final(client, auth, "credit_note", 100.0, original_doc_id=inv, line_items=[line])
    return "POST", f"/docs/{cn}/receive-return", \
        {"items": [{"sku": sku, "quantity": 1}]}, {"items": [{"sku": sku, "quantity": 2}]}


async def _send(client, session, auth):
    inv = await _final(client, auth, "invoice")
    return "POST", f"/docs/{inv}/send", {"sent_via": "print"}, {"sent_via": "whatsapp"}


async def _close(client, session, auth):
    memo = await _final(client, auth, "memo")
    return "POST", f"/docs/{memo}/close", {"reason": "settled"}, {"reason": "other"}


async def _reopen(client, session, auth):
    memo = await _final(client, auth, "memo")
    r = await client.post(f"/docs/{memo}/close", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    return "POST", f"/docs/{memo}/reopen", {"reason": "not settled"}, {"reason": "other"}


DOORS = {
    "payment": _payment, "refund": _refund, "void_payment": _void_payment,
    "delete_payment": _delete_payment, "void": _void, "revert_to_draft": _revert,
    "unvoid": _unvoid, "apply_credit": _apply_credit, "credit_refund": _credit_refund,
    "bulk_payment": _bulk_payment, "receive": _receive, "receive_into_stock": _receive_into_stock, "return_items": _return_items,
    "receive_return": _receive_return, "send": _send, "close": _close, "reopen": _reopen,
}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.asyncio
async def test_the_same_request_sent_again_does_nothing_more(client, session, auth, door):
    method, path, body, other = await DOORS[door](client, session, auth)
    key = f"retry-{uuid.uuid4().hex}"

    def send(payload: dict):
        return client.request(method, path, headers=auth["headers"], json={**payload, "idempotency_key": key})

    first = await send(body)
    assert first.status_code == 200, first.text
    after = await _footprint(session, auth)

    again = await send(body)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await _footprint(session, auth) == after

    refused = await send({**body, **other})
    assert refused.status_code == 409, refused.text
    assert await _footprint(session, auth) == after


@pytest.mark.asyncio
async def test_payment_books_its_amount_once(client, session, auth):
    inv = await _final(client, auth, "invoice")
    body = {"amount": 40.0, "payment_date": DATE, "bank_account": "1111", "idempotency_key": "pay-once"}
    for _ in range(2):
        r = await client.post(f"/docs/{inv}/payment", headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    assert await _account_net(session, auth["company_id"], "1111") == 40.0
    assert (await _state(session, auth, inv))["amount_outstanding"] == 60.0


@pytest.mark.asyncio
async def test_bulk_payment_books_its_amount_once(client, session, auth):
    docs = [await _final(client, auth, "invoice", 60.0) for _ in range(2)]
    body = {"doc_ids": docs, "amount": 90.0, "payment_date": DATE, "bank_account": "1111",
            "idempotency_key": "bulk-once"}
    for _ in range(2):
        r = await client.post("/docs/bulk-payment", headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
        assert (r.json()["total_allocated"], r.json()["remaining"]) == (90.0, 0.0)
    assert await _account_net(session, auth["company_id"], "1111") == 90.0
    outstanding = sorted([(await _state(session, auth, d))["amount_outstanding"] for d in docs])
    assert outstanding == [0.0, 30.0]


@pytest.mark.asyncio
async def test_receipt_moves_stock_once(client, session, auth):
    _, path, body, _ = await _receive(client, session, auth)
    body = {**body, "idempotency_key": "receive-once"}
    for _ in range(2):
        r = await client.post(path, headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    po = path.split("/")[2]
    [parcel] = [await _state(session, auth, i) for i in (await _state(session, auth, po))["received_item_ids"]]
    assert (parcel["quantity"], parcel["cost_total"]) == (4, 56.0)
    assert await _account_net(session, auth["company_id"], "1130-P") == 56.0


@pytest.mark.asyncio
async def test_payment_sent_twice_at_once_is_recorded_once(_db_engine):
    from celerp_docs.routes import DocPaymentBody, record_payment

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    await _seed_chart(factory, company_id)
    inv = "doc:INV-RETRY-RACE"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=inv, entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "status": "final", "ref_id": "INV-RETRY-RACE", "currency": "USD",
                  "line_items": [{"name": "Service", "quantity": 1, "unit_price": 100.0}],
                  "subtotal": 100.0, "total": 100.0, "amount_outstanding": 100.0},
            actor_id=user_id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()

    def body() -> DocPaymentBody:
        return DocPaymentBody(amount=40.0, payment_date=DATE, bank_account="1111",
                              idempotency_key="pay-race")

    first, second = factory(), factory()
    release = asyncio.Event()
    commit = first.commit

    async def held_commit() -> None:
        await release.wait()
        await commit()

    first.commit = held_commit
    user = types.SimpleNamespace(id=user_id)
    try:
        a = asyncio.create_task(record_payment(inv, body(), company_id=company_id, _=None, user=user, session=first))
        await asyncio.sleep(0.3)
        b = asyncio.create_task(record_payment(inv, body(), company_id=company_id, _=None, user=user, session=second))
        await asyncio.sleep(0.3)
        release.set()
        ra, rb = await asyncio.wait_for(asyncio.gather(a, b), timeout=10)
        assert ra == rb
        async with factory() as s:
            row = await s.get(Projection, {"company_id": company_id, "entity_id": inv})
            assert [p["amount"] for p in row.state["payments"]] == [40.0]
            assert await _account_net(s, company_id, "1111") == 40.0
    finally:
        await first.close()
        await second.close()
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_a_document_sent_again_is_emailed_once(client, session, auth, monkeypatch):
    import celerp_docs.routes as doc_routes

    sent: list[str] = []
    monkeypatch.setattr(doc_routes, "_email_with_receipt", lambda *a, **k: sent.append(k["to"]))
    inv = await _final(client, auth, "invoice")
    body = {"sent_to": "buyer@example.test", "idempotency_key": "send-once"}
    for _ in range(2):
        r = await client.post(f"/docs/{inv}/send", headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{inv}/send", headers=auth["headers"],
                          json={**body, "sent_to": "someone-else@example.test"})
    assert r.status_code == 409, r.text
    assert sent == ["buyer@example.test"]


@pytest.mark.asyncio
async def test_the_longest_key_works_on_a_document_with_a_long_number(client, session, auth):
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "purchase_order", "ref_id": "PO-" + "7" * 60, "line_items": [
            {"sku": f"LK-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 14.0}]})
    assert r.status_code == 200, r.text
    po = r.json()["id"]
    sku = (await _state(session, auth, po))["line_items"][0]["sku"]
    body = {"location_id": "loc:1", "idempotency_key": "k" * 200, "received_items": [
        {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 4, "receive_as": "stock"}]}
    for _ in range(2):
        r = await client.post(f"/docs/{po}/receive", headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    assert await _account_net(session, auth["company_id"], "1130-P") == 56.0


@pytest.mark.asyncio
async def test_a_returned_sale_taken_back_twice_at_once_is_taken_back_once(_db_engine):
    from celerp_docs.routes import undo_receive_return

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    await _seed_chart(factory, company_id)
    item, cn = "item:RET-RACE", "doc:CN-RET-RACE"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=item, entity_type="item", event_type="item.created",
            data={"sku": "RET-RACE", "name": "Widget", "quantity": 1, "cost_total": 40.0,
                  "sell_by": "piece", "status": "available"},
            actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=cn, entity_type="doc", event_type="doc.created",
            data={"doc_type": "credit_note", "status": "final", "ref_id": "CN-RET-RACE", "currency": "USD",
                  "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 50.0}], "total": 50.0,
                  "return_received_items": [{"item_id": item, "sku": "RET-RACE", "quantity": 1, "cost_total": 40.0}]},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()

    first, second = factory(), factory()
    release = asyncio.Event()
    commit = first.commit

    async def held_commit() -> None:
        await release.wait()
        await commit()

    first.commit = held_commit
    user = types.SimpleNamespace(id=user_id)
    try:
        a = asyncio.create_task(undo_receive_return(cn, company_id=company_id, _=None, user=user, session=first))
        await asyncio.sleep(0.3)
        b = asyncio.create_task(undo_receive_return(cn, company_id=company_id, _=None, user=user, session=second))
        await asyncio.sleep(0.3)
        release.set()
        ra, rb = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), timeout=10)
        assert ra == {"undone": True, "item_ids": [item]}
        assert getattr(rb, "status_code", None) == 409, rb
        async with factory() as s:
            assert await _account_net(s, company_id, "5100") == 40.0
    finally:
        await first.close()
        await second.close()
        await _cleanup(factory, company_id, user_id)
