# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests: category attributes must be stored under ``attributes``, never top-level.

`attributes["<key>"]` is the canonical location. `item.updated` already normalizes `pieces` there,
but every OTHER attribute set via a **field edit** is written to the TOP of item state
(`state["grade"]`), and `POST /items` keeps an arbitrary top-level attribute key top-level too
(`ItemCreate` accepts extra fields). Import already does the right thing — the UI import builder nests
non-core / non-price columns under `attributes` — so it is intentionally NOT covered here.

Each test asserts the RAW stored projection state carries the attribute under `attributes` and NOT at
top-level. They FAIL today (value left top-level) and pass once the write paths normalize non-core,
non-price fields into `attributes` (generalizing the `pieces` handling).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection


async def _token(client) -> str:
    email = f"attr-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Attr Co", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _state(session, entity_id: str) -> dict:
    """Return the RAW stored projection state (not the flattened read model)."""
    row = (await session.execute(
        select(Projection).where(Projection.entity_id == entity_id)
    )).scalar_one()
    return dict(row.state)


def _assert_attr_nested(state: dict, key: str, expected) -> None:
    """`key` lives under attributes and NOT at top-level."""
    assert key not in state, f"{key!r} must not be stored top-level, got state[{key!r}]={state.get(key)!r}"
    attrs = state.get("attributes") or {}
    assert attrs.get(key) == expected, f"expected attributes[{key!r}]={expected!r}, got {attrs.get(key)!r}"


# ── Scenario 1: field edit (the reproduction) ─────────────────────────────────
@pytest.mark.asyncio
async def test_field_edit_stores_attribute_in_attributes(client, session) -> None:
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    r = await client.post(
        "/items",
        json={"sku": "FE-1", "name": "Bag", "quantity": 1, "sell_by": "piece", "category": "DD"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    # Set a category attribute via a FIELD EDIT — the normal UI path.
    r2 = await client.patch(
        f"/items/{eid}",
        json={"fields_changed": {"grade": {"old": None, "new": "A"}}},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    _assert_attr_nested(await _state(session, eid), "grade", "A")


# ── Scenario 2: POST /items with a top-level attribute (extra="allow") ─────────
@pytest.mark.asyncio
async def test_post_items_top_level_attribute_is_nested(client, session) -> None:
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    r = await client.post(
        "/items",
        json={
            "sku": "PC-1", "name": "Bag", "quantity": 1, "sell_by": "piece",
            "category": "DD", "grade": "A",   # top-level extra field (accepted via extra="allow")
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    _assert_attr_nested(await _state(session, r.json()["id"]), "grade", "A")


# ── Scenario 3: clearing a field-edited attribute removes it from attributes ───
@pytest.mark.asyncio
async def test_field_edit_clear_removes_attribute_everywhere(client, session) -> None:
    """Clearing the attribute must leave it unset in BOTH locations (no orphan top-level or nested)."""
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    r = await client.post(
        "/items",
        json={"sku": "FC-1", "name": "Bag", "quantity": 1, "sell_by": "piece", "category": "DD"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    await client.patch(f"/items/{eid}", json={"fields_changed": {"grade": {"old": None, "new": "A"}}}, headers=headers)
    # Now clear it.
    r3 = await client.patch(f"/items/{eid}", json={"fields_changed": {"grade": {"old": "A", "new": None}}}, headers=headers)
    assert r3.status_code == 200, r3.text
    state = await _state(session, eid)
    assert "grade" not in state, f"cleared grade must not remain top-level: {state.get('grade')!r}"
    assert "grade" not in (state.get("attributes") or {}), f"cleared grade must not remain nested: {state.get('attributes')}"