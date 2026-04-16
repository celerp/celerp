# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/tools.py — 100% line coverage.

Covers:
  - All 5 tools with populated data
  - Tools with empty data (no items / no docs / no deals)
  - execute_tool dispatch
  - execute_tool with unknown name raises KeyError
  - limit parameter passed through
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from datetime import datetime, timezone

from celerp.models.base import Base
from celerp.models.projections import Projection
from celerp.ai.tools import TOOLS, execute_tool

_DB_URL = "sqlite+aiosqlite:///:memory:"
_NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _proj(company_id: uuid.UUID, entity_type: str, state: dict) -> Projection:
    return Projection(
        entity_id=str(uuid.uuid4()),
        company_id=company_id,
        entity_type=entity_type,
        state=state,
        version=1,
        updated_at=_NOW,
    )


@pytest_asyncio.fixture
async def populated_session(session) -> tuple[AsyncSession, uuid.UUID]:
    """Session with a mix of items, docs, deals for a single company."""
    cid = uuid.uuid4()

    # Items: one with stock, two with zero/negative
    session.add(_proj(cid, "item", {"sku": "A1", "name": "Widget", "quantity": 10, "total_cost": 500.0, "retail_price": 700.0}))
    session.add(_proj(cid, "item", {"sku": "B2", "name": "Low Item", "quantity": 0, "total_cost": 0.0}))
    session.add(_proj(cid, "item", {"sku": "C3", "name": "Neg Item", "quantity": -1, "total_cost": 0.0}))

    # Docs: invoice (outstanding), invoice (paid), PO
    session.add(_proj(cid, "doc", {"doc_type": "invoice", "doc_number": "INV-001", "contact_name": "Acme", "amount_outstanding": 1500.0, "status": "partial", "due_date": "2026-01-01", "total": 1500.0}))
    session.add(_proj(cid, "doc", {"doc_type": "invoice", "doc_number": "INV-002", "amount_outstanding": 0.0, "status": "paid", "total": 500.0}))
    session.add(_proj(cid, "doc", {"doc_type": "purchase_order", "doc_number": "PO-001", "amount_outstanding": 200.0, "status": "open", "total": 200.0}))

    # Deals: open, won, lost
    session.add(_proj(cid, "deal", {"status": "negotiating", "stage": "negotiating", "value": 3000.0}))
    session.add(_proj(cid, "deal", {"status": "won", "value": 5000.0}))
    session.add(_proj(cid, "deal", {"status": "lost", "value": 1000.0}))

    await session.commit()
    return session, cid


# ── dashboard_kpis ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_kpis_populated(populated_session):
    sess, cid = populated_session
    result = await execute_tool("dashboard_kpis", {}, sess, cid)
    assert result["total_items"] == 3
    assert result["low_stock_items"] == 2  # qty 0 and -1
    assert result["ar_outstanding"] == 1500.0
    assert result["active_deals"] == 1  # only "negotiating"
    assert result["inventory_value"] == 500.0


@pytest.mark.asyncio
async def test_dashboard_kpis_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("dashboard_kpis", {}, session, cid)
    assert result == {"total_items": 0, "inventory_value": 0.0, "low_stock_items": 0, "ar_outstanding": 0.0, "active_deals": 0}


# ── low_stock_items ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_low_stock_items(populated_session):
    sess, cid = populated_session
    result = await execute_tool("low_stock_items", {}, sess, cid)
    assert result["total_count"] == 2
    skus = {i["sku"] for i in result["items"]}
    assert "B2" in skus
    assert "C3" in skus


@pytest.mark.asyncio
async def test_low_stock_items_limit(populated_session):
    sess, cid = populated_session
    result = await execute_tool("low_stock_items", {"limit": 1}, sess, cid)
    assert len(result["items"]) == 1
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_low_stock_items_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("low_stock_items", {}, session, cid)
    assert result["total_count"] == 0
    assert result["items"] == []


