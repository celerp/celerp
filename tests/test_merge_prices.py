# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for https://github.com/celerp/celerp/issues/199.

Merging items must reconcile money fields across the source items with an all-or-nothing rule,
operating on the source **TOTALS** (not unit prices):

- if **every** source has the value set  -> merged value = the SUM of the source totals;
- if **at least one** source is unset    -> merged value = None (unevaluated), never 0/dropped.

Two bugs this guards against:
- the non-target source's price being silently dropped (merged total = target_unit x merged_qty),
  e.g. total $50 + total $50,000 -> $100 instead of $50,050;
- a missing source price/cost being treated as 0, so the merged price can land below cost.

IMPORTANT — unit price vs total: `retail_price` / `wholesale_price` are *per-unit* prices (rates); the
business **total** is `unit_price x quantity`. `cost_total` is already a total. These tests are written
purely in **totals**: the seed helper converts a desired total into the stored unit price (÷ qty), and
the asserts read totals back (`unit_price x quantity`). Quantities are >1 on purpose so unit != total
and the "sum the totals" requirement is unambiguous.

They FAIL against the current merge (prices copied from target only; cost summed with missing-as-0)
and pass once the merge sums totals / drops to None.
"""

from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"books-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Books & Media", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _seed(client, headers, sku: str, *, qty: float,
                retail_total=None, wholesale_total=None, cost_total=None) -> str:
    """Create a piece-sold `book` item from desired **TOTAL** amounts.

    retail/wholesale are stored per-unit, so we convert: unit = total / qty. cost_total is a total.
    """
    payload: dict = {"sku": sku, "name": sku, "quantity": qty, "sell_by": "piece", "category": "book"}
    if retail_total is not None:
        payload["retail_price"] = retail_total / qty
    if wholesale_total is not None:
        payload["wholesale_price"] = wholesale_total / qty
    if cost_total is not None:
        payload["cost_total"] = cost_total
    r = await client.post("/items", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, headers, source_ids: list[str], target_id: str) -> str:
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": source_ids, "target_sku_from": target_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _get(client, headers, item_id: str) -> dict:
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _unit_total(item: dict, unit_field: str):
    """Business total for a per-unit-priced field = unit_price × quantity (None if unset)."""
    unit = item.get(unit_field)
    if unit is None:
        return None
    return round(float(unit) * float(item.get("quantity") or 0), 2)


# ── All sources priced → merged TOTAL is the SUM of the source totals ───────────────────────────

@pytest.mark.asyncio
async def test_merge_sums_retail_total_when_all_set(client):
    """Scenario B: total $50 (target) + total $50,000 → merged retail total $50,050, not $100."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a = await _seed(client, h, "BOOK-A", qty=2, retail_total=50)       # unit 25 × 2 = 50
    b = await _seed(client, h, "BOOK-B", qty=2, retail_total=50000)    # unit 25000 × 2 = 50000
    merged = await _get(client, h, await _merge(client, h, [a, b], a))
    assert _unit_total(merged, "retail_price") == 50050.0, f"got {_unit_total(merged, 'retail_price')!r}"


@pytest.mark.asyncio
async def test_merge_sums_wholesale_total_when_all_set(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a = await _seed(client, h, "BOOK-A", qty=2, wholesale_total=50)
    b = await _seed(client, h, "BOOK-B", qty=2, wholesale_total=50000)
    merged = await _get(client, h, await _merge(client, h, [a, b], a))
    assert _unit_total(merged, "wholesale_price") == 50050.0, f"got {_unit_total(merged, 'wholesale_price')!r}"


@pytest.mark.asyncio
async def test_merge_sums_cost_total_when_all_set(client):
    """Control: cost already sums when every source is costed (total $5 + total $20 → $25)."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    a = await _seed(client, h, "BOOK-A", qty=2, cost_total=5)
    b = await _seed(client, h, "BOOK-B", qty=2, cost_total=20)
    merged = await _get(client, h, await _merge(client, h, [a, b], a))
    assert merged.get("cost_total") is not None and round(float(merged["cost_total"]), 2) == 25.0


# ── At least one source unset → merged TOTAL is None (unevaluated), never 0 / target-only ────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["retail", "wholesale", "cost"])
async def test_merge_total_none_when_any_source_unset(client, kind):
    """Target HAS the value, the other source does NOT → merged must be None, not the target's value
    (Scenario A: a missing source price/cost must not be treated as 0)."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    seed_kw = {"retail": {"retail_total": 100}, "wholesale": {"wholesale_total": 100}, "cost": {"cost_total": 50}}[kind]
    read_field = {"retail": "retail_price", "wholesale": "wholesale_price", "cost": "cost_total"}[kind]
    a = await _seed(client, h, "BOOK-A", qty=2, **seed_kw)   # target, priced
    b = await _seed(client, h, "BOOK-B", qty=2)              # other source, unpriced
    merged = await _get(client, h, await _merge(client, h, [a, b], a))
    assert merged.get(read_field) is None, (
        f"{kind} total should be None when a source is unset, got {merged.get(read_field)!r}"
    )