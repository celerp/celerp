# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Goods are never received on a document while a line's tax is still for the user to set ("--").

A draft purchase order or consignment can receive goods, and a receipt takes it out of draft
for good, so the tax could no longer be chosen and the document could never be billed. The
shared receipt (record_receipt) refuses first, naming the line, before any stock, cost, event
or ledger change, for every caller: the receive route, an import and a connected company's sync.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_money_stock_and_contact_invariants import _auth_company
from ui.i18n import t


async def _template(client, auth) -> tuple[str, str]:
    sku = f"RX-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": 0, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"], sku


async def _draft(client, auth, doc_type: str, taxes: list) -> tuple[str, list[dict]]:
    """A draft *doc_type* with one line per entry of *taxes*, and the receipt line for each."""
    lines, received = [], []
    for i, tax in enumerate(taxes):
        item_id, sku = await _template(client, auth)
        lines.append({"item_id": item_id, "sku": sku, "name": "Lot", "quantity": 2, "unit_price": 5.0,
                      "line_total": 10.0, "taxes": tax})
        received.append({"item_id": item_id, "sku": sku, "name": "Lot", "quantity_received": 2,
                         "po_line_index": i, "receive_as": "stock"})
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "supplier:1", "line_items": lines})
    assert r.status_code == 200, r.text
    return r.json()["id"], received


async def _state(session, auth, doc_id: str) -> dict:
    session.expire_all()
    return (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc_id})).state


async def _footprint(session, auth) -> tuple[int, list]:
    """The company's ledger size and every item's quantity: what a receipt changes."""
    session.expire_all()
    cid = auth["company_id"]
    events = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid))).scalar_one()
    items = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "item"))).scalars().all()
    return events, sorted((i.entity_id, i.state.get("quantity")) for i in items)


def _receive(client, auth, doc_id: str, received: list[dict], key: str | None = None):
    return client.post(f"/docs/{doc_id}/receive", headers=auth["headers"],
                       json={"location_id": "", "received_items": received, "idempotency_key": key})


def _refused(r, line: int) -> bool:
    return r.status_code == 422 and r.json().get("detail") == t("documents.err_line_tax_unset_receive", line=line)


@pytest.mark.parametrize("doc_type", ["purchase_order", "consignment_in"])
async def test_a_draft_refuses_goods_while_a_line_tax_is_unset_and_changes_nothing(client, session, doc_type):
    auth = await _auth_company(session)
    doc_id, received = await _draft(client, auth, doc_type, [None])
    before = await _footprint(session, auth)
    r = await _receive(client, auth, doc_id, received)
    assert _refused(r, 1), r.text
    assert await _footprint(session, auth) == before
    state = await _state(session, auth, doc_id)
    assert state["status"] == "draft" and not state.get("received_items")


@pytest.mark.parametrize("doc_type", ["purchase_order", "consignment_in"])
async def test_the_refused_receipt_goes_through_once_the_tax_is_chosen(client, session, doc_type):
    auth = await _auth_company(session)
    doc_id, received = await _draft(client, auth, doc_type, [None])
    assert _refused(await _receive(client, auth, doc_id, received), 1)
    lines = (await _state(session, auth, doc_id))["line_items"]
    lines[0]["taxes"] = []
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text
    r = await _receive(client, auth, doc_id, received)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, doc_id))["status"] == "received"


async def test_a_partial_receipt_is_refused_while_another_line_tax_is_unset(client, session):
    """Receiving only the taxed line still takes the order out of draft, stranding the other."""
    auth = await _auth_company(session)
    doc_id, received = await _draft(client, auth, "purchase_order", [[], None])
    before = await _footprint(session, auth)
    assert _refused(await _receive(client, auth, doc_id, received[:1]), 2)
    assert await _footprint(session, auth) == before


async def test_an_earlier_completed_receipt_replays_unrefused(client, session):
    """A receipt an earlier release already made answers its retry as it did, whatever the lines
    hold now."""
    auth = await _auth_company(session)
    doc_id, received = await _draft(client, auth, "purchase_order", [[]])
    key = uuid.uuid4().hex
    first = await _receive(client, auth, doc_id, received, key)
    assert first.status_code == 200, first.text
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc_id})
    row.state["line_items"][0]["taxes"] = None
    flag_modified(row, "state")
    await session.commit()
    before = await _footprint(session, auth)
    again = await _receive(client, auth, doc_id, received, key)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await _footprint(session, auth) == before


async def test_every_caller_of_the_shared_receipt_is_refused(client, session):
    """The import and a connected company's sync call record_receipt directly, past the route."""
    from celerp_docs.routes import ReceiveBody, ReceivedItem, _get_doc, _operation, record_receipt

    auth = await _auth_company(session)
    doc_id, received = await _draft(client, auth, "purchase_order", [None])
    before = await _footprint(session, auth)
    row = await _get_doc(session, auth["company_id"], doc_id, for_update=True)
    body = ReceiveBody(location_id="", received_items=[ReceivedItem.model_validate(x) for x in received])
    key, digest = _operation("receive", doc_id, body)
    with pytest.raises(HTTPException) as refused:
        await record_receipt(session, auth["company_id"], row, body, key, digest, role="owner",
                             settings={}, user=SimpleNamespace(id=auth["user_id"]))
    await session.rollback()
    assert refused.value.status_code == 422
    assert refused.value.detail == t("documents.err_line_tax_unset_receive", line=1)
    assert await _footprint(session, auth) == before
