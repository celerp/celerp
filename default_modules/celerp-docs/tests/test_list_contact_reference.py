# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A List customer is validated and priced by the same rules as a document contact."""
from __future__ import annotations

import pytest

from test_helpers import grant_permission, perm_setup


async def _contact(client, h: dict, name: str = "Alice") -> str:
    r = await client.post("/crm/contacts", json={"name": name, "contact_type": "customer"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _quotation(client, h: dict, item_id: str | None = None) -> str:
    lines = []
    if item_id:
        lines = [{"item_id": item_id, "description": "Catalog", "quantity": 1,
                  "unit_price": 1, "line_total": 1}]
    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "line_items": lines})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _patch(client, h: dict, list_id: str, contact_id: str):
    return await client.patch(f"/lists/{list_id}", headers=h, json={
        "fields_changed": {"contact_id": {"old": None, "new": contact_id}},
    })


@pytest.mark.asyncio
async def test_list_rejects_deleted_contact(client, session):
    ctx = await perm_setup(client, session)
    h = ctx["admin_h"]
    cid = await _contact(client, h)
    r = await client.post("/crm/contacts/bulk/delete", headers=h, json={"contact_ids": [cid]})
    assert r.status_code == 200, r.text
    list_id = await _quotation(client, h)
    r = await _patch(client, h, list_id, cid)
    assert r.status_code == 422, r.text
    assert "deleted" in r.text
    assert (await client.get(f"/lists/{list_id}", headers=h)).json().get("contact_id") is None


@pytest.mark.asyncio
async def test_list_rejects_non_contact_local_record(client, session):
    ctx = await perm_setup(client, session)
    h = ctx["admin_h"]
    list_id = await _quotation(client, h)
    r = await _patch(client, h, list_id, ctx["item_id"])
    assert r.status_code == 422, r.text
    assert "non-contact" in r.text


@pytest.mark.asyncio
async def test_list_keeps_non_local_contact_reference_like_documents(client, session):
    ctx = await perm_setup(client, session)
    h = ctx["admin_h"]
    list_id = await _quotation(client, h)
    r = await _patch(client, h, list_id, "contact:imported-elsewhere")
    assert r.status_code == 200, r.text
    assert (await client.get(f"/lists/{list_id}", headers=h)).json()["contact_id"] == "contact:imported-elsewhere"


@pytest.mark.asyncio
async def test_contact_selection_cannot_bypass_sales_price_permission(client, session):
    ctx = await perm_setup(client, session)
    admin, operator = ctx["admin_h"], ctx["operator_h"]
    await grant_permission(client, admin, "set_sales_doc_prices", "manager")
    r = await client.patch(f"/items/{ctx['item_id']}", headers=admin,
                           json={"fields_changed": {"wholesale_price": {"old": None, "new": 42}}})
    assert r.status_code == 200, r.text
    list_id = await _quotation(client, admin, ctx["item_id"])
    contact_id = await _contact(client, admin)
    r = await client.patch(f"/crm/contacts/{contact_id}", headers=admin,
                           json={"fields_changed": {"price_list": {"old": None, "new": "Wholesale"}}})
    assert r.status_code == 200, r.text
    # The selection reprices at the customer's price list, so it is refused as a whole.
    r = await _patch(client, operator, list_id, contact_id)
    assert r.status_code == 403, r.text
    state = (await client.get(f"/lists/{list_id}", headers=admin)).json()
    assert state.get("contact_id") is None
    assert state["line_items"][0]["unit_price"] == 1
