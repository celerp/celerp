# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""POST /items/metadata: bulk item-metadata read for list/doc/audit renderers.

The endpoint returns, per requested entity_id, the same visibility-filtered flat
item dict that GET /items/{entity_id} returns (minus the sold_price enrichment
lists never consume). It is a read gated by the router-level authentication only,
with company_id derived server-side from the JWT, and it must reproduce the exact
per-category, role-based field/cost visibility of the per-item route.
"""

from __future__ import annotations

import uuid

import pytest

from test_helpers import (
    register_admin,
    invite_user,
    create_item,
    default_location_id,
    create_location,
)


async def _admin_ctx(client):
    tok = await register_admin(client)
    h = {"Authorization": f"Bearer {tok}"}
    loc = await default_location_id(client, h)
    return h, loc


async def _set_company_settings(session, patch: dict) -> None:
    """Merge *patch* into the (single) company's settings via the session.

    Reassigns the whole settings dict so the JSON column is marked dirty.
    """
    from sqlalchemy import select
    from celerp.models.company import Company

    co = (await session.execute(select(Company))).scalars().first()
    merged = dict(co.settings or {})
    merged.update(patch)
    co.settings = merged
    await session.flush()


@pytest.mark.asyncio
async def test_items_metadata_single_query(client):
    """A single POST returns one entry per requested id; the flat shape matches
    GET /items/{id} for the same item (minus sold_price)."""
    h, loc = await _admin_ctx(client)
    id1 = await create_item(client, h, loc, sku="META-1")
    id2 = await create_item(client, h, loc, sku="META-2")

    r = await client.post("/items/metadata", json={"entity_ids": [id1, id2]}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert set(items.keys()) == {id1, id2}

    single = (await client.get(f"/items/{id1}", headers=h)).json()
    single.pop("sold_price", None)
    assert items[id1] == single


@pytest.mark.asyncio
async def test_items_metadata_preserves_visibility(client, session):
    """For a restricted role, the bulk result carries the SAME field/cost set as
    GET /items/{id} for the same item. An operator lacks view_inventory_costs by
    default, so cost keys are stripped identically on both paths."""
    h, loc = await _admin_ctx(client)
    item_id = await create_item(client, h, loc, sku="VIS-1")

    op_tok = await invite_user(client, session, h, "operator@acme.example", "operator")
    op_h = {"Authorization": f"Bearer {op_tok}"}

    single = (await client.get(f"/items/{item_id}", headers=op_h)).json()
    single.pop("sold_price", None)

    r = await client.post("/items/metadata", json={"entity_ids": [item_id]}, headers=op_h)
    assert r.status_code == 200, r.text
    bulk = r.json()["items"][item_id]

    assert "cost_price" not in bulk and "cost_total" not in bulk
    assert bulk == single


@pytest.mark.asyncio
async def test_items_metadata_per_category_schema(client, session):
    """Two items in DIFFERENT categories share a custom field key that is restricted
    (visible_to_roles) in one category and unrestricted in the other. For a low role
    the key must be PRESENT for the unrestricted-category item and ABSENT for the
    restricted-category item, proving each item's OWN category schema is applied and
    never one shared schema or category=None (which would leak or over-strip)."""
    h, loc = await _admin_ctx(client)

    # secret_note is visible to managers+ under category "Restricted"; open to all
    # under category "Open". An operator (below manager) must see it only in "Open".
    field_restricted = {
        "key": "secret_note", "label": "Secret", "type": "text",
        "editable": True, "required": False, "options": [],
        "visible_to_roles": ["manager"], "position": 50, "show_in_table": True,
    }
    field_open = {**field_restricted, "visible_to_roles": []}
    await _set_company_settings(session, {
        "category_schemas": {
            "Restricted": [field_restricted],
            "Open": [field_open],
        }
    })

    id_restricted = await create_item(client, h, loc, sku="CAT-R")
    id_open = await create_item(client, h, loc, sku="CAT-O")
    assert (await client.patch(f"/items/{id_restricted}",
        json={"fields_changed": {"category": {"old": None, "new": "Restricted"},
                                 "secret_note": {"old": None, "new": "hush"}}},
        headers=h)).status_code == 200
    assert (await client.patch(f"/items/{id_open}",
        json={"fields_changed": {"category": {"old": None, "new": "Open"},
                                 "secret_note": {"old": None, "new": "shown"}}},
        headers=h)).status_code == 200

    op_tok = await invite_user(client, session, h, "op2@acme.example", "operator")
    op_h = {"Authorization": f"Bearer {op_tok}"}

    r = await client.post("/items/metadata",
                          json={"entity_ids": [id_restricted, id_open]}, headers=op_h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert "secret_note" not in items[id_restricted], (
        "restricted-category field must be stripped for the low role")
    assert items[id_open].get("secret_note") == "shown", (
        "unrestricted-category field must survive for the low role")


@pytest.mark.asyncio
async def test_items_metadata_strips_derived_companions(client, session):
    """A derived key must be stripped for a role denied its SOURCE field, exactly as
    GET /items/{id} does. location_id mirrors the schema field location_name (a denied
    role could otherwise resolve the hidden location via its id), and qty_each is
    derived from quantity + pieces. Both dependencies live in the inventory router's
    DERIVED_FIELD_DEPS, which the bulk endpoint must hand to apply_field_visibility just
    like the per-item route; without it the bulk read leaks location_id and qty_each to
    a role that cannot see location_name or quantity."""
    h, loc = await _admin_ctx(client)

    # Restrict location_name and quantity to managers+ under one category. Their derived
    # companions (location_id, qty_each) must fall with them.
    def _floor(key, label):
        return {"key": key, "label": label, "type": "text" if key == "location_name" else "number",
                "editable": key != "location_name", "required": False, "options": [],
                "visible_to_roles": ["admin", "manager"], "position": 5, "show_in_table": True}
    await _set_company_settings(session, {
        "category_schemas": {"Locked": [_floor("location_name", "Location"), _floor("quantity", "Qty")]}
    })

    item_id = await create_item(client, h, loc, sku="DERIV-1")
    assert (await client.patch(f"/items/{item_id}",
        json={"fields_changed": {"category": {"old": None, "new": "Locked"}}},
        headers=h)).status_code == 200

    mgr_tok = await invite_user(client, session, h, "mgr@acme.example", "manager")
    mgr_h = {"Authorization": f"Bearer {mgr_tok}"}
    op_tok = await invite_user(client, session, h, "op3@acme.example", "operator")
    op_h = {"Authorization": f"Bearer {op_tok}"}

    # Manager is at/above the floor: source fields and their derived companions survive.
    mgr_bulk = (await client.post("/items/metadata",
        json={"entity_ids": [item_id]}, headers=mgr_h)).json()["items"][item_id]
    assert mgr_bulk.get("location_id") == loc
    assert "qty_each" in mgr_bulk

    # Operator is below the floor: source fields AND derived companions are stripped.
    op_bulk = (await client.post("/items/metadata",
        json={"entity_ids": [item_id]}, headers=op_h)).json()["items"][item_id]
    assert "location_name" not in op_bulk and "location_id" not in op_bulk, (
        "location_id must be stripped when location_name is hidden")
    assert "quantity" not in op_bulk and "qty_each" not in op_bulk, (
        "qty_each must be stripped when quantity is hidden")

    # Parity: the bulk entry matches GET /items/{id} for the same role (minus sold_price).
    op_single = (await client.get(f"/items/{item_id}", headers=op_h)).json()
    op_single.pop("sold_price", None)
    assert op_bulk == op_single


@pytest.mark.asyncio
async def test_items_metadata_dedup(client):
    """Duplicate entity_ids collapse to one result each (map keyed by id)."""
    h, loc = await _admin_ctx(client)
    item_id = await create_item(client, h, loc, sku="DUP-1")

    r = await client.post("/items/metadata",
                          json={"entity_ids": [item_id, item_id, item_id]}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert list(items.keys()) == [item_id]


@pytest.mark.asyncio
async def test_items_metadata_unknown_ids_absent(client):
    """Unknown ids are simply absent from the result map: no error, no fabricated
    entry."""
    h, loc = await _admin_ctx(client)
    known = await create_item(client, h, loc, sku="KNOWN-1")
    missing = "item:does-not-exist"

    r = await client.post("/items/metadata",
                          json={"entity_ids": [known, missing]}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert known in items
    assert missing not in items


@pytest.mark.asyncio
async def test_items_metadata_validation(client):
    """Empty list and an over-max list are rejected function-level with 422."""
    h, _ = await _admin_ctx(client)

    r_empty = await client.post("/items/metadata", json={"entity_ids": []}, headers=h)
    assert r_empty.status_code == 422, r_empty.text

    from celerp_inventory.routes import MAX_ITEMS_METADATA
    over = [f"item:{i}" for i in range(MAX_ITEMS_METADATA + 1)]
    r_over = await client.post("/items/metadata", json={"entity_ids": over}, headers=h)
    assert r_over.status_code == 422, r_over.text


@pytest.mark.asyncio
async def test_items_metadata_requires_auth(client):
    """An unauthenticated request is rejected by the router-level auth dependency."""
    r = await client.post("/items/metadata", json={"entity_ids": ["item:x"]})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_items_metadata_returns_only_item_projections(client, session):
    """The endpoint returns item projections only. Journal-entry, document, and
    contact projections share the (company_id, entity_id) table and carry
    predictable ids (e.g. je:auto:{doc}:fin), so a caller lacking financial
    permissions could otherwise name one and read accounting state. The query is
    scoped to entity_type == 'item', so a non-item id is simply absent."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from celerp.models.company import Company
    from celerp.models.projections import Projection

    h, loc = await _admin_ctx(client)
    item_id = await create_item(client, h, loc, sku="SEC-1")

    company_id = (await session.execute(select(Company))).scalars().first().id
    now = datetime.now(timezone.utc)
    je_id = "je:auto:doc-abc:fin:5000|rcv"
    doc_id = "doc:secret-invoice"
    contact_id = "contact:supplier-1"
    for eid, etype, state in [
        (je_id, "je", {"debit_account": "1200", "credit_account": "4000", "amount_cents": 5000}),
        (doc_id, "doc", {"doc_type": "invoice", "status": "issued", "total": 999.0}),
        (contact_id, "contact", {"name": "Confidential Supplier"}),
    ]:
        session.add(Projection(company_id=company_id, entity_id=eid, entity_type=etype,
                               state=state, version=1, updated_at=now))
    await session.commit()

    r = await client.post("/items/metadata",
                          json={"entity_ids": [item_id, je_id, doc_id, contact_id]}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    leaked = set(items) - {item_id}
    assert leaked == set(), f"non-item projections leaked through /items/metadata: {leaked}"

    # The pre-existing single-item GET must refuse a non-item id the same way,
    # never flattening a journal entry or document into an item response.
    assert (await client.get(f"/items/{je_id}", headers=h)).status_code == 404
    assert (await client.get(f"/items/{doc_id}", headers=h)).status_code == 404
    assert (await client.get(f"/items/{item_id}", headers=h)).status_code == 200
    # The sibling item-only read (reorder suggestion) refuses a non-item id too.
    assert (await client.get(f"/items/{je_id}/reorder-suggestion", headers=h)).status_code == 404
    assert (await client.get(f"/items/{item_id}/reorder-suggestion", headers=h)).status_code == 200
