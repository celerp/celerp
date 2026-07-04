# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for https://github.com/celerp/celerp/issues/205.

`pieces` must be stored in **`attributes["pieces"]`** in every case, never at top-level
(`state["pieces"]`). `attributes["pieces"]` is the single canonical location; the `item.updated`
projection already normalizes there, but the create projection (`item.created` / `item.snapshot`)
historically kept whatever the producer put in the payload verbatim.

Three producers put `pieces` at top-level; each gets one regression test asserting the STORED
projection state carries the count under `attributes` and NOT at top-level:

1. **API / script** — ``POST /items`` with a top-level ``pieces`` (accepted via ``extra="allow"``).
2. **Batch import** — a CIF import record whose ``data`` carries top-level ``pieces``.
3. **UI import** — the UI CSV/XLSX builder emits a record with top-level ``pieces`` *alongside* a
   populated ``attributes`` dict (see ``ui/routes/inventory.py``); the count must merge into that
   ``attributes`` dict without clobbering its sibling keys.

Each test FAILS before the fix (pieces left top-level) and passes once the create projection
normalizes ``pieces`` -> ``attributes``.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection


async def _token(client) -> str:
    email = f"pieces-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Pieces Co", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _state(session, entity_id: str) -> dict:
    """Return the RAW stored projection state (not the flattened read model)."""
    row = (await session.execute(
        select(Projection).where(Projection.entity_id == entity_id)
    )).scalar_one()
    return dict(row.state)


def _assert_pieces_in_attributes(state: dict, expected: int) -> None:
    """`pieces` lives under attributes and NOT at top-level."""
    assert "pieces" not in state, f"pieces must not be stored top-level, got state['pieces']={state.get('pieces')!r}"
    attrs = state.get("attributes") or {}
    assert "pieces" in attrs, f"pieces must be stored under attributes, got attributes={attrs!r}"
    assert int(float(attrs["pieces"])) == expected, f"expected attributes.pieces={expected}, got {attrs['pieces']!r}"


# ── Scenario 1: API / POST /items ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_items_stores_pieces_in_attributes(client, session) -> None:
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    r = await client.post(
        "/items",
        json={
            "sku": "API-1", "name": "API stone", "quantity": 45.0, "sell_by": "carat",
            "category": "colored_stone", "pieces": 3,  # top-level extra field (extra="allow")
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    _assert_pieces_in_attributes(await _state(session, r.json()["id"]), 3)


# ── Scenario 2: Batch import (bare CIF record) ────────────────────────────────
@pytest.mark.asyncio
async def test_batch_import_stores_pieces_in_attributes(client, session) -> None:
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    entity_id = f"item:{uuid.uuid4()}"
    r = await client.post(
        "/items/import/batch",
        json={"records": [{
            "entity_id": entity_id,
            "event_type": "item.created",
            "data": {  # top-level pieces, exactly as a CIF record carries it
                "sku": "IMP-1", "name": "Imported stone", "quantity": 46.0,
                "sell_by": "carat", "pieces": 4,
            },
            "source": "csv_import",
            "idempotency_key": f"csv:item:{uuid.uuid4().hex}",
        }]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.text
    _assert_pieces_in_attributes(await _state(session, entity_id), 4)


# ── Scenario 3: UI import (top-level pieces + populated attributes dict) ───────
@pytest.mark.asyncio
async def test_ui_import_record_stores_pieces_in_attributes(client, session) -> None:
    """Mirrors the exact record shape the UI CSV/XLSX builder emits (ui/routes/inventory.py):
    top-level ``pieces`` next to a separate ``attributes`` dict. The count must merge INTO that
    attributes dict, leaving its sibling keys intact."""
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    entity_id = f"item:{uuid.uuid4()}"
    r = await client.post(
        "/items/import/batch",
        json={"records": [{
            "entity_id": entity_id,
            "event_type": "item.created",
            "data": {
                "sku": "UI-1", "name": "UI stone", "quantity": 100.0, "sell_by": "carat",
                "pieces": 5,                       # UI builder puts pieces top-level ...
                "attributes": {"color": "green"},  # ... alongside the attributes dict
            },
            "source": "csv_import",
            "idempotency_key": f"csv:item:{uuid.uuid4().hex}",
        }]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.text
    state = await _state(session, entity_id)
    _assert_pieces_in_attributes(state, 5)
    # sibling attribute must survive the normalization
    assert (state.get("attributes") or {}).get("color") == "green", state.get("attributes")