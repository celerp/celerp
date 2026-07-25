# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the fulfillment engine: pick algorithm, fulfill/un-fulfill, and lifecycle wiring."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

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
            "cost_total": cost_price * qty,  # cost_total is now the primitive
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

    def test_lifo_newest_first(self):
        line_items = [{"sku": "SKU-A", "quantity": 15}]
        inventory = [
            self._inv("SKU-A", 10, created_at="2026-01-05", cost_price=2.0),
            self._inv("SKU-A", 10, created_at="2026-01-01", cost_price=1.5),
            self._inv("SKU-A", 10, created_at="2026-01-10", cost_price=3.0),
        ]
        result = compute_pick_plan(line_items, inventory, strategy="lifo")
        assert result.strategy == "lifo"
        # Newest batch (Jan 10) first (full 10), then next-newest (Jan 05) for remaining 5.
        assert result.picks[0].cost_price == 3.0 and result.picks[0].pick_qty == 10
        assert result.picks[1].cost_price == 2.0 and result.picks[1].pick_qty == 5
        # COGS = specific cost of the lots drawn (10*3.0 + 5*2.0 = 40).
        assert sum(p.pick_qty * p.cost_price for p in result.picks) == 40.0

    def test_explicit_strategy_overrides_auto_detect(self):
        # Inventory HAS an expiry (would auto-detect FEFO), but an explicit FIFO wins.
        line_items = [{"sku": "SKU-A", "quantity": 5}]
        inventory = [
            self._inv("SKU-A", 5, created_at="2026-01-01", expires_at="2026-09-01", cost_price=1.0),
            self._inv("SKU-A", 5, created_at="2026-01-02", expires_at="2026-03-01", cost_price=2.0),
        ]
        result = compute_pick_plan(line_items, inventory, strategy="fifo")
        assert result.strategy == "fifo"
        assert result.picks[0].cost_price == 1.0  # oldest received, not soonest-expiry

    def test_resolve_pick_method(self):
        from celerp.services.pick import resolve_pick_method
        # Item override wins over company default.
        assert resolve_pick_method({"pick_method": "lifo"}, {"inventory_method": "fifo"}) == "lifo"
        # "default"/blank item -> company default.
        assert resolve_pick_method({"pick_method": "default"}, {"inventory_method": "fefo"}) == "fefo"
        assert resolve_pick_method({}, {"inventory_method": "lifo"}) == "lifo"
        # Nothing set -> fifo. Invalid values ignored.
        assert resolve_pick_method({}, {}) == "fifo"
        assert resolve_pick_method({"pick_method": "bogus"}, {}) == "fifo"

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

    def test_split_child_keeps_parent_sku(self):
        """A partial pick splits off the needed qty; the child KEEPS the parent SKU (same
        product, distinct lot by barcode/entity_id) - no '.N' suffix is generated."""
        line_items = [{"sku": "SKU-A", "quantity": 3}]
        inventory = [self._inv("SKU-A", 10, cost_price=1.0)]
        result = compute_pick_plan(line_items, inventory)
        assert result.picks[0].action == "split"
        assert result.picks[0].sku == "SKU-A"
        assert not hasattr(result.picks[0], "split_sku")  # field removed

    def test_split_child_sku_unaffected_by_existing_lots(self):
        """Existing same-SKU lots never change the split child's SKU - still the parent SKU."""
        line_items = [{"sku": "SKU-A", "quantity": 3}]
        inventory = [
            self._inv("SKU-A", 4, created_at="2026-01-01", cost_price=1.0),
            self._inv("SKU-A", 5, created_at="2026-01-02", cost_price=1.0),
        ]
        result = compute_pick_plan(line_items, inventory)
        split_picks = [p for p in result.picks if p.action == "split"]
        assert split_picks and all(p.sku == "SKU-A" for p in split_picks)

    def test_non_digit_suffix_is_a_distinct_product(self):
        """A non-digit suffix (e.g. SKU-A.PRO) is a distinct product, NOT a legacy split
        child, so it must not be picked for a SKU-A line."""
        line_items = [{"sku": "SKU-A", "quantity": 5}]
        inventory = [self._inv("SKU-A.PRO", 10, cost_price=1.0)]
        result = compute_pick_plan(line_items, inventory)
        assert result.picks == []
        assert len(result.unfulfilled) == 1

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
    session.add(User(id=uid, email="admin@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()  # parents before membership for Postgres FK checks
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token, _ = create_access_token(subject=str(uid), company_id=str(cid), role="admin")
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "company_id": cid,       # UUID, not string
        "user_id": str(uid),
    }


async def _create_item(client, auth, sku, qty, cost_price=0, expires_at=None, sell_by="piece"):
    """Helper: create inventory item via API."""
    data = {"sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by}
    if cost_price:
        data["cost_total"] = cost_price * qty  # cost_total is now the primitive
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
        "created_at": inv_row.created_at.isoformat() if inv_row.created_at else "",
        "expires_at": inv_row.state.get("expires_at"),
        "cost_total": float(inv_row.state.get("cost_total", 0)),
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
async def test_fulfilled_item_event_carries_doc_number(client, session, auth, _setup_ids):
    """Issue 2: a sold/fulfilled item's history event records source_doc_id + a stable
    doc_number, and the Activity renderer turns it into a link to the doc."""
    from celerp.models.projections import Projection
    from celerp.models.ledger import LedgerEntry
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan
    from sqlalchemy import select

    # qty == line qty -> a "full" pick, so item.fulfilled lands on the item itself
    # (a partial pick would carve a child and fulfill that instead).
    item_id = await _create_item(client, auth, "DOCNUM-A", 2, cost_price=2.0)
    doc_id = await _create_and_finalize_invoice(
        client, auth, [{"sku": "DOCNUM-A", "quantity": 2, "unit_price": 9.0}], ref_id="INV-DOCNUM-1")

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    available_inv = [{"entity_id": item_id, "sku": inv_row.state["sku"],
                      "quantity": float(inv_row.state["quantity"]),
                      "created_at": inv_row.created_at.isoformat() if inv_row.created_at else "",
                      "expires_at": inv_row.state.get("expires_at"),
                      "cost_total": float(inv_row.state.get("cost_total", 0))}]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(session, doc_entity_id=doc_id, doc_state=doc_row.state,
                          pick_result=pick_result, company_id=_setup_ids["company_id"],
                          user_id=str(_setup_ids["user_id"]), doc_type="invoice")
    await session.commit()

    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == _setup_ids["company_id"],
        LedgerEntry.entity_id == item_id,
        LedgerEntry.event_type == "item.fulfilled"))).scalars().all()
    assert rows, "item.fulfilled must be emitted on the sold item"
    data = rows[0].data
    assert data["source_doc_id"] == doc_id
    # doc_number is the doc's real current number (finalize renumbers it), which can differ
    # from the entity_id suffix — exactly why it must be captured at emit time.
    expected_num = doc_row.state.get("doc_number") or doc_row.state.get("ref_id")
    assert expected_num and data["doc_number"] == expected_num

    from ui.components.activity import activity_table
    from fasthtml.common import to_xml
    html = to_xml(activity_table([{"id": rows[0].id, "event_type": "item.fulfilled",
                                   "entity_id": item_id, "ts": "2026-06-21T10:00:00+00:00", "data": data}],
                                 subject_entity_id=item_id))
    assert expected_num in html and f"/docs/{doc_id}" in html


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
        "created_at": "", "expires_at": None, "cost_total": 30.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Verify item is sold and qty preserved (= fulfilled qty) after fulfillment
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert float(inv_row.state.get("quantity", 0)) == 10

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
    assert inv_row.state.get("status") == "available"

    # Verify doc fulfillment cleared
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") is None


