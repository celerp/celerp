# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for the celerp-warehousing: fulfillment UI renderers and API endpoints."""

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
    import importlib.util, sys, os
    pkg_path = os.path.join(os.path.dirname(__file__), "..", "premium_modules", "celerp-warehousing")
    spec = importlib.util.spec_from_file_location(
        "celerp-warehousing",
        os.path.join(pkg_path, "__init__.py"),
        submodule_search_locations=[pkg_path],
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.PLUGIN_MANIFEST
    assert m["name"] == "celerp-warehousing"
    assert m["default_enabled"] is True
    assert "doc_detail_actions" in m["slots"]
    assert "doc_detail_badges" in m["slots"]
    assert m["depends_on"] == ["celerp-docs", "celerp-inventory"]


# ---------------------------------------------------------------------------
# UI renderers
# ---------------------------------------------------------------------------

class TestRenderFulfillToggle:
    def test_none_for_draft(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        assert render_fulfill_toggle({"status": "draft", "entity_id": "d1"}) is None

    def test_none_for_void(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        assert render_fulfill_toggle({"status": "void", "entity_id": "d1"}) is None

    def test_none_for_service_only(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        doc = {
            "status": "final", "entity_id": "d1",
            "line_items": [{"sku": "SVC", "quantity": 1, "sell_by": "service"}],
        }
        assert render_fulfill_toggle(doc) is None

    def test_returns_button_for_final(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        doc = {
            "status": "final", "entity_id": "d1",
            "line_items": [{"sku": "PHY", "quantity": 1, "sell_by": "piece"}],
        }
        el = render_fulfill_toggle(doc)
        assert el is not None

    def test_returns_button_for_sent(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        doc = {
            "status": "sent", "entity_id": "d1",
            "line_items": [{"sku": "PHY", "quantity": 1}],
        }
        el = render_fulfill_toggle(doc)
        assert el is not None

    def test_fulfilled_shows_unfulfill(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        from fasthtml.common import to_xml
        doc = {
            "status": "final", "entity_id": "d1",
            "line_items": [{"sku": "PHY", "quantity": 1}],
            "fulfillment_status": "fulfilled",
        }
        el = render_fulfill_toggle(doc)
        html = to_xml(el)
        assert "Fulfilled ✓" in html
        assert "unfulfill" in html

    def test_partial_shows_complete(self):
        from celerp_warehousing.ui import render_fulfill_toggle
        from fasthtml.common import to_xml
        doc = {
            "status": "final", "entity_id": "d1",
            "line_items": [{"sku": "PHY", "quantity": 1}],
            "fulfillment_status": "partial",
        }
        el = render_fulfill_toggle(doc)
        html = to_xml(el)
        assert "Partially Fulfilled" in html
        assert "Complete Fulfillment" in html


class TestRenderFulfillmentBadge:
    def test_none_for_unfulfilled(self):
        from celerp_warehousing.ui import render_fulfillment_badge
        assert render_fulfillment_badge({"status": "final"}) is None

    def test_green_badge_for_fulfilled(self):
        from celerp_warehousing.ui import render_fulfillment_badge
        from fasthtml.common import to_xml
        el = render_fulfillment_badge({"fulfillment_status": "fulfilled"})
        assert el is not None
        html = to_xml(el)
        assert "badge--green" in html
        assert "Fulfilled" in html

    def test_amber_badge_for_partial(self):
        from celerp_warehousing.ui import render_fulfillment_badge
        from fasthtml.common import to_xml
        el = render_fulfillment_badge({"fulfillment_status": "partial"})
        assert el is not None
        html = to_xml(el)
        assert "badge--amber" in html
        assert "Partially Fulfilled" in html


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
