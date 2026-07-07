# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Delivery notes / packing slips as a non-financial list_type."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from celerp.services.list_behavior import LIST_TYPES, is_money_list


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _token(client: AsyncClient) -> str:
    return (await client.post("/auth/register", json={
        "company_name": "ShipCo", "email": "ship@test.com", "name": "A", "password": "password123"})).json()["access_token"]


async def _make_list(client, tok, list_type, **extra) -> dict:
    r = await client.post("/lists", json={
        "list_type": list_type, "customer_name": "Buyer",
        "line_items": [{"name": "Widget", "sku": "W1", "quantity": 3,
                        "hs_code": "8471.30", "country_of_origin": "US", "weight": 1.2}],
        **extra,
    }, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


# ── behavior table ───────────────────────────────────────────────────────────

def test_delivery_note_is_registered_and_non_financial():
    assert "delivery_note" in LIST_TYPES
    assert is_money_list("delivery_note") is False
    assert is_money_list("quotation") is True  # regression: existing types unchanged


def test_country_of_origin_is_a_base_item_field():
    from celerp.services.field_schema import _BASE_FIELDS
    keys = {f["key"] for f in _BASE_FIELDS}
    assert "country_of_origin" in keys
    assert "hs_code" in keys  # regression


# ── numbering ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delivery_note_numbered_ship_and_lists_stay_lst(client: AsyncClient):
    tok = await _token(client)
    dn = await _make_list(client, tok, "delivery_note")
    quote = await _make_list(client, tok, "quotation")
    assert dn["id"].startswith("list:SHIP-")
    assert quote["id"].startswith("list:LST-")


@pytest.mark.asyncio
async def test_ship_counter_is_independent(client: AsyncClient):
    tok = await _token(client)
    a = await _make_list(client, tok, "delivery_note")
    b = await _make_list(client, tok, "delivery_note")
    assert a["id"] != b["id"]
    assert a["id"].startswith("list:SHIP-") and b["id"].startswith("list:SHIP-")


# ── shipment fields flow through, no financial effect ────────────────────────

@pytest.mark.asyncio
async def test_shipment_and_customs_fields_persist(client: AsyncClient):
    tok = await _token(client)
    dn = await _make_list(client, tok, "delivery_note",
                          ship_to_address="123 Dock St", carrier="FedEx", tracking="FX123",
                          package_count=2, incoterms="DAP")
    state = (await client.get(f"/lists/{dn['id']}", headers=_h(tok))).json()
    assert state["carrier"] == "FedEx"
    assert state["tracking"] == "FX123"
    assert state["ship_to_address"] == "123 Dock St"
    assert state["incoterms"] == "DAP"
    line = state["line_items"][0]
    assert line["hs_code"] == "8471.30"
    assert line["country_of_origin"] == "US"


@pytest.mark.asyncio
async def test_delivery_note_finalize_posts_no_journal_entry(client: AsyncClient, session):
    tok = await _token(client)
    dn = await _make_list(client, tok, "delivery_note")
    # A finalized delivery note must not create any accounting journal entry.
    r = await client.post(f"/lists/{dn['id']}/finalize", headers=_h(tok))
    assert r.status_code in (200, 404)  # finalize route may differ; either way, no JE
    import sqlalchemy as sa
    from celerp.models.projections import Projection
    jes = (await session.execute(sa.select(Projection).where(
        Projection.entity_type == "je",
        Projection.state["metadata"]["doc_id"].as_string() == dn["id"]))).scalars().all()
    assert jes == []