@pytest.mark.asyncio
async def test_void_blocked_while_fulfilled_and_state_untouched(client, session, auth, _setup_ids):
    """Voiding a fulfilled doc is REFUSED (goods must come back first), and the
    refused attempt changes nothing: fulfillment state and stock stay as they were."""
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
        "created_at": "", "expires_at": None, "cost_total": 50.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Void via API: blocked while goods are out (revert fulfillment first).
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={"reason": "test"})
    assert r.status_code == 409, r.text
    assert "revert fulfillment" in r.json()["detail"].lower()

    # The refused void changes nothing: fulfillment state and stock untouched.
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    assert doc_row.state.get("fulfillment_status") == "fulfilled"
    assert doc_row.state.get("status") != "void"
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert float(inv_row.state.get("quantity", 0)) > 0
    assert inv_row.state.get("status") == "sold"


@pytest.mark.asyncio
async def test_revert_to_draft_blocked_when_fulfilled_items_exist(client, session, auth, _setup_ids):
    """Reverting a fulfilled doc to draft must be blocked while any line items are fulfilled."""
    sku = f"REVERT-BLOCK-{uuid.uuid4().hex[:6]}"
    item_id = await _create_item(client, auth, sku, 1, cost_price=4.0)
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 12.0, "entity_id": item_id},
    ])

    # Fulfill via API
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text

    # Revert to draft must be blocked while items are fulfilled
    r = await client.post(f"/docs/{doc_id}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert "fulfilled" in r.text.lower()


async def test_revert_to_draft_allowed_after_all_lines_reverted(client, session, auth, _setup_ids):
    """Revert to draft must succeed once all fulfilled line items have been reverted."""
    sku = f"REVERT-OK-{uuid.uuid4().hex[:6]}"
    item_id = await _create_item(client, auth, sku, 1, cost_price=4.0)
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 12.0, "entity_id": item_id},
    ])

    # Fulfill via API
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text

    # Revert fulfillment first
    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text

    # Now revert to draft must succeed
    r = await client.post(f"/docs/{doc_id}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text

    from celerp.models.projections import Projection
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert inv_row.state.get("status") == "available"



@pytest.mark.asyncio
async def test_unvoid_does_not_auto_refulfill(client, session, auth, _setup_ids):
    """Unvoiding a doc must NOT auto-re-fulfill it (fulfillment is always manual)."""
    from celerp.models.projections import Projection
    from celerp.services.fulfill import execute_fulfill
    from celerp.services.pick import compute_pick_plan

    item_id = await _create_item(client, auth, "UNVOID-A", 10, cost_price=2.0)
    # entity_id binds the line to the parcel, and the FULL quantity is drawn so the
    # parcel itself goes 'sold' (a partial draw splits off a child instead) -
    # revert-lines then operates on this item directly.
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": "UNVOID-A", "quantity": 10, "unit_price": 6.0, "entity_id": item_id},
    ])

    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    available_inv = [{
        "entity_id": item_id, "sku": "UNVOID-A", "quantity": 10,
        "created_at": "", "expires_at": None, "cost_total": 20.0,
    }]
    pick_result = compute_pick_plan(doc_row.state.get("line_items", []), available_inv)
    await execute_fulfill(
        session, doc_entity_id=doc_id, doc_state=doc_row.state,
        pick_result=pick_result, company_id=_setup_ids["company_id"],
        user_id=str(_setup_ids["user_id"]),
    )
    await session.commit()

    # Void requires the goods back first: revert fulfillment, then void.
    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={"reason": "test void"})
    assert r.status_code == 200, r.text

    # Unvoid the doc
    r = await client.post(f"/docs/{doc_id}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200

    # Unvoiding must NOT auto-re-fulfill: fulfillment stays reverted (manual only).
    doc_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": doc_id})
    fs = doc_row.state.get("fulfillment_status")
    assert fs in (None, "unfulfilled"), \
        f"unvoid must not auto-re-fulfill; expected no fulfillment_status, got: {fs}"
    inv_row = await session.get(Projection, {"company_id": _setup_ids["company_id"], "entity_id": item_id})
    assert inv_row.state.get("status") == "available", \
        "stock reverted before the void must stay available through unvoid"


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
        "created_at": "", "expires_at": None, "cost_total": 35.0,
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
        inventory = [{"entity_id": "e1", "sku": "NS-A", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_total": 50.0, "allow_splitting": False}]
        result = compute_pick_plan(line_items, inventory)
        # The item has qty 10 > 3 needed; cannot split -> unfulfilled
        assert result.picks == []
        assert len(result.unfulfilled) == 1
        assert result.unfulfilled[0]["sku"] == "NS-A"

    def test_full_pick_still_works_when_allow_splitting_false(self):
        """Full pick (no split needed) must still succeed when allow_splitting=False."""
        line_items = [{"sku": "NS-B", "quantity": 10}]
        inventory = [{"entity_id": "e2", "sku": "NS-B", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_total": 20.0, "allow_splitting": False}]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "full"
        assert result.unfulfilled == []

    def test_split_when_allow_splitting_true(self):
        """Items with allow_splitting=True must be split as usual."""
        line_items = [{"sku": "SP-A", "quantity": 3}]
        inventory = [{"entity_id": "e3", "sku": "SP-A", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_total": 10.0, "allow_splitting": True}]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "split"
        assert result.unfulfilled == []

    def test_split_when_allow_splitting_missing_defaults_to_allowed(self):
        """Items without allow_splitting key default to splittable (backward compat for existing data)."""
        line_items = [{"sku": "SP-B", "quantity": 3}]
        inventory = [{"entity_id": "e4", "sku": "SP-B", "quantity": 10, "created_at": "2026-01-01", "expires_at": None, "cost_total": 10.0}]
        result = compute_pick_plan(line_items, inventory)
        assert len(result.picks) == 1
        assert result.picks[0].action == "split"


# ---------------------------------------------------------------------------
# consignment_in fulfillment: must NOT deduct inventory
# ---------------------------------------------------------------------------

async def _create_consignment_in(client, auth, line_items) -> str:
    """Create a finalized consignment_in document."""
    total = sum(li.get("quantity", 0) * li.get("unit_price", 0) for li in line_items)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "consignment_in",
        "ref_id": f"CONS-{uuid.uuid4().hex[:6]}",
        "line_items": line_items,
        "total": total,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r2.status_code == 200, r2.text
    return doc_id



# ---------------------------------------------------------------------------
# Helpers for memo tests
# ---------------------------------------------------------------------------


async def _create_memo(client, auth, line_items, contact_id=None) -> str:
    """Create a finalized memo document. line_items should include entity_id for linked items."""
    total = sum(li.get("quantity", 0) * li.get("unit_price", 0) for li in line_items)
    payload = {
        "doc_type": "memo",
        "ref_id": f"MEMO-{uuid.uuid4().hex[:6]}",
        "line_items": line_items,
        "total": total,
    }
    if contact_id:
        payload["contact_id"] = contact_id
    r = await client.post("/docs", headers=auth["headers"], json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r2.status_code == 200, r2.text
    return doc_id


# ---------------------------------------------------------------------------
# Update existing consignment_in tests to use /fulfill-lines
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# New tests: per-line fulfill/revert on memo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fulfill_lines_marks_items_memo_out(client, session, auth, _setup_ids):
    """fulfill-lines on a memo marks selected items memo_out and doc partially_fulfilled."""
    sku_a = f"MEMO-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"MEMO-B-{uuid.uuid4().hex[:6]}"
    sku_c = f"MEMO-C-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=100.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=200.0)
    eid_c = await _create_item(client, auth, sku_c, 1, cost_price=300.0)

    doc_id = await _create_memo(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 100.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 200.0, "entity_id": eid_b},
        {"sku": sku_c, "name": sku_c, "quantity": 1, "unit_price": 300.0, "entity_id": eid_c},
    ])

    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid_a, eid_b]})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["fulfillment_status"] == "partial"
    assert set(result["fulfilled"]) == {eid_a, eid_b}

    # Items A and B are memo_out; C is still available
    ra = await client.get(f"/items/{eid_a}", headers=auth["headers"])
    assert ra.json()["status"] == "memo_out"
    rb = await client.get(f"/items/{eid_b}", headers=auth["headers"])
    assert rb.json()["status"] == "memo_out"
    rc = await client.get(f"/items/{eid_c}", headers=auth["headers"])
    assert rc.json()["status"] == "available"

    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc.get("fulfillment_status") in ("partial", "partially_fulfilled")


@pytest.mark.asyncio
async def test_fulfill_lines_all_items_marks_doc_fulfilled(client, session, auth, _setup_ids):
    """fulfill-lines on all items marks doc fulfilled."""
    sku_a = f"MEMO-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"MEMO-B-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=50.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=75.0)

    doc_id = await _create_memo(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 50.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 75.0, "entity_id": eid_b},
    ])

    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid_a, eid_b]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "fulfilled"

    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc.get("fulfillment_status") == "fulfilled"


