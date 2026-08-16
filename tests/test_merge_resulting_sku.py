# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Issue #190: a merged item can take a custom SKU (keep A, keep B, or a new one).

The merged item is genuinely new, so its SKU is the target's by default but may
be any value the user types. SKU is a non-unique product-type (per-lot identity
is the barcode + entity_id), so no uniqueness check is applied - consistent with
the create/rename paths.
"""

from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"merge-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Merge Co", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _seed(client, headers, sku: str) -> str:
    r = await client.post(
        "/items",
        json={"sku": sku, "name": sku, "quantity": 5.0, "sell_by": "piece", "category": "widgets",
              "status": "available"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, headers, source_ids, target_id, **extra):
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": source_ids, "target_sku_from": target_id, **extra},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _sku(client, headers, item_id: str) -> str:
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json().get("sku")


@pytest.mark.asyncio
async def test_merge_defaults_to_target_sku(client):
    """No resulting_sku -> the merged item keeps the target source's SKU (today's behavior)."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a, b = await _seed(client, h, "AAA"), await _seed(client, h, "BBB")
    merged = await _merge(client, h, [a, b], b)  # target = B
    assert await _sku(client, h, merged) == "BBB"


@pytest.mark.asyncio
async def test_merge_uses_custom_sku(client):
    """A typed resulting_sku becomes the merged item's SKU, overriding the target's."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a, b = await _seed(client, h, "AAA"), await _seed(client, h, "BBB")
    merged = await _merge(client, h, [a, b], a, resulting_sku="MERGED-1")
    assert await _sku(client, h, merged) == "MERGED-1"


@pytest.mark.asyncio
async def test_merge_blank_custom_sku_falls_back_to_target(client):
    """A blank/whitespace resulting_sku is ignored -> falls back to the target SKU."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a, b = await _seed(client, h, "AAA"), await _seed(client, h, "BBB")
    merged = await _merge(client, h, [a, b], a, resulting_sku="   ")
    assert await _sku(client, h, merged) == "AAA"  # target = A


@pytest.mark.asyncio
async def test_merged_into_sku_matches_custom(client):
    """The sources' 'merged_into_sku' deactivation marker uses the custom SKU too,
    so history points at the SKU the merged item actually has."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a, b = await _seed(client, h, "AAA"), await _seed(client, h, "BBB")
    await _merge(client, h, [a, b], a, resulting_sku="NEWSKU")

    rows = (await client.get("/ledger", params={"entity_id": a, "limit": 100}, headers=h)).json()["items"]
    dea = [e for e in rows if e["event_type"] == "item.source_deactivated"]
    assert dea and dea[0]["data"]["merged_into_sku"] == "NEWSKU"
