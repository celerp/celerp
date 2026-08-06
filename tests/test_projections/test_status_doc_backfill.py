# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One-time status-to-document backfill for pre-existing inventory items."""
from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.status_doc_backfill import run_status_doc_backfill


async def _seed_company(session) -> uuid.UUID:
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="BackfillCo", slug=f"bf-{company_id.hex[:8]}"))
    await session.flush()
    return company_id


async def _emit(session, company_id, entity_id, event_type, data):
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type=event_type,
        data=data,
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )


async def _strip_status_doc(session, company_id, entity_id):
    """Simulate a projection built before the status_doc pairing field existed."""
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    state = dict(proj.state)
    state.pop("status_doc_id", None)
    state.pop("status_doc_number", None)
    proj.state = state
    await session.commit()


@pytest.mark.asyncio
async def test_backfill_stamps_pre_existing_sold_item(session):
    """A sold item whose projection predates the pairing gets its document link
    back, derived by replaying its own events through the live handler."""
    company_id = await _seed_company(session)
    eid = "item:sold1"
    await _emit(session, company_id, eid, "item.created", {"sku": "S1", "name": "Ring", "quantity": 1})
    await _emit(
        session, company_id, eid, "item.fulfilled",
        {"source_doc_id": "doc:inv1", "quantity_fulfilled": 1, "fulfilled_by": "u",
         "doc_number": "INV-0001", "doc_type": "invoice"},
    )
    await session.commit()

    # Sanity: the live handler stamps the pairing for a fresh fulfilment.
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
    assert proj.state["status_doc_id"] == "doc:inv1"
    assert proj.state["status"] == "sold"

    # Old-data condition: the pairing field was never written for this item.
    await _strip_status_doc(session, company_id, eid)

    result = await run_status_doc_backfill(session)
    await session.commit()

    proj2 = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
    assert proj2.state.get("status_doc_id") == "doc:inv1", "backfill did not restore the document link"
    assert proj2.state.get("status_doc_number") == "INV-0001"
    assert result["stamped"] == 1


@pytest.mark.asyncio
async def test_backfill_does_not_fabricate_for_undocumented_item(session):
    """An item with no source document behind its status is left untouched: the
    backfill degrades honestly and never invents a link."""
    company_id = await _seed_company(session)
    eid = "item:avail1"
    await _emit(session, company_id, eid, "item.created", {"sku": "A1", "name": "Loose stone", "quantity": 1})
    await session.commit()

    result = await run_status_doc_backfill(session)
    await session.commit()

    proj = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
    assert not proj.state.get("status_doc_id")
    assert result["stamped"] == 0


@pytest.mark.asyncio
async def test_backfill_marker_gates_second_run(session):
    """The backfill runs once per database: a second run is a no-op even when a
    fresh gap appears, so it never rescans the whole catalog on every boot."""
    company_id = await _seed_company(session)
    eid = "item:sold2"
    await _emit(session, company_id, eid, "item.created", {"sku": "S2", "name": "Pendant", "quantity": 1})
    await _emit(
        session, company_id, eid, "item.fulfilled",
        {"source_doc_id": "doc:memo1", "quantity_fulfilled": 1, "fulfilled_by": "u",
         "doc_number": "MEMO-0007", "doc_type": "memo"},
    )
    await session.commit()

    first = await run_status_doc_backfill(session)
    await session.commit()
    assert first["changed"] is True

    # A new gap after the marker is set must not be touched by a later run.
    await _strip_status_doc(session, company_id, eid)
    second = await run_status_doc_backfill(session)
    await session.commit()

    assert second == {"changed": False, "stamped": 0}
    proj = await session.get(Projection, {"company_id": company_id, "entity_id": eid})
    assert not proj.state.get("status_doc_id"), "gated run must not refill after the marker is set"