@pytest.mark.asyncio
async def test_fulfill_lines_rejects_already_memo_out(client, session, auth, _setup_ids):
    """fulfill-lines rejects items that are already memo_out."""
    sku = f"MEMO-{uuid.uuid4().hex[:6]}"
    eid = await _create_item(client, auth, sku, 1, cost_price=50.0)
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 50.0, "entity_id": eid},
    ])

    # First fulfill
    r1 = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid]})
    assert r1.status_code == 200, r1.text

    # Second fulfill on same item should 422
    r2 = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid]})
    assert r2.status_code == 422, r2.text
    body = r2.json()
    assert "errors" in body or "detail" in body


@pytest.mark.asyncio
async def test_fulfill_lines_no_cogs_je_on_memo(client, session, auth, _setup_ids):
    """fulfill-lines on a memo must NOT create a COGS journal entry."""
    sku = f"MEMO-{uuid.uuid4().hex[:6]}"
    eid = await _create_item(client, auth, sku, 1, cost_price=500.0)
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 500.0, "entity_id": eid},
    ])

    r_before = await client.get("/ledger?entity_type=journal_entry", headers=auth["headers"])
    je_count_before = len(r_before.json().get("items", []))

    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid]})
    assert r.status_code == 200, r.text

    r_after = await client.get("/ledger?entity_type=journal_entry", headers=auth["headers"])
    je_count_after = len(r_after.json().get("items", []))
    assert je_count_after == je_count_before, (
        f"memo fulfill-lines must not create COGS JE: before={je_count_before}, after={je_count_after}"
    )


@pytest.mark.asyncio
async def test_revert_lines_marks_items_available(client, session, auth, _setup_ids):
    """revert-lines restores memo_out items to available."""
    sku_a = f"MEMO-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"MEMO-B-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=100.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=200.0)

    doc_id = await _create_memo(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 100.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 200.0, "entity_id": eid_b},
    ])

    # Fulfill both
    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid_a, eid_b]})

    # Revert only A
    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid_a]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "partial"

    ra = await client.get(f"/items/{eid_a}", headers=auth["headers"])
    assert ra.json()["status"] == "available"

    rb = await client.get(f"/items/{eid_b}", headers=auth["headers"])
    assert rb.json()["status"] == "memo_out"


