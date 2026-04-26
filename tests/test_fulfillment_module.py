# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for warehousing: pick instruction badges and API endpoints."""

from __future__ import annotations

import pytest
pytest.importorskip("celerp_warehousing")

import uuid

import pytest
import pytest_asyncio

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.auth import create_access_token


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_loads():
    import importlib
    import sys
    mod = importlib.import_module("celerp_warehousing")
    m = mod.PLUGIN_MANIFEST
    assert m["name"] == "celerp-warehousing"
    assert m["default_enabled"] is True
    assert "pre_fulfill_hook" in m["slots"]
    assert m["depends_on"] == ["celerp-docs", "celerp-inventory"]


# ---------------------------------------------------------------------------
# UI renderers (kept: pick/stock badges)
# ---------------------------------------------------------------------------

class TestRenderPickInstructionBadge:
    def test_none_when_no_pick(self):
        from celerp_warehousing.ui import render_pick_instruction_badge
        assert render_pick_instruction_badge({}) is None

    def test_returns_link_when_pick_present(self):
        from celerp_warehousing.ui import render_pick_instruction_badge
        from fasthtml.common import to_xml
        el = render_pick_instruction_badge({"pick_instruction_id": "list:PICK-123"})
        assert el is not None
        html = to_xml(el)
        assert "Pick Instruction" in html

class TestRenderStockReceiptBadge:
    def test_none_when_no_receipt(self):
        from celerp_warehousing.ui import render_stock_receipt_badge
        assert render_stock_receipt_badge({}) is None

    def test_returns_link_when_receipt_present(self):
        from celerp_warehousing.ui import render_stock_receipt_badge
        from fasthtml.common import to_xml
        el = render_stock_receipt_badge({"stock_receipt_id": "list:RCPT-456"})
        assert el is not None
        html = to_xml(el)
        assert "Stock Receipt" in html


# ---------------------------------------------------------------------------
# API fulfill/unfulfill endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def _setup_ids():
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def auth(session, _setup_ids):
    cid = _setup_ids["company_id"]
    uid = _setup_ids["user_id"]
    session.add(Company(id=cid, name="FulfillCo", slug="fulfillco", settings={"currency": "USD"}))
    session.add(User(id=uid, company_id=cid, email="admin@fulfill.test", name="Admin", auth_hash="x", role="admin", is_active=True))
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token = create_access_token(subject=str(uid), company_id=str(cid), role="admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": str(uid)}


async def _create_item(client, auth, sku, qty, cost_price=0):
    r = await client.post("/items", headers=auth["headers"], json={"sku": sku, "name": sku, "quantity": qty, "cost_price": cost_price, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _create_and_finalize_invoice(client, auth, line_items):
    payload = {
        "doc_type": "invoice",
        "ref_id": f"FUL-{uuid.uuid4().hex[:6]}",
        "line_items": line_items,
        "total": sum(li.get("quantity", 0) * li.get("unit_price", 0) for li in line_items),
    }
    r = await client.post("/docs", headers=auth["headers"], json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r2.status_code == 200, r2.text
    return doc_id


@pytest.mark.asyncio
async def test_fulfill_api_endpoint(client, session, auth, _setup_ids):
    """POST /docs/{id}/fulfill deducts inventory and sets fulfillment_status."""
    await _create_item(client, auth, "API-A", 10, cost_price=5.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "API-A", "quantity": 3, "unit_price": 10.0},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["fulfillment_status"] in ("fulfilled", "partial")

    # Verify via GET
    r2 = await client.get(f"/docs/{doc_id}", headers=auth["headers"])
    assert r2.json().get("fulfillment_status") in ("fulfilled", "partial")


@pytest.mark.asyncio
async def test_unfulfill_api_endpoint(client, session, auth, _setup_ids):
    """POST /docs/{id}/unfulfill reverses fulfillment."""
    await _create_item(client, auth, "API-B", 5, cost_price=3.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "API-B", "quantity": 5, "unit_price": 8.0},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill", headers=auth["headers"], json={})
    assert r.status_code == 200

    r2 = await client.post(f"/docs/{doc_id}/unfulfill", headers=auth["headers"], json={})
    assert r2.status_code == 200, r2.text
    assert r2.json()["success"] is True


@pytest.mark.asyncio
async def test_fulfill_rejects_draft(client, session, auth, _setup_ids):
    """Cannot fulfill a draft document."""
    payload = {
        "doc_type": "invoice",
        "ref_id": f"DRAFT-{uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": "X", "quantity": 1, "unit_price": 5.0}],
        "total": 5.0,
    }
    r = await client.post("/docs", headers=auth["headers"], json=payload)
    doc_id = r.json()["id"]

    r2 = await client.post(f"/docs/{doc_id}/fulfill", headers=auth["headers"], json={})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_unfulfill_rejects_unfulfilled(client, session, auth, _setup_ids):
    """Cannot un-fulfill a document that isn't fulfilled."""
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "Y", "quantity": 1, "unit_price": 5.0},
    ])
    r = await client.post(f"/docs/{doc_id}/unfulfill", headers=auth["headers"], json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_fulfill_raises_on_stock_shortage(client, session, auth, _setup_ids):
    """POST /docs/{id}/fulfill returns 409 with item detail when stock is insufficient."""
    # Create a doc needing 9999 units of an item we only have a little of
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "X", "quantity": 9999, "unit_price": 1.0, "sell_by": "piece"},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill", headers=auth["headers"], json={})
    assert r.status_code == 409
    detail = r.json().get("detail", "")
    assert "insufficient stock" in detail.lower() or "cannot fulfill" in detail.lower(), \
        f"Expected shortage detail, got: {detail!r}"
