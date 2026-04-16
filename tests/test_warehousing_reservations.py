# Copyright (c) 2026 Noah Severs. All Rights Reserved.
# SPDX-License-Identifier: Proprietary
"""Tests for the reservation system — reserve/release items for pick instructions."""

from __future__ import annotations

import pytest
pytest.importorskip("celerp_warehousing")

import uuid

import pytest
import pytest_asyncio

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.projections import Projection
from celerp.services.auth import create_access_token


@pytest.fixture
def _res_ids():
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def res_auth(session, _res_ids):
    cid = _res_ids["company_id"]
    uid = _res_ids["user_id"]
    session.add(Company(id=cid, name="ReserveCo", slug="reserveco", settings={"currency": "USD"}))
    session.add(User(id=uid, company_id=cid, email="admin@reserve.test", name="Admin", auth_hash="x", role="admin", is_active=True))
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token = create_access_token(subject=str(uid), company_id=str(cid), role="admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": str(uid)}


@pytest.mark.asyncio
async def test_reserve_items_emits_events(session, res_auth):
    """reserve_items_for_pick emits item.reserved events and updates item state."""
    from celerp.events.engine import emit_event
    from celerp.services.pick import PickLine, PickResult
    from celerp_warehousing.reservations import reserve_items_for_pick

    cid = res_auth["company_id"]
    uid = uuid.UUID(res_auth["user_id"])

    # Create an inventory item
    item_id = f"item:{uuid.uuid4()}"
    await emit_event(
        session, company_id=cid, entity_id=item_id, entity_type="item",
        event_type="item.created",
        data={"sku": "RES-A", "name": "RES-A", "quantity": 10, "cost_price": 5.0},
        actor_id=uid, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )

    pick_result = PickResult(
        picks=[PickLine(item_id=item_id, sku="RES-A", pick_qty=5, cost_price=5.0, action="split")],
        unfulfilled=[],
        strategy="fifo",
    )

    reservations = await reserve_items_for_pick(
        session,
        pick_result=pick_result,
        source_doc_id="doc:fake-inv-001",
        company_id=cid,
        user_id=uid,
    )
    await session.commit()

    assert len(reservations) == 1
    assert reservations[0]["sku"] == "RES-A"
    assert reservations[0]["quantity"] == 5

    # Check item state has reserved_quantity
    item_row = await session.get(Projection, {"company_id": cid, "entity_id": item_id})
    assert item_row is not None
    assert float(item_row.state.get("reserved_quantity", 0)) == 5.0


@pytest.mark.asyncio
async def test_release_reservations(session, res_auth):
    """release_reservations emits item.unreserved events."""
    from celerp.events.engine import emit_event
    from celerp.services.pick import PickLine, PickResult
    from celerp_warehousing.reservations import release_reservations, reserve_items_for_pick

    cid = res_auth["company_id"]
    uid = uuid.UUID(res_auth["user_id"])

    item_id = f"item:{uuid.uuid4()}"
    await emit_event(
        session, company_id=cid, entity_id=item_id, entity_type="item",
        event_type="item.created",
        data={"sku": "REL-A", "name": "REL-A", "quantity": 10, "cost_price": 3.0},
        actor_id=uid, location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )

    pick_result = PickResult(
        picks=[PickLine(item_id=item_id, sku="REL-A", pick_qty=4, cost_price=3.0, action="split")],
        unfulfilled=[], strategy="fifo",
    )
    await reserve_items_for_pick(
        session, pick_result=pick_result,
        source_doc_id="doc:release-test", company_id=cid, user_id=uid,
    )
    await session.commit()

    # Now release
    await release_reservations(
        session,
        reservations=[{"item_id": item_id, "quantity": 4}],
        source_doc_id="doc:release-test",
        company_id=cid,
        user_id=uid,
        reason="test_release",
    )
    await session.commit()

    # reserved_quantity should be 0 after release
    item_row = await session.get(Projection, {"company_id": cid, "entity_id": item_id})
    assert float(item_row.state.get("reserved_quantity", 0)) == 0.0


@pytest.mark.asyncio
async def test_reservation_reflects_in_item_state(client, session, res_auth):
    """Reserve/unreserve cycle correctly updates item state via API."""
    # Create item via API
    r = await client.post("/items", headers=res_auth["headers"], json={
        "sku": "API-RES-A", "name": "API-RES-A", "quantity": 20, "cost_price": 2.0, "sell_by": "piece",
    })
    assert r.status_code == 200
    item_id = r.json()["id"]

    # Reserve via inventory API
    r2 = await client.post(f"/items/{item_id}/reserve", headers=res_auth["headers"], json={"quantity": 8})
    assert r2.status_code == 200

    # Check state
    r3 = await client.get(f"/items/{item_id}", headers=res_auth["headers"])
    item = r3.json()
    assert float(item.get("reserved_quantity") or 0) == 8.0

    # Unreserve
    r4 = await client.post(f"/items/{item_id}/unreserve", headers=res_auth["headers"], json={"quantity": 8})
    assert r4.status_code == 200

    r5 = await client.get(f"/items/{item_id}", headers=res_auth["headers"])
    assert float(r5.json().get("reserved_quantity") or 0) == 0.0


@pytest.mark.asyncio
async def test_warehousing_settings_api(client, session, res_auth):
    """POST/GET /warehousing/settings persists picking strategy."""
    r = await client.post("/warehousing/settings", headers=res_auth["headers"], json={
        "pick_strategy": "fefo",
        "auto_create_pick_instructions": False,
        "require_pick_before_fulfill": True,
    })
    assert r.status_code == 200

    r2 = await client.get("/warehousing/settings", headers=res_auth["headers"])
    assert r2.status_code == 200
    data = r2.json()
    assert data["pick_strategy"] == "fefo"
    assert data["auto_create_pick_instructions"] is False
    assert data["require_pick_before_fulfill"] is True


@pytest.mark.asyncio
async def test_warehousing_settings_default(client, session, res_auth):
    """GET /warehousing/settings returns empty dict when not configured."""
    r = await client.get("/warehousing/settings", headers=res_auth["headers"])
    assert r.status_code == 200
    # Empty or default settings dict
    data = r.json()
    assert isinstance(data, dict)