@pytest.mark.asyncio
async def test_revert_lines_rejects_available_item(client, session, auth, _setup_ids):
    """revert-lines rejects items that are already available."""
    sku = f"MEMO-{uuid.uuid4().hex[:6]}"
    eid = await _create_item(client, auth, sku, 1, cost_price=50.0)
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 50.0, "entity_id": eid},
    ])

    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid]})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_revert_lines_all_items_clears_fulfillment(client, session, auth, _setup_ids):
    """Reverting all fulfilled items sets doc fulfillment_status back to unfulfilled."""
    sku_a = f"MEMO-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"MEMO-B-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=100.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=200.0)

    doc_id = await _create_memo(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 100.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 200.0, "entity_id": eid_b},
    ])

    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid_a, eid_b]})

    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid_a, eid_b]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "unfulfilled"

    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc.get("fulfillment_status") in (None, "", "unfulfilled")


@pytest.mark.asyncio
async def test_memo_to_invoice_conversion_excludes_reverted_items(client, session, auth, _setup_ids):
    """Memo→invoice conversion only carries memo_out items; reverted items are excluded."""
    sku_a = f"MEMO-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"MEMO-B-{uuid.uuid4().hex[:6]}"
    sku_c = f"MEMO-C-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=100.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=200.0)
    eid_c = await _create_item(client, auth, sku_c, 1, cost_price=300.0)

    doc_id = await _create_memo(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 100.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 200.0, "entity_id": eid_b},
        {"sku": sku_c, "name": sku_c, "quantity": 1, "unit_price": 300.0, "entity_id": eid_c},
    ])

    # Fulfill A and B; revert A → only B is memo_out
    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid_a, eid_b]})
    await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid_a]})

    r = await client.post(f"/docs/{doc_id}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    invoice_id = r.json()["target_doc_id"]

    invoice = (await client.get(f"/docs/{invoice_id}", headers=auth["headers"])).json()
    invoice_skus = [li.get("sku") for li in invoice.get("line_items", [])]
    assert sku_b in invoice_skus, f"Expected {sku_b} in invoice; got {invoice_skus}"
    assert sku_a not in invoice_skus, f"Reverted item {sku_a} must not be in invoice"
    assert sku_c not in invoice_skus, f"Never-fulfilled item {sku_c} must not be in invoice"


@pytest.mark.asyncio
async def test_memo_to_invoice_fails_when_no_memo_out_items(client, session, auth, _setup_ids):
    """Memo→invoice conversion 422s when no items are memo_out."""
    sku = f"MEMO-{uuid.uuid4().hex[:6]}"
    eid = await _create_item(client, auth, sku, 1, cost_price=100.0)
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 100.0, "entity_id": eid},
    ])

    # Don't fulfill - all items still available
    r = await client.post(f"/docs/{doc_id}/convert", headers=auth["headers"])
    assert r.status_code == 422, r.text
    assert "memo_out" in r.text.lower() or "on memo" in r.text.lower()


@pytest.mark.asyncio
async def test_old_fulfill_endpoint_removed(client, session, auth, _setup_ids):
    """POST /docs/{id}/fulfill no longer exists."""
    sku = f"TEST-{uuid.uuid4().hex[:6]}"
    await _create_item(client, auth, sku, 1)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 10.0},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill", headers=auth["headers"])
    assert r.status_code in (404, 405), f"Expected 404/405, got {r.status_code}"


@pytest.mark.asyncio
async def test_old_unfulfill_endpoint_removed(client, session, auth, _setup_ids):
    """POST /docs/{id}/unfulfill no longer exists."""
    sku = f"TEST-{uuid.uuid4().hex[:6]}"
    await _create_item(client, auth, sku, 1)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 10.0},
    ])
    r = await client.post(f"/docs/{doc_id}/unfulfill", headers=auth["headers"])
    assert r.status_code in (404, 405), f"Expected 404/405, got {r.status_code}"


@pytest.mark.asyncio
async def test_fulfill_lines_invoice_marks_items_sold(client, session, auth, _setup_ids):
    """fulfill-lines on a finalized invoice marks items sold and doc partially/fully fulfilled."""
    sku_a = f"INV-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"INV-B-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=100.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=200.0)

    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 100.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 200.0, "entity_id": eid_b},
    ])

    # Partial fulfill: one item
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid_a]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "partial"

    item_a = (await client.get(f"/items/{eid_a}", headers=auth["headers"])).json()
    assert item_a["status"] == "sold", f"Expected sold, got {item_a['status']}"

    # Full fulfill: remaining item
    r2 = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid_b]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["fulfillment_status"] == "fulfilled"

    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc.get("fulfillment_status") == "fulfilled"


@pytest.mark.asyncio
async def test_fulfill_lines_rejects_empty_line_entity_ids(client, session, auth, _setup_ids):
    """fulfill-lines with empty line_entity_ids must return 422 for non-inbound docs."""
    sku = f"EMPTY-{uuid.uuid4().hex[:6]}"
    await _create_item(client, auth, sku, 1)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 50.0},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": []})
    assert r.status_code == 422, f"Expected 422 for empty line_entity_ids, got {r.status_code}"


@pytest.mark.asyncio
async def test_revert_lines_rejects_empty_line_entity_ids(client, session, auth, _setup_ids):
    """revert-lines with empty line_entity_ids must return 422 for non-inbound docs."""
    sku = f"EMPTY-{uuid.uuid4().hex[:6]}"
    eid = await _create_item(client, auth, sku, 1)
    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": sku, "name": sku, "quantity": 1, "unit_price": 50.0, "entity_id": eid},
    ])
    # First fulfill
    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid]})
    # Now revert with empty list
    r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                          json={"line_entity_ids": []})
    assert r.status_code == 422, f"Expected 422 for empty line_entity_ids, got {r.status_code}"


