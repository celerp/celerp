# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Per-line shipped-state labels on a memo, derived from the event log.

get_doc attaches a `shipped_label` to each line: "Returned" (shipped then
reversed), "Kept/Sold" (shipped and not since reversed), "Not shipped" (never
fulfilled for this memo). A read error in the derivation degrades to no label
(the raw status badge stands), never a 500.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@memolabel.test"
    r = await client.post("/auth/register", json={"company_name": "MemoLabel Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, h, sku) -> str:
    r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _memo(client, h, item_ids: list[str]) -> str:
    line_items = [{"entity_id": i, "sku": f"L-{n}", "name": f"L-{n}", "quantity": 1, "unit_price": 10, "sell_by": "piece"}
                  for n, i in enumerate(item_ids)]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _label_for(doc: dict, eid: str):
    for li in doc.get("line_items") or []:
        if (li.get("entity_id") or li.get("item_id")) == eid:
            return li.get("shipped_label")
    raise AssertionError(f"line {eid} not found")


@pytest.mark.asyncio
async def test_line_labels_returned_vs_not_shipped(client):
    token = await _register(client)
    h = _h(token)
    shipped_kept = await _item(client, h, "LB-KEPT")
    returned = await _item(client, h, "LB-RET")
    never = await _item(client, h, "LB-NONE")
    memo = await _memo(client, h, [shipped_kept, returned, never])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200

    # Ship two lines; leave the third unshipped.
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [shipped_kept, returned]})).status_code == 200
    # Return one of the shipped lines.
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [returned]})).status_code == 200

    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert _label_for(doc, shipped_kept) == "Kept/Sold"
    assert _label_for(doc, returned) == "Returned"
    assert _label_for(doc, never) == "Not shipped"


@pytest.mark.asyncio
async def test_line_label_derivation_degrades(client, monkeypatch):
    """A read error inside the label derivation leaves lines without a
    shipped_label rather than 500ing the whole doc payload."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "LB-DEG")
    memo = await _memo(client, h, [a])
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200

    from celerp_docs import routes as _routes

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(_routes, "_derive_shipped_labels", _boom)

    r = await client.get(f"/docs/{memo}", headers=h)
    assert r.status_code == 200, r.text
    doc = r.json()
    for li in doc.get("line_items") or []:
        assert "shipped_label" not in li
