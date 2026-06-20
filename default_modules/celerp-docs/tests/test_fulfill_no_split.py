# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fulfillment must respect 'no split' even when allow_splitting was never set.

A partial draw on an invoice line splits the parcel (split-on-fulfill). The
fulfillment guard must block that split for a parcel that isn't explicitly
splittable — a missing/None allow_splitting (e.g. older imports) must not bypass
the gate, the same as a manual split."""
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
async def test_partial_fulfillment_blocked_when_allow_splitting_missing(client, session):
    token = await _register(client)
    h = _h(token)

    # Parcel with 10 in stock.
    item = (await client.post("/items", headers=h, json={
        "sku": "PARCEL-NS", "name": "Parcel", "quantity": 10, "sell_by": "piece"})).json()["id"]

    # Remove allow_splitting from the parcel's projection state (as older imports left it).
    row = (await session.execute(select(Projection).where(Projection.entity_id == item))).scalar_one()
    new_state = dict(row.state)
    new_state.pop("allow_splitting", None)
    row.state = new_state
    await session.commit()

    # Invoice drawing a PARTIAL 3 of the 10 → fulfillment would split off a child.
    doc = (await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "PARCEL-NS", "name": "Parcel", "quantity": 3, "unit_price": 5, "sell_by": "piece"}],
    })).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200

    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=h, json={"line_entity_ids": [item]})
    assert r.status_code == 409, (
        f"partial fulfillment of a no-split parcel must be blocked, got {r.status_code}: {r.text}"
    )
    assert "Allow Splitting" in str(r.json().get("detail", ""))

    # And the parcel must be untouched (not split).
    parcel = (await client.get(f"/items/{item}", headers=h)).json()
    assert float(parcel.get("quantity") or 0) == 10.0, f"parcel was split: qty={parcel.get('quantity')}"