@pytest.mark.asyncio
async def test_invoice_finalize_promotes_memo_out_items_to_sold(client, session, auth, _setup_ids):
    """Finalizing an invoice converts memo_out line items to sold.

    Flow: create memo → fulfill items (→ memo_out) → convert to invoice → finalize invoice
    → assert items are sold, not memo_out.
    """
    sku_a = f"MEMO-SELL-A-{uuid.uuid4().hex[:6]}"
    sku_b = f"MEMO-SELL-B-{uuid.uuid4().hex[:6]}"

    eid_a = await _create_item(client, auth, sku_a, 1, cost_price=100.0)
    eid_b = await _create_item(client, auth, sku_b, 1, cost_price=200.0)

    doc_id = await _create_memo(client, auth, [
        {"sku": sku_a, "name": sku_a, "quantity": 1, "unit_price": 150.0, "entity_id": eid_a},
        {"sku": sku_b, "name": sku_b, "quantity": 1, "unit_price": 250.0, "entity_id": eid_b},
    ])

    # Fulfill both items on the memo → they become memo_out
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [eid_a, eid_b]})
    assert r.status_code == 200, r.text

    item_a = (await client.get(f"/items/{eid_a}", headers=auth["headers"])).json()
    item_b = (await client.get(f"/items/{eid_b}", headers=auth["headers"])).json()
    assert item_a["status"] == "memo_out", f"Expected memo_out, got {item_a['status']}"
    assert item_b["status"] == "memo_out", f"Expected memo_out, got {item_b['status']}"

    # Convert memo to invoice
    r = await client.post(f"/docs/{doc_id}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    invoice_id = r.json()["target_doc_id"]

    # Finalize the invoice → memo_out items must become sold
    r = await client.post(f"/docs/{invoice_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text

    item_a = (await client.get(f"/items/{eid_a}", headers=auth["headers"])).json()
    item_b = (await client.get(f"/items/{eid_b}", headers=auth["headers"])).json()
    assert item_a["status"] == "sold", f"Expected sold after invoice finalize, got {item_a['status']}"
    assert item_b["status"] == "sold", f"Expected sold after invoice finalize, got {item_b['status']}"


@pytest.mark.asyncio
async def test_patch_doc_cannot_delete_fulfilled_line_item(client, session, auth, _setup_ids):
    """Fix 3: PATCH doc with line_items.new that omits a fulfilled entity_id must be rejected.

    Sets up the state via the service layer (bypassing Fix 1) to test the PATCH guard
    independently: draft doc + item in sold state (simulates a forced/migrated state).
    """
    import uuid as _uuid
    from celerp.events.engine import emit_event

    sku = f"FIX3-GUARD-{_uuid.uuid4().hex[:6]}"
    item_id = await _create_item(client, auth, sku, 1, cost_price=10.0)

    # Create a draft doc with explicit entity_id on the line item
    total = 1 * 20.0
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo",
        "ref_id": f"FIX3-{_uuid.uuid4().hex[:6]}",
        "line_items": [{"sku": sku, "name": sku, "quantity": 1, "unit_price": 20.0, "entity_id": item_id}],
        "total": total,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    # Doc is draft at this point

    # Set item status to sold via service layer (bypassing HTTP guards)
    await emit_event(
        session,
        company_id=_setup_ids["company_id"],
        entity_id=item_id,
        entity_type="item",
        event_type="item.status.set",
        data={"new_status": "sold"},
        actor_id=_setup_ids["user_id"],
        location_id=None,
        source="test",
        idempotency_key=str(_uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    # Try to PATCH the draft doc to remove the fulfilled line item → must be rejected
    doc_state = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    remaining_lines = [
        li for li in doc_state.get("line_items", [])
        if (li.get("entity_id") or li.get("item_id")) != item_id
    ]
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"new": remaining_lines}},
    })
    assert r.status_code == 409, r.text
    assert "fulfilled" in r.text.lower()


@pytest.mark.asyncio
async def test_fulfill_re_fulfill_after_revert(client, auth, _setup_ids):
    """Fulfill → revert-lines → re-fulfill must succeed (no idempotency key collision on COGS JE)."""
    # Create invoice with one inventory item
    item_r = await client.post("/items", headers=auth["headers"], json={
        "sku": "REFULFILL-001", "name": "Re-fulfill test item", "quantity": 1,
        "cost_price": 100.0, "unit_price": 200.0, "sell_by": "piece",
    })
    assert item_r.status_code == 200, item_r.text
    item_id = item_r.json()["id"]

    inv_r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "currency": "USD",
        "line_items": [{"sku": "REFULFILL-001", "quantity": 1, "unit_price": 200.0, "entity_id": item_id}],
    })
    assert inv_r.status_code == 200, inv_r.text
    doc_id = inv_r.json()["id"]

    # Finalize
    fin_r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert fin_r.status_code == 200, fin_r.text

    # First fulfill
    r1 = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [item_id]})
    assert r1.status_code == 200, r1.text
    assert r1.json()["fulfillment_status"] == "fulfilled"

    item_state = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item_state["status"] == "sold", f"Expected sold after first fulfill, got {item_state['status']}"

    # Revert lines
    rv_r = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                              json={"line_entity_ids": [item_id]})
    assert rv_r.status_code == 200, rv_r.text

    item_state = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item_state["status"] == "available", f"Expected available after revert, got {item_state['status']}"

    # Re-fulfill — must succeed without idempotency key collision
    r2 = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [item_id]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["fulfillment_status"] == "fulfilled"

    item_state = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item_state["status"] == "sold", f"Expected sold after re-fulfill, got {item_state['status']}"


@pytest.mark.asyncio
async def test_fulfill_lines_deduplicates_duplicate_ids(client, auth, _setup_ids):
    """Submitting the same entity_id twice in one fulfill-lines call must only fulfill it once.

    Regression: strip_empty did not de-duplicate, so a duplicate ID would emit
    item.fulfilled twice for the same item, corrupting quantity_fulfilled and COGS.
    """
    item_id = await _create_item(client, auth, "DEDUP-FL-001", 5, cost_price=10)
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "DEDUP-FL-001", "entity_id": item_id, "quantity": 5, "unit_price": 20}],
    )

    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id, item_id, item_id]})
    assert r.status_code == 200, r.text
    body = r.json()
    # Must appear exactly once in the fulfilled list despite 3 submissions
    assert body["fulfilled"].count(item_id) == 1, f"Expected 1 occurrence, got: {body['fulfilled']}"
    assert body["fulfillment_status"] == "fulfilled"

    item = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item["status"] == "sold"


