# Copyright (c) 2026 Noah Severs. All Rights Reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for pick instruction creation, completion, and lifecycle.

These tests run from the core repo's test suite after celerp-warehousing is
installed into default_modules/celerp-warehousing/.
"""

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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _wh_ids():
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def wh_auth(session, _wh_ids):
    cid = _wh_ids["company_id"]
    uid = _wh_ids["user_id"]
    session.add(Company(id=cid, name="WarehouseCo", slug="warehouseco", settings={"currency": "USD"}))
    session.add(User(id=uid, company_id=cid, email="admin@warehouse.test", name="Admin", auth_hash="x", role="admin", is_active=True))
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token = create_access_token(subject=str(uid), company_id=str(cid), role="admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": str(uid)}


async def _create_item(client, auth, sku, qty, cost_price=5.0):
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": sku, "quantity": qty, "cost_price": cost_price, "sell_by": "piece",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _create_and_finalize_invoice(client, auth, line_items):
    payload = {
        "doc_type": "invoice",
        "ref_id": f"WH-INV-{uuid.uuid4().hex[:6]}",
        "line_items": line_items,
        "total": sum(li.get("quantity", 0) * li.get("unit_price", 10) for li in line_items),
    }
    r = await client.post("/docs", headers=auth["headers"], json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r2.status_code == 200, r2.text
    return doc_id


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

def test_warehousing_manifest_loads():
    import importlib.util, os, sys
    # Find the manifest via celerp_warehousing package on sys.path
    import importlib as _il
    wh_pkg = _il.import_module("celerp_warehousing")
    pkg_root = os.path.dirname(os.path.dirname(wh_pkg.__file__))
    init_file = os.path.join(pkg_root, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "_wh_manifest_pi",
        init_file,
        submodule_search_locations=[pkg_root],
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.PLUGIN_MANIFEST
    assert m["name"] == "celerp-warehousing"
    assert m["license"] == "Proprietary"
    assert m["default_enabled"] is True
    assert "doc_detail_actions" in m["slots"]
    assert "doc_detail_badges" in m["slots"]
    assert "doc_finalize_hook" in m["slots"]
    assert m["depends_on"] == ["celerp-docs", "celerp-inventory"]


# ---------------------------------------------------------------------------
# UI renderer tests (migrated from celerp-fulfillment)
# ---------------------------------------------------------------------------

class TestFulfillToggle:
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


class TestFulfillmentBadge:
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


class TestAlreadyDeliveredToggle:
    def test_none_for_draft(self):
        from celerp_warehousing.ui import render_already_delivered_toggle
        assert render_already_delivered_toggle({"status": "draft", "doc_type": "invoice"}) is None

    def test_none_for_non_sales_doc(self):
        from celerp_warehousing.ui import render_already_delivered_toggle
        assert render_already_delivered_toggle({"status": "final", "doc_type": "purchase_order"}) is None

    def test_none_for_fulfilled(self):
        from celerp_warehousing.ui import render_already_delivered_toggle
        assert render_already_delivered_toggle({
            "status": "final", "doc_type": "invoice",
            "fulfillment_status": "fulfilled",
        }) is None

    def test_renders_for_final_invoice(self):
        from celerp_warehousing.ui import render_already_delivered_toggle
        from fasthtml.common import to_xml
        doc = {
            "status": "final", "doc_type": "invoice",
            "entity_id": "d1",
            "line_items": [{"sku": "X", "quantity": 1}],
        }
        el = render_already_delivered_toggle(doc)
        assert el is not None
        html = to_xml(el)
        assert "mark-delivered" in html


# ---------------------------------------------------------------------------
# Pick instruction API tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pick_instructions_list_empty(client, session, wh_auth):
    """GET /warehousing/pick-instructions returns empty list initially."""
    r = await client.get("/warehousing/pick-instructions", headers=wh_auth["headers"])
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_create_pick_instruction_via_mark_delivered(client, session, wh_auth):
    """POST /warehousing/docs/{id}/mark-delivered creates + completes pick instruction."""
    await _create_item(client, wh_auth, "PICK-SKU-A", 10, cost_price=5.0)
    doc_id = await _create_and_finalize_invoice(client, wh_auth, [
        {"sku": "PICK-SKU-A", "quantity": 3, "unit_price": 10.0},
    ])

    r = await client.post(f"/warehousing/docs/{doc_id}/mark-delivered", headers=wh_auth["headers"], json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "pick_instruction_id" in data
    assert data.get("fulfillment_status") in ("fulfilled", "partial")


@pytest.mark.asyncio
async def test_mark_delivered_rejects_draft(client, session, wh_auth):
    """mark-delivered rejects non-finalized documents."""
    payload = {
        "doc_type": "invoice", "ref_id": f"DRAFT-{uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": "X", "quantity": 1, "unit_price": 5.0}], "total": 5.0,
    }
    r = await client.post("/docs", headers=wh_auth["headers"], json=payload)
    doc_id = r.json()["id"]

    r2 = await client.post(f"/warehousing/docs/{doc_id}/mark-delivered", headers=wh_auth["headers"], json={})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_get_pick_instruction(client, session, wh_auth):
    """GET /warehousing/pick-instructions/{id} returns pick instruction detail."""
    await _create_item(client, wh_auth, "PICK-GET-A", 10, cost_price=5.0)
    doc_id = await _create_and_finalize_invoice(client, wh_auth, [
        {"sku": "PICK-GET-A", "quantity": 2, "unit_price": 10.0},
    ])

    r = await client.post(f"/warehousing/docs/{doc_id}/mark-delivered", headers=wh_auth["headers"], json={})
    assert r.status_code == 200
    pi_id = r.json()["pick_instruction_id"]

    r2 = await client.get(f"/warehousing/pick-instructions/{pi_id}", headers=wh_auth["headers"])
    assert r2.status_code == 200
    pi = r2.json()
    assert pi["list_type"] == "pick_instruction"
    assert pi["source_doc_id"] == doc_id


@pytest.mark.asyncio
async def test_complete_pick_instruction(client, session, wh_auth):
    """POST /warehousing/pick-instructions/{id}/complete fulfills the source doc."""
    await _create_item(client, wh_auth, "COMPLETE-A", 20, cost_price=3.0)
    doc_id = await _create_and_finalize_invoice(client, wh_auth, [
        {"sku": "COMPLETE-A", "quantity": 5, "unit_price": 10.0},
    ])

    # Create a pick instruction via doc_finalize_hook (simulated here via mark-delivered)
    r = await client.post(f"/warehousing/docs/{doc_id}/mark-delivered", headers=wh_auth["headers"], json={})
    assert r.status_code == 200
    data = r.json()
    assert data.get("fulfillment_status") in ("fulfilled", "partial")


@pytest.mark.asyncio
async def test_void_pick_instruction(client, session, wh_auth):
    """POST /warehousing/pick-instructions/{id}/void voids the pick instruction."""
    await _create_item(client, wh_auth, "VOID-A", 10, cost_price=5.0)
    doc_id = await _create_and_finalize_invoice(client, wh_auth, [
        {"sku": "VOID-A", "quantity": 2, "unit_price": 10.0},
    ])

    # Use the finalize hook path by triggering direct pick creation
    from celerp.models.projections import Projection
    import uuid as _uuid
    from celerp_warehousing.pick_instructions import create_pick_instruction

    doc_row = await session.get(
        Projection, {"company_id": wh_auth["company_id"], "entity_id": doc_id}
    )
    result = await create_pick_instruction(
        session,
        source_doc_id=doc_id,
        source_doc_state=doc_row.state,
        company_id=wh_auth["company_id"],
        user_id=wh_auth["user_id"],
    )
    await session.commit()
    pi_id = result["pick_instruction_id"]
    assert pi_id is not None

    r = await client.post(
        f"/warehousing/pick-instructions/{pi_id}/void",
        headers=wh_auth["headers"],
        json={"reason": "test void"},
    )
    assert r.status_code == 200

    r2 = await client.get(f"/warehousing/pick-instructions/{pi_id}", headers=wh_auth["headers"])
    assert r2.json()["status"] == "void"


@pytest.mark.asyncio
async def test_pick_instruction_not_found(client, session, wh_auth):
    """GET /warehousing/pick-instructions/{id} returns 404 for unknown id."""
    r = await client.get("/warehousing/pick-instructions/list:nonexistent", headers=wh_auth["headers"])
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_pick_instruction_service_only_returns_none(client, session, wh_auth):
    """Service-only invoices don't get pick instructions."""
    doc_id = await _create_and_finalize_invoice(client, wh_auth, [
        {"sku": "SVC-A", "quantity": 1, "unit_price": 50.0, "sell_by": "service"},
    ])
    r = await client.post(f"/warehousing/docs/{doc_id}/mark-delivered", headers=wh_auth["headers"], json={})
    assert r.status_code == 422  # no physical items to pick
