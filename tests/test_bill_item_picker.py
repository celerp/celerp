# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Picking a catalog item on a bill sets how the line is received from the item's type.

A service or other non-stock item comes in as an expense and a stocked item comes in
as stock. The user can still change it on the line.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fasthtml.common import to_xml

from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_form_submitted_twice import _Request, routes  # noqa: F401  (routes is a fixture)


@pytest.mark.parametrize(("inventory_type", "receive_as"), [
    ("service", "expense"),
    ("non_stocked", "expense"),
    ("freight", "expense"),
    ("stocked", "stock"),
    ("component", "stock"),
])
@pytest.mark.asyncio
async def test_a_picked_catalog_item_is_received_as_its_type(client, auth, routes, inventory_type, receive_as):
    name = f"Pick {uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": f"PK-{uuid.uuid4().hex[:6]}", "name": name, "quantity": 1,
        "sell_by": "piece", "inventory_type": inventory_type})
    assert r.status_code == 200, r.text

    resp = await routes[("get", "/docs/catalog-search")](_Request(query={"q": name, "doc_type": "bill"}))

    [picked] = json.loads(resp.body)
    assert picked["receive_as"] == receive_as


@pytest.mark.asyncio
async def test_the_bill_line_takes_the_picked_items_receive_as(client, auth):
    from ui.routes import documents
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "bill", "line_items": []})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=auth["headers"])).json()

    page = to_xml(documents._doc_detail(doc))

    assert "receiveAsEl.value = data.receive_as" in page
