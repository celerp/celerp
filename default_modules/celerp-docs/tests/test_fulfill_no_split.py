# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fulfillment treats an unset allow_splitting as splittable.

A partial draw on an invoice line splits the parcel (split-on-fulfill). A
missing/None allow_splitting (e.g. older imports) is splittable by default, so a
partial fulfillment carves a child and leaves the remainder on the mother; only
an explicit False blocks the split."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@nosplit.test"
    r = await client.post("/auth/register", json={"company_name": "NoSplit Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_partial_fulfillment_allowed_when_allow_splitting_missing(client, session):
    token = await _register(client)
    h = _h(token)

    # Parcel with 10 in stock.
    item = (await client.post("/items", headers=h, json={
        "status": "available", "sku": "PARCEL-NS", "name": "Parcel", "quantity": 10, "sell_by": "piece"})).json()["id"]

    # Remove allow_splitting from the parcel's projection state (as older imports left it).
    row = (await session.execute(select(Projection).where(Projection.entity_id == item))).scalar_one()
    new_state = dict(row.state)
    new_state.pop("allow_splitting", None)
    row.state = new_state
    await session.commit()

    # Invoice drawing a PARTIAL 3 of the 10 → fulfillment splits off a child.
    doc = (await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "PARCEL-NS", "name": "Parcel", "quantity": 3, "unit_price": 5, "sell_by": "piece"}],
    })).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=h, json={"line_entity_ids": [item]})
    assert r.status_code == 200, (
        f"partial fulfillment of an unset-split parcel must be allowed, got {r.status_code}: {r.text}"
    )

    # And the mother parcel keeps the un-drawn remainder (10 - 3 = 7).
    parcel = (await client.get(f"/items/{item}", headers=h)).json()
    assert float(parcel.get("quantity") or 0) == 7.0, f"mother remainder wrong: qty={parcel.get('quantity')}"