@pytest.mark.asyncio
async def test_revert_lines_deduplicates_duplicate_ids(client, auth, _setup_ids):
    """Submitting the same entity_id twice in one revert-lines call must only revert it once."""
    item_id = await _create_item(client, auth, "DEDUP-RL-001", 5, cost_price=10)
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "DEDUP-RL-001", "entity_id": item_id, "quantity": 5, "unit_price": 20}],
    )
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text

    rv = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                           json={"line_entity_ids": [item_id, item_id]})
    assert rv.status_code == 200, rv.text
    body = rv.json()
    assert body["reverted"].count(item_id) == 1, f"Expected 1 occurrence, got: {body['reverted']}"

    item = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item["status"] == "available"


# ---------------------------------------------------------------------------
# Stock guard on fulfill-lines (Phase 1: prohibit over-commit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fulfill_lines_rejects_stock_shortage(client, auth):
    """Invoicing more than the parcel's stock must reject the whole fulfill (409)."""
    item_id = await _create_item(client, auth, "SHORTAGE-A", 1)
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SHORTAGE-A", "entity_id": item_id, "quantity": 999, "unit_price": 1.0}],
    )
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 409, r.text
    assert "stock" in r.json()["detail"].lower()
    # Nothing was fulfilled — the item stays available.
    item = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item["status"] == "available"


@pytest.mark.asyncio
async def test_fulfill_lines_rejects_partial_when_splitting_off(client, auth):
    """A partial draw of a non-splittable item must reject (409) — can't split."""
    item_id = await _create_item(client, auth, "NOSPLIT-A", 10)
    pr = await client.patch(f"/items/{item_id}", headers=auth["headers"],
                            json={"fields_changed": {"allow_splitting": {"old": True, "new": False}}})
    assert pr.status_code == 200, pr.text
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "NOSPLIT-A", "entity_id": item_id, "quantity": 3, "unit_price": 1.0}],
    )
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 409, r.text
    assert "splitting" in r.json()["detail"].lower()
    item = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert item["status"] == "available"


# ---------------------------------------------------------------------------
# Phase 2: auto-split a splittable parcel on partial fulfillment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fulfill_split_partial_generic(client, auth):
    """Partial fulfill of a splittable liter parcel splits off a child; the mother keeps
    the remainder, and the split-off child KEEPS the parent SKU (same product, distinct
    lot by barcode/entity_id) - no '.N' suffix. The doc line points at the child."""
    item_id = await _create_item(client, auth, "SPLIT-L", 10, sell_by="liter")
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SPLIT-L", "entity_id": item_id, "quantity": 3, "unit_price": 1.0}],
    )
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text

    mother = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert mother["status"] == "available"
    assert float(mother["quantity"]) == 7

    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    li = doc["line_items"][0]
    assert li["sku"] == "SPLIT-L"  # child keeps the parent SKU (no '.1')
    child_eid = li.get("entity_id") or li.get("item_id")
    assert child_eid and child_eid != item_id
    child = (await client.get(f"/items/{child_eid}", headers=auth["headers"])).json()
    assert child["sku"] == "SPLIT-L"                 # same product as the mother
    assert child.get("barcode") and child["barcode"] != mother.get("barcode")  # distinct lot
    assert float(child["quantity"]) == 3
    assert child["status"] != "available"  # the child was fulfilled


@pytest.mark.asyncio
async def test_fulfill_split_piece_item_with_pieces(client, auth):
    """A piece-sold parcel splits when the line carries pieces == qty."""
    item_id = await _create_item(client, auth, "SPLIT-P2", 10, sell_by="piece")
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SPLIT-P2", "entity_id": item_id, "quantity": 3, "pieces": 3, "unit_price": 1.0}],
    )
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text
    mother = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert float(mother["quantity"]) == 7


@pytest.mark.asyncio
async def test_fulfill_split_piece_item_derives_pieces(client, auth):
    """A piece-sold parcel splits on a partial draw even without pieces on the line:
    the quantity IS the pieces, so the child pieces are derived from qty."""
    item_id = await _create_item(client, auth, "SPLIT-P3", 10, sell_by="piece")
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SPLIT-P3", "entity_id": item_id, "quantity": 3, "unit_price": 1.0}],
    )
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [item_id]})
    assert r.status_code == 200, r.text
    mother = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert mother["status"] == "available"
    assert float(mother["quantity"]) == 7  # the 3-piece child was split off


@pytest.mark.asyncio
async def test_fulfill_split_secondary_overshoot_floors_mother(client, auth):
    """Partial draw: the child's secondary measure is uncapped (may exceed the parcel)
    and the mother floors at 0. sell_by=piece, mother 5pcs/15ct -> child 3pcs/20ct,
    mother 2pcs/0ct."""
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": "SPLIT-SEC", "name": "SPLIT-SEC", "quantity": 5, "sell_by": "piece",
        "weight": 15, "weight_unit": "carat",
    })
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SPLIT-SEC", "entity_id": item_id, "quantity": 3, "weight": 20, "unit_price": 1.0}],
    )
    fr = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [item_id]})
    assert fr.status_code == 200, fr.text
    mother = (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()
    assert float(mother["quantity"]) == 2            # 5 - 3
    assert float(mother.get("weight") or 0) == 0     # max(0, 15 - 20)
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    li = doc["line_items"][0]
    child = (await client.get(f"/items/{li.get('entity_id') or li.get('item_id')}", headers=auth["headers"])).json()
    assert float(child["quantity"]) == 3
    assert float(child["weight"]) == 20              # uncapped


@pytest.mark.asyncio
async def test_fulfill_whole_draw_secondary_must_equal(client, auth):
    """A whole-quantity draw must take all the secondary measure — a reduced weight is rejected."""
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": "SPLIT-WHOLE", "name": "SPLIT-WHOLE", "quantity": 4, "sell_by": "piece",
        "weight": 12, "weight_unit": "carat",
    })
    item_id = r.json()["id"]
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SPLIT-WHOLE", "entity_id": item_id, "quantity": 4, "weight": 10, "unit_price": 1.0}],
    )
    fr = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [item_id]})
    assert fr.status_code == 409, fr.text
    assert "whole" in fr.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fulfill_whole_draw_secondary_equal_ok(client, auth):
    """A whole-quantity draw with the matching secondary measure fulfills the parcel."""
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": "SPLIT-WHOLE-OK", "name": "SPLIT-WHOLE-OK", "quantity": 4, "sell_by": "piece",
        "weight": 12, "weight_unit": "carat",
    })
    item_id = r.json()["id"]
    doc_id = await _create_and_finalize_invoice(
        client, auth,
        [{"sku": "SPLIT-WHOLE-OK", "entity_id": item_id, "quantity": 4, "weight": 12, "unit_price": 1.0}],
    )
    fr = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [item_id]})
    assert fr.status_code == 200, fr.text