# ── outstanding_invoices ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outstanding_invoices(populated_session):
    sess, cid = populated_session
    result = await execute_tool("outstanding_invoices", {}, sess, cid)
    assert result["total_count"] == 1
    assert result["invoices"][0]["doc_number"] == "INV-001"
    assert result["invoices"][0]["amount_outstanding"] == 1500.0


@pytest.mark.asyncio
async def test_outstanding_invoices_limit(populated_session):
    sess, cid = populated_session
    result = await execute_tool("outstanding_invoices", {"limit": 0}, sess, cid)
    assert result["invoices"] == []


@pytest.mark.asyncio
async def test_outstanding_invoices_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("outstanding_invoices", {}, session, cid)
    assert result["total_count"] == 0


# ── top_items_by_value ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_top_items_by_value(populated_session):
    sess, cid = populated_session
    result = await execute_tool("top_items_by_value", {}, sess, cid)
    assert result["items"][0]["sku"] == "A1"  # highest total_cost
    assert len(result["items"]) == 3  # default limit 10, only 3 items


@pytest.mark.asyncio
async def test_top_items_by_value_limit(populated_session):
    sess, cid = populated_session
    result = await execute_tool("top_items_by_value", {"limit": 1}, sess, cid)
    assert len(result["items"]) == 1


@pytest.mark.asyncio
async def test_top_items_by_value_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("top_items_by_value", {}, session, cid)
    assert result["items"] == []


# ── active_deals_summary ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_deals_summary(populated_session):
    sess, cid = populated_session
    result = await execute_tool("active_deals_summary", {}, sess, cid)
    assert len(result["stages"]) == 1
    stage = result["stages"][0]
    assert stage["stage"] == "negotiating"
    assert stage["count"] == 1
    assert stage["value"] == 3000.0


@pytest.mark.asyncio
async def test_active_deals_summary_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("active_deals_summary", {}, session, cid)
    assert result["stages"] == []


@pytest.mark.asyncio
async def test_active_deals_summary_no_stage_field(session):
    """Deal with no 'stage' key falls back to status."""
    cid = uuid.uuid4()
    session.add(_proj(cid, "deal", {"status": "open", "value": 100.0}))
    await session.commit()
    result = await execute_tool("active_deals_summary", {}, session, cid)
    assert result["stages"][0]["stage"] == "open"

# ── execute_tool unknown name ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_tool_unknown_raises(session):
    with pytest.raises(KeyError):
        await execute_tool("nonexistent_tool", {}, session, uuid.uuid4())


# ── TOOLS registry completeness ───────────────────────────────────────────────

def test_tools_registry_has_all_names():
    expected = {"dashboard_kpis", "low_stock_items", "outstanding_invoices", "top_items_by_value", "active_deals_summary", "active_contacts_list", "active_items_list", "dormant_contacts", "top_sellers", "pending_pos"}
    assert set(TOOLS.keys()) == expected


# ── dormant_contacts (real implementation) ────────────────────────────────────

@pytest.mark.asyncio
async def test_dormant_contacts_all_dormant(session):
    """Contacts with no docs are all dormant."""
    cid = uuid.uuid4()
    session.add(_proj(cid, "contact", {"name": "Acme", "contact_type": "vendor"}))
    session.add(_proj(cid, "contact", {"name": "Beta", "contact_type": "customer"}))
    await session.commit()
    result = await execute_tool("dormant_contacts", {}, session, cid)
    assert result["total_count"] == 2
    names = {c["name"] for c in result["dormant_contacts"]}
    assert names == {"Acme", "Beta"}


