# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""List line unit identity and reference integrity: for a List line linked by item_id the STORED sell_by
governs the per-unit rule and a submitted sell_by is ignored, so a stocked piece item cannot be smuggled
past the positive/decimal rule by submitting a service unit; and a linked item_id that does not resolve to
a real item is rejected 422 as an invalid reference rather than silently falling back to the free-text
finiteness gate.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@listref.test"
    r = await client.post("/auth/register", json={"company_name": "List Ref Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t, name="Warehouse A") -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, t, sku, *, loc, qty, barcode=None, sell_by="piece", allow_splitting=False) -> str:
    body = {"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by,
            "location_id": loc, "inventory_type": "stocked", "allow_splitting": allow_splitting}
    if barcode:
        body["barcode"] = barcode
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _quotation(client, t) -> str:
    r = await client.post("/lists", headers=_h(t), json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _state(client, t, list_id) -> dict:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()


@pytest.mark.asyncio
async def test_list_linked_item_sell_by_wins_over_submitted_spoof(client):
    """A List line linking a stocked piece item but submitting sell_by='service' and quantity=0 is
    rejected 422: the stored piece rule (require_positive) rejects zero and the submitted 'service' must be
    ignored for a linked line. Red at merge-base: the validator resolves the unit as
    submitted-or-stored, so the submitted 'service' wins, qty=0 bypasses the positive rule, and the list
    is created (200)."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "SPF-1", loc=loc, qty=20, sell_by="piece")

    r = await client.post("/lists", headers=_h(t), json={
        "list_type": "quotation",
        "line_items": [{"item_id": iid, "sku": "SPF-1", "name": "SPF-1", "sell_by": "service", "quantity": 0}],
    })
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_list_fractional_piece_rejected_via_stored_unit(client):
    """A List line linking a stocked piece item but submitting sell_by='service' and quantity=2.5 is
    rejected 422: a piece carries zero decimals, and the stored unit governs for a linked line. Red at
    merge-base: the submitted 'service' unit wins, precision is not enforced, and the fractional quantity
    is accepted (200)."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "FRC-1", loc=loc, qty=20, sell_by="piece")

    r = await client.post("/lists", headers=_h(t), json={
        "list_type": "quotation",
        "line_items": [{"item_id": iid, "sku": "FRC-1", "name": "FRC-1", "sell_by": "service", "quantity": 2.5}],
    })
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_list_unresolvable_item_id_422(client):
    """A List line whose item_id is syntactically valid but resolves to no real item is rejected 422 with
    an 'invalid_reference' body, even with an otherwise-fine quantity. Red at merge-base: the validator
    does no existence check on linked ids, so the unresolvable id silently drops to the free-text
    finiteness gate, the quantity passes, and the list is created (200) with no invalid_reference."""
    t = await _register(client)
    await _location(client, t)
    ghost = f"item:{uuid.uuid4()}"

    r = await client.post("/lists", headers=_h(t), json={
        "list_type": "quotation",
        "line_items": [{"item_id": ghost, "sku": "GHOST", "name": "GHOST", "quantity": 3}],
    })
    assert r.status_code == 422, r.text
    assert "invalid_reference" in r.text