@pytest.mark.asyncio
async def test_fulfill_spans_across_lots_specific_id_cogs(client, session, auth, _setup_ids):
    """Cross-lot spanning: a splittable line whose qty exceeds its bound lot draws the
    shortfall from another lot of the same SKU (bound lot first, then FIFO). Both lots
    are consumed and COGS is each lot's own cost (specific identification by lot)."""
    h = auth["headers"]
    sku = f"SPAN-{uuid.uuid4().hex[:6]}"
    ra = await client.post("/items", headers=h, json={"sku": sku, "name": sku, "quantity": 60,
                           "sell_by": "piece", "cost_total": 60.0, "allow_splitting": True})
    assert ra.status_code == 200, ra.text
    lot_a = ra.json()["id"]
    rb = await client.post("/items", headers=h, json={"sku": sku, "name": sku, "quantity": 40,
                           "sell_by": "piece", "cost_total": 80.0, "allow_splitting": True})
    assert rb.status_code == 200, rb.text
    lot_b = rb.json()["id"]

    doc_id = await _create_and_finalize_invoice(client, auth, [
        {"sku": sku, "name": sku, "quantity": 100, "unit_price": 5.0, "entity_id": lot_a},
    ])
    r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=h, json={"line_entity_ids": [lot_a]})
    assert r.status_code == 200, r.text
    assert r.json()["fulfillment_status"] == "fulfilled"

    # Both lots consumed by the span (bound lot A + spanned lot B), not just the bound one.
    assert (await client.get(f"/items/{lot_a}", headers=h)).json()["status"] == "sold"
    assert (await client.get(f"/items/{lot_b}", headers=h)).json()["status"] == "sold"

    # COGS = specific cost of the drawn lots: 60 (lot A) + 80 (lot B) = 140.
    led = (await client.get(f"/ledger?entity_id={doc_id}", headers=h)).json()["items"]
    fulfilled = next((e for e in led if e.get("event_type") == "doc.fulfilled"), None)
    assert fulfilled is not None
    assert abs(float(fulfilled["data"].get("total_cogs", 0)) - 140.0) < 1e-6


# ---------------------------------------------------------------------------
# Contact-scoped holdings filter: items currently out on memo to a customer.
# GET /items?on_memo_to=<contact_id> (celerp.services.holdings.memo_holdings).
# ---------------------------------------------------------------------------


async def _memo_out(client, auth, sku, unit_price, contact_id, cost_price=0.0, retail_price=None):
    """Create an item, put it on memo to `contact_id` at `unit_price`, return its id."""
    data = {"sku": sku, "name": sku, "quantity": 1, "sell_by": "piece"}
    if cost_price:
        data["cost_total"] = cost_price
    if retail_price is not None:
        data["retail_price"] = retail_price
    r = await client.post("/items", headers=auth["headers"], json=data)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    doc_id = await _create_memo(
        client, auth,
        [{"sku": sku, "name": sku, "quantity": 1, "unit_price": unit_price, "entity_id": eid}],
        contact_id=contact_id,
    )
    fr = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid]})
    assert fr.status_code == 200, fr.text
    return eid, doc_id


@pytest.mark.asyncio
async def test_items_on_memo_to_lists_outstanding_at_quoted_price(client, session, auth, _setup_ids):
    cust = "contact:custA"
    sku_a, sku_b = f"HOL-A-{uuid.uuid4().hex[:6]}", f"HOL-B-{uuid.uuid4().hex[:6]}"
    eid_a, _ = await _memo_out(client, auth, sku_a, 500.0, cust)
    eid_b, _ = await _memo_out(client, auth, sku_b, 750.0, cust)

    resp = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert {i["id"] for i in resp["items"]} == {eid_a, eid_b}
    assert {i["id"]: i["holding_value"] for i in resp["items"]} == {eid_a: 500.0, eid_b: 750.0}
    assert resp["value_total"] == 1250.0


@pytest.mark.asyncio
async def test_items_on_memo_to_uses_quoted_override_not_catalog(client, session, auth, _setup_ids):
    """The value is the price quoted on the memo line, never the item's catalog/retail price."""
    cust = "contact:custOverride"
    sku = f"HOL-OV-{uuid.uuid4().hex[:6]}"
    # Catalog/retail says 1000, but the customer was quoted 800 on the memo.
    eid, _ = await _memo_out(client, auth, sku, 800.0, cust, cost_price=400.0, retail_price=1000.0)

    resp = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert resp["value_total"] == 800.0
    (item,) = resp["items"]
    assert item["holding_value"] == 800.0
    # Prove it is genuinely the quoted price, not the catalog value carried on the item.
    assert item.get("retail_price") in (1000.0, None) and item["holding_value"] != 1000.0


@pytest.mark.asyncio
async def test_items_on_memo_to_excludes_other_customer(client, session, auth, _setup_ids):
    a, b = "contact:memoA", "contact:memoB"
    eid_a, _ = await _memo_out(client, auth, f"HOL-CA-{uuid.uuid4().hex[:6]}", 100.0, a)
    await _memo_out(client, auth, f"HOL-CB-{uuid.uuid4().hex[:6]}", 100.0, b)

    resp = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": a})).json()
    assert {i["id"] for i in resp["items"]} == {eid_a}
    assert resp["value_total"] == 100.0


@pytest.mark.asyncio
async def test_items_on_memo_to_excludes_returned_item(client, session, auth, _setup_ids):
    cust = "contact:memoReturn"
    eid, doc_id = await _memo_out(client, auth, f"HOL-R-{uuid.uuid4().hex[:6]}", 300.0, cust)
    # Return the memo line: item goes back to available and drops out of the scope.
    rr = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid]})
    assert rr.status_code == 200, rr.text

    resp = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert resp["items"] == []
    assert resp["value_total"] == 0.0