@pytest.mark.asyncio
async def test_dormant_contacts_active_contact_excluded(session):
    """Contact with recent invoice doc is NOT dormant."""
    from datetime import datetime, timezone, timedelta

    cid = uuid.uuid4()
    contact_id = str(uuid.uuid4())
    session.add(Projection(
        entity_id=contact_id,
        company_id=cid,
        entity_type="contact",
        state={"name": "Active Co", "contact_type": "customer"},
        version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    # Invoice for this contact created recently (within 90 days)
    session.add(Projection(
        entity_id=str(uuid.uuid4()),
        company_id=cid,
        entity_type="doc",
        state={"doc_type": "invoice", "contact_id": contact_id},
        version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    # Old inactive contact
    session.add(_proj(cid, "contact", {"name": "Dormant Co", "contact_type": "vendor"}))
    await session.commit()
    result = await execute_tool("dormant_contacts", {}, session, cid)
    names = {c["name"] for c in result["dormant_contacts"]}
    assert "Dormant Co" in names
    assert "Active Co" not in names


@pytest.mark.asyncio
async def test_dormant_contacts_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("dormant_contacts", {}, session, cid)
    assert result["total_count"] == 0
    assert result["dormant_contacts"] == []


@pytest.mark.asyncio
async def test_dormant_contacts_limit(session):
    cid = uuid.uuid4()
    for i in range(5):
        session.add(_proj(cid, "contact", {"name": f"Vendor {i}", "contact_type": "vendor"}))
    await session.commit()
    result = await execute_tool("dormant_contacts", {"limit": 2}, session, cid)
    assert len(result["dormant_contacts"]) == 2
    assert result["total_count"] == 5


# ── top_sellers (real implementation) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_top_sellers_aggregates_invoice_lines(session):
    """Top sellers are ranked by qty sold across invoices."""
    cid = uuid.uuid4()
    # Invoice with 2 line items
    session.add(_proj(cid, "doc", {
        "doc_type": "invoice",
        "line_items": [
            {"item_id": "item-A", "sku": "SKU-A", "description": "Widget A", "quantity": 10, "line_total": 100.0},
            {"item_id": "item-B", "sku": "SKU-B", "description": "Widget B", "quantity": 5, "line_total": 50.0},
        ],
    }))
    # Another invoice for item-A
    session.add(_proj(cid, "doc", {
        "doc_type": "invoice",
        "line_items": [
            {"item_id": "item-A", "sku": "SKU-A", "description": "Widget A", "quantity": 20, "line_total": 200.0},
        ],
    }))
    await session.commit()
    result = await execute_tool("top_sellers", {}, session, cid)
    assert result["total_count"] == 2
    sellers = result["top_sellers"]
    # item-A should be first (30 qty sold total)
    assert sellers[0]["sku"] == "SKU-A"
    assert sellers[0]["qty_sold"] == 30.0
    assert sellers[1]["sku"] == "SKU-B"


@pytest.mark.asyncio
async def test_top_sellers_ignores_non_invoices(session):
    """Bills and POs don't count toward top sellers."""
    cid = uuid.uuid4()
    session.add(_proj(cid, "doc", {
        "doc_type": "bill",
        "line_items": [{"item_id": "item-X", "sku": "SKU-X", "quantity": 100, "line_total": 1000.0}],
    }))
    await session.commit()
    result = await execute_tool("top_sellers", {}, session, cid)
    assert result["total_count"] == 0


@pytest.mark.asyncio
async def test_top_sellers_empty(session):
    cid = uuid.uuid4()
    result = await execute_tool("top_sellers", {}, session, cid)
    assert result["total_count"] == 0
    assert result["top_sellers"] == []


@pytest.mark.asyncio
async def test_top_sellers_limit(session):
    cid = uuid.uuid4()
    items = [{"item_id": f"item-{i}", "sku": f"SKU-{i}", "description": f"Item {i}", "quantity": i + 1, "line_total": float(i + 1)} for i in range(5)]
    session.add(_proj(cid, "doc", {"doc_type": "invoice", "line_items": items}))
    await session.commit()
    result = await execute_tool("top_sellers", {"limit": 3}, session, cid)
    assert len(result["top_sellers"]) == 3
