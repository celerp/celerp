# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the fulfillment engine: pick algorithm, fulfill/un-fulfill, and lifecycle wiring."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.auth import create_access_token
from celerp.services.pick import PickResult, compute_pick_plan


# ---------------------------------------------------------------------------
# Pure pick algorithm tests (no DB needed)
# ---------------------------------------------------------------------------


class TestPickAlgorithm:
    """Tests for the FIFO/FEFO pick algorithm — pure function, zero side effects."""

    def _inv(self, sku, qty, entity_id=None, created_at=None, expires_at=None, cost_price=0):
        return {
            "entity_id": entity_id or f"item:{uuid.uuid4()}",
            "sku": sku,
            "quantity": qty,
            "created_at": created_at or "2026-01-01T00:00:00",
            "expires_at": expires_at,
            "cost_price": cost_price,
        }

    def test_fifo_single_item(self):
        line_items = [{"sku": "SKU-A", "quantity": 5}]
        inventory = [self._inv("SKU-A", 10, cost_price=2.0)]
        result = compute_pick_plan(line_items, inventory)
        assert result.strategy == "fifo"
        assert len(result.picks) == 1
        assert result.picks[0].pick_qty == 5
        assert result.picks[0].action == "split"
        assert result.unfulfilled == []

    def test_fifo_full_pick(self):
        line_items = [{"sku": "SKU-A", "quantity": 10}]
        inventory = [self._inv("SKU-A", 10, cost_price=3.0)]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "full"
        assert result.picks[0].pick_qty == 10

    def test_fifo_multiple_batches_oldest_first(self):
        line_items = [{"sku": "SKU-A", "quantity": 15}]
        inventory = [
            self._inv("SKU-A", 10, created_at="2026-01-05", cost_price=2.0),
            self._inv("SKU-A", 10, created_at="2026-01-01", cost_price=1.5),
            self._inv("SKU-A", 10, created_at="2026-01-10", cost_price=3.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        assert result.strategy == "fifo"
        # Oldest batch (Jan 01) picked first (full 10), then next oldest (Jan 05) for remaining 5
        assert len(result.picks) == 2
        assert result.picks[0].cost_price == 1.5
        assert result.picks[0].pick_qty == 10
        assert result.picks[0].action == "full"
        assert result.picks[1].cost_price == 2.0
        assert result.picks[1].pick_qty == 5
        assert result.picks[1].action == "split"
        assert result.unfulfilled == []

    def test_fifo_insufficient_stock(self):
        line_items = [{"sku": "SKU-A", "quantity": 20}]
        inventory = [self._inv("SKU-A", 5, cost_price=1.0)]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].pick_qty == 5
        assert len(result.unfulfilled) == 1
        assert result.unfulfilled[0]["sku"] == "SKU-A"
        assert result.unfulfilled[0]["short_qty"] == 15

    def test_fifo_no_stock(self):
        line_items = [{"sku": "SKU-A", "quantity": 5}]
        result = compute_pick_plan(line_items, [])
        assert result.picks == []
        assert len(result.unfulfilled) == 1

    def test_fefo_expires_at_sorting(self):
        line_items = [{"sku": "SKU-A", "quantity": 8}]
        inventory = [
            self._inv("SKU-A", 5, created_at="2026-01-01", expires_at="2026-06-01", cost_price=2.0),
            self._inv("SKU-A", 5, created_at="2026-01-10", expires_at="2026-03-01", cost_price=1.5),
            self._inv("SKU-A", 5, created_at="2026-01-05", cost_price=3.0),  # no expiry
        ]
        result = compute_pick_plan(line_items, inventory)
        assert result.strategy == "fefo"
        # Earliest expiry (Mar 01) first, then next (Jun 01)
        assert result.picks[0].cost_price == 1.5  # expires Mar 01
        assert result.picks[0].pick_qty == 5
        assert result.picks[1].cost_price == 2.0  # expires Jun 01
        assert result.picks[1].pick_qty == 3

    def test_sku_exact_match(self):
        line_items = [{"sku": "SKU-A", "quantity": 5}]
        inventory = [
            self._inv("SKU-A", 10, cost_price=1.0),
            self._inv("SKU-B", 10, cost_price=2.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].sku == "SKU-A"

    def test_sku_child_prefix_matching(self):
        """Child SKUs like SKU-A.1 should match parent line item SKU-A."""
        line_items = [{"sku": "SKU-A", "quantity": 8}]
        inventory = [
            self._inv("SKU-A", 3, created_at="2026-01-01", cost_price=1.0),
            self._inv("SKU-A.1", 5, created_at="2026-01-02", cost_price=1.5),
            self._inv("SKU-A.2", 4, created_at="2026-01-03", cost_price=2.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        assert result.unfulfilled == []
        total_picked = sum(p.pick_qty for p in result.picks)
        assert total_picked == 8

    def test_child_prefix_no_false_match(self):
        """SKU-AB should NOT match line item SKU-A."""
        line_items = [{"sku": "SKU-A", "quantity": 5}]
        inventory = [
            self._inv("SKU-AB", 10, cost_price=1.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        assert result.picks == []
        assert len(result.unfulfilled) == 1

    def test_service_items_skipped(self):
        line_items = [
            {"sku": "SVC-01", "quantity": 2, "sell_by": "service"},
            {"sku": "HOUR-01", "quantity": 8, "sell_by": "hour"},
        ]
        inventory = []  # no inventory at all
        result = compute_pick_plan(line_items, inventory)
        assert result.picks == []
        assert result.unfulfilled == []

    def test_mixed_physical_and_service(self):
        line_items = [
            {"sku": "PHYS-01", "quantity": 3},
            {"sku": "SVC-01", "quantity": 2, "sell_by": "service"},
        ]
        inventory = [self._inv("PHYS-01", 3, cost_price=5.0)]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].sku == "PHYS-01"
        assert result.unfulfilled == []

    def test_multiple_line_items(self):
        line_items = [
            {"sku": "SKU-A", "quantity": 3},
            {"sku": "SKU-B", "quantity": 5},
        ]
        inventory = [
            self._inv("SKU-A", 10, cost_price=1.0),
            self._inv("SKU-B", 5, cost_price=2.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 2
        assert result.unfulfilled == []

    def test_split_sku_generation(self):
        line_items = [{"sku": "SKU-A", "quantity": 3}]
        inventory = [self._inv("SKU-A", 10, cost_price=1.0)]
        result = compute_pick_plan(line_items, inventory)
        assert result.picks[0].action == "split"
        assert result.picks[0].split_sku == "SKU-A.1"

    def test_split_sku_increments_suffix(self):
        """When SKU-A.1 already exists, split should use SKU-A.2."""
        line_items = [{"sku": "SKU-A", "quantity": 3}]
        inventory = [
            self._inv("SKU-A", 10, cost_price=1.0),
            self._inv("SKU-A.1", 5, cost_price=1.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        split_picks = [p for p in result.picks if p.action == "split"]
        if split_picks:
            assert split_picks[0].split_sku == "SKU-A.2"

    def test_zero_quantity_line_skipped(self):
        line_items = [{"sku": "SKU-A", "quantity": 0}]
        inventory = [self._inv("SKU-A", 10)]
        result = compute_pick_plan(line_items, inventory)
        assert result.picks == []
        assert result.unfulfilled == []

    def test_empty_sku_line_skipped(self):
        line_items = [{"sku": "", "quantity": 5}]
        inventory = [self._inv("SKU-A", 10)]
        result = compute_pick_plan(line_items, inventory)
        assert result.picks == []
        assert result.unfulfilled == []


# ---------------------------------------------------------------------------
# Integration tests using the API (need client + session)
# ---------------------------------------------------------------------------


@pytest.fixture
def _setup_ids():
    return {
        "company_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
    }


@pytest_asyncio.fixture
async def auth(session, _setup_ids):
    cid = _setup_ids["company_id"]
    uid = _setup_ids["user_id"]
    session.add(Company(id=cid, name="TestCo", slug="testco", settings={"currency": "USD"}))
    session.add(User(id=uid, company_id=cid, email="admin@test.co", name="Admin", auth_hash="x", role="admin", is_active=True))
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token = create_access_token(subject=str(uid), company_id=str(cid), role="admin")
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "company_id": cid,       # UUID, not string
        "user_id": str(uid),
    }


async def _create_item(client, auth, sku, qty, cost_price=0, created_at=None, expires_at=None, sell_by="piece"):
    """Helper: create inventory item via API."""
    data = {"sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by}
    if cost_price:
        data["cost_price"] = cost_price
    if created_at:
        data["created_at"] = created_at
    if expires_at:
        data["expires_at"] = expires_at
    r = await client.post("/items", headers=auth["headers"], json=data)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _create_and_finalize_invoice(client, auth, line_items, ref_id=None):
    """Helper: create invoice, finalize it, return entity_id."""
    payload = {
        "doc_type": "invoice",
        "ref_id": ref_id or f"TEST-{uuid.uuid4().hex[:6]}",
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
async def test_fulfill_creates_events_and_updates_projections(client, session, auth, _setup_ids):
    """Fulfill execution: creates events and updates projections."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "WIDGET-A", 10, cost_price=5.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "WIDGET-A", "quantity": 3, "unit_price": 10.0},
    ])

    # Get doc state
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    doc_state = doc_row.state

    # Build pick plan
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    available_inv = [{
        "entity_id": item_id,
        "sku": inv_row.state["sku"],
        "quantity": float(inv_row.state["quantity"]),
        "created_at": inv_row.state.get("created_at", ""),
        "expires_at": inv_row.state.get("expires_at"),
        "cost_price": float(inv_row.state.get("cost_price", 0)),
    }]
    pick_result = compute_pick_plan(doc_state.get("line_items", []), available_inv)
    result = await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    assert result["fulfillment_status"] in ("fulfilled", "partial")
    assert len(result["fulfilled_items"]) >= 1
    assert result["total_cogs"] == 15.0  # 3 * 5.0

    # Check doc projection
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") in ("fulfilled", "partial")


@pytest.mark.asyncio
async def test_unfulfill_restores_stock_and_reverses_je(client, session, auth, _setup_ids):
    """Un-fulfill: restores stock and reverses JE."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill, execute_unfulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "RESTORE-A", 10, cost_price=3.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "RESTORE-A", "quantity": 10, "unit_price": 8.0},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    available_inv = [{
        "entity_id": item_id, "sku": "RESTORE-A", "quantity": 10,
        "created_at": "", "expires_at": None, "cost_price": 3.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Verify item is sold/qty=0 after fulfillment
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert float(inv_row.state.get("quantity", 0)) == 0

    # Re-read doc state (now has fulfilled_items)
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") == "fulfilled"

    # Un-fulfill
    result = await execute_unfulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]), reason="test",
    )
    await session.commit()

    assert result["success"] is True

    # Verify stock restored
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert float(inv_row.state.get("quantity", 0)) == 10
    assert inv_row.state.get("is_available") is True

    # Verify doc fulfillment cleared
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") is None


@pytest.mark.asyncio
async def test_void_does_not_change_fulfillment_status(client, session, auth, _setup_ids):
    """Voiding a fulfilled doc must NOT change its fulfillment_status (independent lifecycles)."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "VOID-A", 5, cost_price=10.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "VOID-A", "quantity": 5, "unit_price": 20.0},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    available_inv = [{
        "entity_id": item_id, "sku": "VOID-A", "quantity": 5,
        "created_at": "", "expires_at": None, "cost_price": 10.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Void via API
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={"reason": "test"})
    assert r.status_code == 200, r.text

    # fulfillment_status must be preserved - void and fulfillment are independent lifecycles
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") == "fulfilled", \
        "Voiding a fulfilled doc must NOT clear fulfillment_status"

    # Stock must NOT be automatically restored (fulfillment is independent)
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert float(inv_row.state.get("quantity", 0)) == 0, \
        "Voiding must not auto-restore stock; use Revert Fulfillment for that"


@pytest.mark.asyncio
async def test_revert_to_draft_does_not_change_fulfillment_status(client, session, auth, _setup_ids):
    """Reverting a fulfilled doc to draft must NOT change its fulfillment_status (independent lifecycles)."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "REVERT-A", 8, cost_price=4.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "REVERT-A", "quantity": 8, "unit_price": 12.0},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    available_inv = [{
        "entity_id": item_id, "sku": "REVERT-A", "quantity": 8,
        "created_at": "", "expires_at": None, "cost_price": 4.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Revert to draft via API
    r = await client.post(f"/docs/{doc_id}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text

    # fulfillment_status must be preserved
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") == "fulfilled", \
        "Reverting to draft must NOT clear fulfillment_status"

    # Stock must NOT be automatically restored
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert float(inv_row.state.get("quantity", 0)) == 0, \
        "Revert-to-draft must not auto-restore stock; use Revert Fulfillment for that"


@pytest.mark.asyncio
async def test_unvoid_does_not_auto_refulfill(client, session, auth, _setup_ids):
    """Unvoiding a doc must NOT auto-re-fulfill it (fulfillment is always manual)."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "UNVOID-A", 10, cost_price=2.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "UNVOID-A", "quantity": 5, "unit_price": 6.0},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    available_inv = [{
        "entity_id": item_id, "sku": "UNVOID-A", "quantity": 10,
        "created_at": "", "expires_at": None, "cost_price": 2.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Void the doc
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={"reason": "test void"})
    assert r.status_code == 200

    # Unvoid the doc
    r = await client.post(f"/docs/{doc_id}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200

    # fulfillment_status must remain what it was (fulfilled - void didn't change it)
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    fs = doc_row.state.get("fulfillment_status")
    assert fs == "fulfilled", \
        f"fulfillment_status should be 'fulfilled' (unchanged through void/unvoid cycle), got: {fs}"


@pytest.mark.asyncio
async def test_service_items_auto_fulfilled(client, session, auth, _setup_ids):
    """Service items should be auto-marked fulfilled, no physical pick."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "SVC-01", "quantity": 2, "unit_price": 50.0, "sell_by": "service"},
        {"sku": "SVC-02", "quantity": 4, "unit_price": 25.0, "sell_by": "hour"},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), [])
    result = await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # All service items → fulfilled, no picks needed
    assert result["fulfillment_status"] == "fulfilled"
    service_items = [fi for fi in result["fulfilled_items"] if fi["action"] == "service"]
    assert len(service_items) == 2
    assert result["total_cogs"] == 0.0


@pytest.mark.asyncio
async def test_mixed_invoice_physical_and_service(client, session, auth, _setup_ids):
    """Mixed invoice: only physical items get picked, service auto-marked."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "MIX-PHYS", 5, cost_price=7.0)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "MIX-PHYS", "quantity": 3, "unit_price": 15.0},
        {"sku": "MIX-SVC", "quantity": 1, "unit_price": 100.0, "sell_by": "service"},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    available_inv = [{
        "entity_id": item_id, "sku": "MIX-PHYS", "quantity": 5,
        "created_at": "", "expires_at": None, "cost_price": 7.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    result = await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    physical = [fi for fi in result["fulfilled_items"] if fi["action"] != "service"]
    services = [fi for fi in result["fulfilled_items"] if fi["action"] == "service"]
    assert len(physical) >= 1
    assert len(services) == 1
    assert result["total_cogs"] == 21.0  # 3 * 7.0


@pytest.mark.asyncio
async def test_pick_event_schemas_registered():
    """Verify all fulfillment event types are registered in EVENT_SCHEMA_MAP."""
    from celerp.events.schemas import EVENT_SCHEMA_MAP

    expected = [
        "item.fulfilled",
        "item.fulfillment_reversed",
        "doc.fulfilled",
        "doc.partially_fulfilled",
        "doc.fulfillment_reversed",
    ]
    for event_type in expected:
        assert event_type in EVENT_SCHEMA_MAP, f"{event_type} not in EVENT_SCHEMA_MAP"


class TestPickAllowSplitting:
    def test_no_split_when_allow_splitting_false(self):
        """Items with allow_splitting=False must not be split during picking; mark as unfulfilled if no full item available."""
        line_items = [{"sku": "NS-A", "quantity": 3}]
        inventory = [{"entity_id": "e1", "sku": "NS-A", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_price": 5.0, "allow_splitting": False}]
        result = compute_pick_plan(line_items, inventory)
        # The item has qty 10 > 3 needed; cannot split -> unfulfilled
        assert result.picks == []
        assert len(result.unfulfilled) == 1
        assert result.unfulfilled[0]["sku"] == "NS-A"

    def test_full_pick_still_works_when_allow_splitting_false(self):
        """Full pick (no split needed) must still succeed when allow_splitting=False."""
        line_items = [{"sku": "NS-B", "quantity": 10}]
        inventory = [{"entity_id": "e2", "sku": "NS-B", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_price": 2.0, "allow_splitting": False}]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "full"
        assert result.unfulfilled == []

    def test_split_when_allow_splitting_true(self):
        """Items with allow_splitting=True must be split as usual."""
        line_items = [{"sku": "SP-A", "quantity": 3}]
        inventory = [{"entity_id": "e3", "sku": "SP-A", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_price": 1.0, "allow_splitting": True}]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "split"
        assert result.unfulfilled == []

    def test_split_when_allow_splitting_missing_defaults_to_allowed(self):
        """Items without allow_splitting key default to splittable (backward compat for existing data)."""
        line_items = [{"sku": "SP-B", "quantity": 3}]
        inventory = [{"entity_id": "e4", "sku": "SP-B", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_price": 1.0}]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "split"
