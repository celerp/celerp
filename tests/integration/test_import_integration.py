# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Integration tests — verify the projection engine computes correct totals after real import.

All truth anchors live in conftest.py. Tests are thin: call API, check numbers.
Requires: dev.db pre-populated by the importer.
"""
from __future__ import annotations

import pytest

from .conftest import (
    AR_OUTSTANDING,
    CONTACT_COUNT,
    COST_TOTAL,
    INVOICE_COUNT_NON_VOID,
    ITEM_COUNT,
    MEMO_TOTAL,
    RETAIL_TOTAL,
    TOLERANCE,
    WHOLESALE_TOTAL,
)


async def test_item_count(api):
    """Projection engine must report exactly ITEM_COUNT items for this company."""
    r = await api.get("/items/valuation")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["item_count"] == ITEM_COUNT, f"Expected {ITEM_COUNT}, got {data['item_count']}"


async def test_inventory_cost(api):
    r = await api.get("/items/valuation")
    assert r.status_code == 200, r.text
    cost = r.json()["cost_total"]
    assert abs(cost - COST_TOTAL) <= TOLERANCE, f"Cost diff: {cost - COST_TOTAL:.2f}"


async def test_inventory_wholesale(api):
    r = await api.get("/items/valuation")
    assert r.status_code == 200, r.text
    ws = r.json()["wholesale_total"]
    assert abs(ws - WHOLESALE_TOTAL) <= TOLERANCE, f"Wholesale diff: {ws - WHOLESALE_TOTAL:.2f}"


async def test_inventory_retail(api):
    r = await api.get("/items/valuation")
    assert r.status_code == 200, r.text
    rt = r.json()["retail_total"]
    assert abs(rt - RETAIL_TOTAL) <= TOLERANCE, f"Retail diff: {rt - RETAIL_TOTAL:.2f}"


async def test_invoice_count(api):
    r = await api.get("/docs/summary")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["non_void_count"] == INVOICE_COUNT_NON_VOID, (
        f"Expected {INVOICE_COUNT_NON_VOID} non-void invoices, got {data['non_void_count']}"
    )


async def test_ar_outstanding(api):
    r = await api.get("/docs/summary")
    assert r.status_code == 200, r.text
    outstanding = r.json()["ar_outstanding"]
    assert abs(outstanding - AR_OUTSTANDING) <= TOLERANCE, (
        f"AR outstanding diff: {outstanding - AR_OUTSTANDING:.2f}"
    )


async def test_contact_count(api):
    r = await api.get("/crm/contacts")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)

    # Determinism: business journeys may have added extra contacts to the DB.
    # This anchored integration suite only requires that at least the imported
    # dataset is present (CONTACT_COUNT), not an exact total.
    total = data.get("total")
    assert isinstance(total, int)
    assert total >= CONTACT_COUNT, f"Expected >= {CONTACT_COUNT} contacts, got {total}"


async def test_memo_total(api):
    """All memos total (face value at creation) must match truth anchor."""
    r = await api.get("/crm/memos/summary")
    assert r.status_code == 200, r.text
    data = r.json()
    all_total = data["all_total"]
    assert abs(all_total - MEMO_TOTAL) <= TOLERANCE, (
        f"Memo total diff: {all_total - MEMO_TOTAL:.2f}"
    )
