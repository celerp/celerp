# Copyright (c) 2026 Noah Severs. All Rights Reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for fulfill toggle (migrated from celerp-fulfillment to celerp-warehousing).

Verifies that the fulfillment toggle, badges, and API endpoints work correctly
under the new module name.
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


@pytest.fixture
def _ft_ids():
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def ft_auth(session, _ft_ids):
    cid = _ft_ids["company_id"]
    uid = _ft_ids["user_id"]
    session.add(Company(id=cid, name="FulfillToggleCo", slug="fttco", settings={"currency": "USD"}))
    session.add(User(id=uid, company_id=cid, email="admin@ft.test", name="Admin", auth_hash="x", role="admin", is_active=True))
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
        "ref_id": f"FT-INV-{uuid.uuid4().hex[:6]}",
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
# UI tests (module-level)
# ---------------------------------------------------------------------------

def _load_warehousing_manifest():
    """Load PLUGIN_MANIFEST from celerp-warehousing (resolved via sys.path)."""
    import importlib.util, os, sys
    for base in sys.path:
        candidate = os.path.join(base, "__init__.py")
        # Look for a path that contains celerp-warehousing
        pkg_dir = base if os.path.basename(base) == "celerp-warehousing" else None
        if pkg_dir is None:
            # Try the parent (when base is the module dir itself)
            parent = os.path.dirname(base)
            if os.path.basename(parent) == "celerp-warehousing" or os.path.isfile(
                os.path.join(base, "celerp_warehousing", "__init__.py")
            ):
                pkg_dir = base
        if pkg_dir is None:
            continue
        init_file = os.path.join(pkg_dir, "__init__.py")
        if not os.path.isfile(init_file):
            continue
        spec = importlib.util.spec_from_file_location(
            "_wh_manifest_test",
            init_file,
            submodule_search_locations=[pkg_dir],
        )
        if spec is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "PLUGIN_MANIFEST"):
            return mod.PLUGIN_MANIFEST
    # Fallback: import via the module package directly
    import importlib
    wh = importlib.import_module("celerp_warehousing")
    # Walk up to package root
    pkg_root = os.path.dirname(os.path.dirname(wh.__file__))
    init_file = os.path.join(pkg_root, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "_wh_manifest_test2", init_file, submodule_search_locations=[pkg_root],
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PLUGIN_MANIFEST


def test_warehousing_module_has_fulfill_toggle_slot():
    """PLUGIN_MANIFEST registers fulfill toggle in doc_detail_actions slot."""
    m = _load_warehousing_manifest()
    actions = m["slots"]["doc_detail_actions"]
    renders = [a["render"] for a in actions]
    assert any("render_fulfill_toggle" in r for r in renders)


def test_fulfillment_badge_slot():
    """PLUGIN_MANIFEST registers fulfillment badge in doc_detail_badges slot."""
    m = _load_warehousing_manifest()
    badges = m["slots"]["doc_detail_badges"]
    renders = [b["render"] for b in badges]
    assert any("render_fulfillment_badge" in r for r in renders)


# ---------------------------------------------------------------------------
# API endpoint tests (same as former celerp-fulfillment tests)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fulfill_api_endpoint(client, session, ft_auth):
    """POST /docs/{id}/fulfill deducts inventory and sets fulfillment_status."""
    await _create_item(client, ft_auth, "FT-API-A", 10, cost_price=5.0)
    doc_id = await _create_and_finalize_invoice(client, ft_auth, [
        {"sku": "FT-API-A", "quantity": 3, "unit_price": 10.0},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill", headers=ft_auth["headers"], json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["fulfillment_status"] in ("fulfilled", "partial")

    r2 = await client.get(f"/docs/{doc_id}", headers=ft_auth["headers"])
    assert r2.json().get("fulfillment_status") in ("fulfilled", "partial")


@pytest.mark.asyncio
async def test_unfulfill_api_endpoint(client, session, ft_auth):
    """POST /docs/{id}/unfulfill reverses fulfillment."""
    await _create_item(client, ft_auth, "FT-API-B", 5, cost_price=3.0)
    doc_id = await _create_and_finalize_invoice(client, ft_auth, [
        {"sku": "FT-API-B", "quantity": 5, "unit_price": 8.0},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill", headers=ft_auth["headers"], json={})
    assert r.status_code == 200

    r2 = await client.post(f"/docs/{doc_id}/unfulfill", headers=ft_auth["headers"], json={})
    assert r2.status_code == 200, r2.text

    r3 = await client.get(f"/docs/{doc_id}", headers=ft_auth["headers"])
    doc = r3.json()
    assert doc.get("fulfillment_status") in ("unfulfilled", None, "")


@pytest.mark.asyncio
async def test_fulfill_rejects_draft(client, session, ft_auth):
    """Cannot fulfill a draft document."""
    payload = {
        "doc_type": "invoice", "ref_id": f"DRAFT-FT-{uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": "X", "quantity": 1, "unit_price": 5.0}], "total": 5.0,
    }
    r = await client.post("/docs", headers=ft_auth["headers"], json=payload)
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/fulfill", headers=ft_auth["headers"], json={})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_unfulfill_rejects_unfulfilled(client, session, ft_auth):
    """Cannot un-fulfill a document that isn't fulfilled."""
    doc_id = await _create_and_finalize_invoice(client, ft_auth, [
        {"sku": "Y", "quantity": 1, "unit_price": 5.0},
    ])
    r = await client.post(f"/docs/{doc_id}/unfulfill", headers=ft_auth["headers"], json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_pick_instruction_badge_shows_when_linked(client, session, ft_auth):
    """render_pick_instruction_badge returns a badge when pick_instruction_id is set."""
    from celerp_warehousing.ui import render_pick_instruction_badge
    from fasthtml.common import to_xml

    doc = {"pick_instruction_id": "list:PICK-001"}
    el = render_pick_instruction_badge(doc)
    assert el is not None
    html = to_xml(el)
    assert "pick-instructions" in html
    assert "PICK-001" in html


@pytest.mark.asyncio
async def test_stock_receipt_badge_shows_when_linked(client, session, ft_auth):
    """render_stock_receipt_badge returns a badge when stock_receipt_id is set."""
    from celerp_warehousing.ui import render_stock_receipt_badge
    from fasthtml.common import to_xml

    doc = {"stock_receipt_id": "list:RCPT-001"}
    el = render_stock_receipt_badge(doc)
    assert el is not None
    html = to_xml(el)
    assert "stock-receipts" in html
    assert "RCPT-001" in html


@pytest.mark.asyncio
async def test_no_badges_when_no_ids(client, session, ft_auth):
    """render_*_badge returns None when no linked document."""
    from celerp_warehousing.ui import render_pick_instruction_badge, render_stock_receipt_badge
    assert render_pick_instruction_badge({}) is None
    assert render_stock_receipt_badge({}) is None