@pytest.mark.asyncio
async def test_items_on_memo_to_aggregates_multiple_memos(client, session, auth, _setup_ids):
    """The whole point of the feature: several open memos to one customer, one list + total."""
    cust = "contact:memoMulti"
    eid_1, _ = await _memo_out(client, auth, f"HOL-M1-{uuid.uuid4().hex[:6]}", 400.0, cust)
    eid_2, _ = await _memo_out(client, auth, f"HOL-M2-{uuid.uuid4().hex[:6]}", 600.0, cust)  # separate memo doc

    resp = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert {i["id"] for i in resp["items"]} == {eid_1, eid_2}
    assert resp["value_total"] == 1000.0


@pytest.mark.asyncio
async def test_items_value_total_reconciles_with_listed_items(client, session, auth, _setup_ids):
    """Keystone: value_total equals the sum of the per-item holding_value shown in the list."""
    cust = "contact:memoRecon"
    await _memo_out(client, auth, f"HOL-X1-{uuid.uuid4().hex[:6]}", 123.45, cust)
    await _memo_out(client, auth, f"HOL-X2-{uuid.uuid4().hex[:6]}", 678.90, cust)

    resp = (await client.get("/items", headers=auth["headers"],
                             params={"on_memo_to": cust, "limit": 999})).json()
    listed = round(sum(float(i["holding_value"]) for i in resp["items"]), 2)
    assert resp["value_total"] == listed == 802.35


# ---------------------------------------------------------------------------
# Partial memo returns: a customer sends back part of a lot and keeps the rest.
# The balance must stay out on memo, not be written off as returned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_memo_return_keeps_balance_out_with_customer(client, session, auth, _setup_ids):
    cust = "contact:partialMemo"
    sku = f"PM-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"],
                          json={"sku": sku, "name": sku, "quantity": 7, "sell_by": "piece",
                                "cost_total": 700.0})
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 7, "unit_price": 100.0, "entity_id": eid},
    ], contact_id=cust)
    fr = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid]})
    assert fr.status_code == 200, fr.text

    before = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert before["value_total"] == 700.0

    # Customer returns 3, keeps 4.
    rr = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid], "quantities": {eid: 3}})
    assert rr.status_code == 200, rr.text
    assert rr.json()["reverted"] == [], "the line is still out, so nothing was fully reverted"
    (returned,) = rr.json()["partially_returned"]
    assert returned["quantity"] == 3

    # The 4 they kept are still out on memo, at the price they were quoted.
    after = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert {i["id"] for i in after["items"]} == {eid}, "the balance must stay out with the customer"
    (still_out,) = after["items"]
    assert still_out["quantity"] == 4.0
    assert still_out["status"] == "memo_out"
    assert after["value_total"] == 400.0, "4 units still out at the quoted 100 each"

    # The 3 that came back are in stock, and are not counted as still out.
    returned_item = (await client.get(f"/items/{returned['item_id']}", headers=auth["headers"])).json()
    assert returned_item["status"] == "available"
    assert returned_item["quantity"] == 3.0
    assert returned_item["id"] not in {i["id"] for i in after["items"]}


@pytest.mark.asyncio
async def test_full_quantity_revert_still_reverts_whole_line(client, session, auth, _setup_ids):
    """Passing the whole quantity is the same as passing no quantity: a full revert,
    with no stray split."""
    cust = "contact:fullQtyMemo"
    sku = f"FQ-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"],
                          json={"sku": sku, "name": sku, "quantity": 5, "sell_by": "piece"})
    eid = r.json()["id"]
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 5, "unit_price": 10.0, "entity_id": eid},
    ], contact_id=cust)
    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid]})

    rr = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid], "quantities": {eid: 5}})
    assert rr.status_code == 200, rr.text
    assert rr.json()["reverted"] == [eid]
    assert rr.json()["partially_returned"] == []
    item = (await client.get(f"/items/{eid}", headers=auth["headers"])).json()
    assert item["status"] == "available" and item["quantity"] == 5.0
    after = (await client.get("/items", headers=auth["headers"], params={"on_memo_to": cust})).json()
    assert after["items"] == []


@pytest.mark.asyncio
async def test_partial_memo_return_rejects_more_than_went_out(client, session, auth, _setup_ids):
    cust = "contact:overReturn"
    sku = f"OR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"],
                          json={"sku": sku, "name": sku, "quantity": 2, "sell_by": "piece"})
    eid = r.json()["id"]
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 2, "unit_price": 10.0, "entity_id": eid},
    ], contact_id=cust)
    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid]})

    rr = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid], "quantities": {eid: 5}})
    assert rr.status_code == 422
    for bad in (0, -1):
        assert (await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                                  json={"line_entity_ids": [eid], "quantities": {eid: bad}})).status_code == 422
    # Nothing moved.
    item = (await client.get(f"/items/{eid}", headers=auth["headers"])).json()
    assert item["status"] == "memo_out" and item["quantity"] == 2.0


@pytest.mark.asyncio
async def test_revert_lines_without_quantities_is_unchanged(client, session, auth, _setup_ids):
    """The existing whole-line call shape keeps working untouched."""
    cust = "contact:noQty"
    sku = f"NQ-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"],
                          json={"sku": sku, "name": sku, "quantity": 3, "sell_by": "piece"})
    eid = r.json()["id"]
    doc_id = await _create_memo(client, auth, [
        {"sku": sku, "name": sku, "quantity": 3, "unit_price": 10.0, "entity_id": eid},
    ], contact_id=cust)
    await client.post(f"/docs/{doc_id}/fulfill-lines", headers=auth["headers"],
                      json={"line_entity_ids": [eid]})

    rr = await client.post(f"/docs/{doc_id}/revert-lines", headers=auth["headers"],
                           json={"line_entity_ids": [eid]})
    assert rr.status_code == 200, rr.text
    assert rr.json()["reverted"] == [eid]
    item = (await client.get(f"/items/{eid}", headers=auth["headers"])).json()
    assert item["status"] == "available" and item["quantity"] == 3.0
