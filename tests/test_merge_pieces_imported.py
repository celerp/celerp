# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression test for https://github.com/celerp/celerp/issues/204.

Merging items must sum `pieces` regardless of WHERE each source stores the count:

- items created via the full-payload path (imports / ``POST /items`` with a top-level ``pieces``)
  keep the count at **top-level** (``state["pieces"]``);
- items created-then-edited in the UI keep it under **``attributes["pieces"]``**.

The merge reads ``attributes["pieces"]`` only (routes.py:2082), so it silently drops the count for
imported items. This test seeds two items with **top-level** pieces (the import path, mirroring
``data/test_items.csv``) and asserts the merge sums them. It FAILS against the current merge
(pieces dropped -> None) and passes once the merge resolves pieces via ``_read_pieces``
(top-level OR attributes).
"""
from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"imp-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Imported Gems", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _seed_imported(client, headers, sku: str, pieces, *, qty: float) -> str:
    """Create a carat-sold stone with ``pieces`` at TOP-LEVEL — the import / POST-payload path
    (NOT under ``attributes``). Mirrors the rows of data/test_items.csv."""
    r = await client.post(
        "/items",
        json={
            "sku": sku, "name": sku, "quantity": qty, "sell_by": "carat",
            "category": "colored_stone", "pieces": pieces,   # top-level, exactly like an import
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _pieces(client, headers, item_id: str):
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json().get("pieces")


async def _merge(client, headers, source_ids: list[str], target_id: str) -> str:
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": source_ids, "target_sku_from": target_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_merge_sums_pieces_for_imported_items(client) -> None:
    """A and B imported with top-level pieces=1 each -> merged pieces must be 2 (issue #204)."""
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    a = await _seed_imported(client, headers, "A", 1, qty=45.0)
    b = await _seed_imported(client, headers, "B", 1, qty=46.0)

    # Sanity: each seeded source really carries pieces=1 (proves the top-level count is stored/read).
    assert int(float(await _pieces(client, headers, a))) == 1
    assert int(float(await _pieces(client, headers, b))) == 1

    merged_id = await _merge(client, headers, [a, b], a)
    merged_pieces = await _pieces(client, headers, merged_id)

    assert merged_pieces is not None and int(float(merged_pieces)) == 2, (
        f"imported items' pieces must sum on merge (expected 2), got {merged_pieces!r}"
    )