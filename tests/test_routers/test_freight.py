# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Freight inventory type (P1): a freight-typed item is a landed-cost charge line - non-stock,
carries a landed_cost_kind, and validates the kind. See context/2026-0614-freight-tracking-plan.md."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@frt.test"
    r = await client.post("/auth/register", json={"company_name": "Frt Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_create_freight_item_with_kind(client):
    t = await _register(client)
    r = await client.post("/items", headers=_h(t), json={
        "sku": "SEA-FREIGHT", "name": "Sea freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/items/{r.json()['id']}", headers=_h(t))).json()
    assert state["inventory_type"] == "freight"
    assert state["landed_cost_kind"] == "freight"


@pytest.mark.asyncio
async def test_import_vat_recoverable_flag_stored(client):
    t = await _register(client)
    r = await client.post("/items", headers=_h(t), json={
        "sku": "IMPORT-VAT", "name": "Import VAT", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "import_vat", "recoverable": True})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/items/{r.json()['id']}", headers=_h(t))).json()
    assert state["landed_cost_kind"] == "import_vat" and state["recoverable"] is True


@pytest.mark.asyncio
async def test_bad_landed_cost_kind_rejected(client):
    t = await _register(client)
    r = await client.post("/items", headers=_h(t), json={
        "sku": "BAD-KIND", "name": "x", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "tariff"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_bad_landed_cost_kind_rejected(client):
    t = await _register(client)
    iid = (await client.post("/items", headers=_h(t), json={
        "sku": "FRT-PATCH", "name": "x", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})).json()["id"]
    r = await client.patch(f"/items/{iid}", headers=_h(t),
                           json={"fields_changed": {"landed_cost_kind": {"old": "freight", "new": "nope"}}})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_freight_type_accepted_in_valid_set(client):
    """'freight' is a valid inventory_type alongside the existing ones."""
    t = await _register(client)
    for kind in ("stocked", "component", "non_stocked", "service", "freight"):
        r = await client.post("/items", headers=_h(t), json={
            "sku": f"TYPE-{kind}", "name": kind, "quantity": 0, "sell_by": "piece",
            "inventory_type": kind})
        assert r.status_code == 200, f"{kind}: {r.text}"
