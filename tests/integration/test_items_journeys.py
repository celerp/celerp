# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _u() -> str:
    return str(uuid.uuid4())


async def _create_item(journey_api, *, quantity: int = 10, location_id: str | None = None) -> str:
    payload: dict = {
        "sku": f"IT-SKU-{_u()[:8]}",
        "name": "Integration Item",
        "quantity": quantity,
        "cost_price": 10.0,
        "sale_price": 20.0,
        "idempotency_key": _u(),
    }
    if location_id is not None:
        payload["location_id"] = location_id

    r = await journey_api.post("/items", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _get_item(journey_api, item_id: str) -> dict:
    r = await journey_api.get(f"/items/{item_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def _ensure_two_locations(journey_api) -> tuple[str, str]:
    locs = await journey_api.get("/companies/me/locations")
    assert locs.status_code == 200, locs.text
    rows = locs.json()["items"]
    if len(rows) >= 2:
        return rows[0]["id"], rows[1]["id"]

    mk1 = await journey_api.post(
        "/companies/me/locations",
        json={"name": f"ITest Loc A {_u()[:8]}", "type": "warehouse", "idempotency_key": _u()},
    )
    assert mk1.status_code == 200, mk1.text

    mk2 = await journey_api.post(
        "/companies/me/locations",
        json={"name": f"ITest Loc B {_u()[:8]}", "type": "warehouse", "idempotency_key": _u()},
    )
    assert mk2.status_code == 200, mk2.text

    locs2 = await journey_api.get("/companies/me/locations")
    assert locs2.status_code == 200, locs2.text
    rows2 = locs2.json()["items"]
    assert len(rows2) >= 2
    return rows2[0]["id"], rows2[1]["id"]


async def test_inventory_reserve_unreserve_transfer_and_status_changes(journey_api):
    from_loc, to_loc = await _ensure_two_locations(journey_api)
    item_id = await _create_item(journey_api, quantity=10, location_id=from_loc)

    reserve = await journey_api.post(
        f"/items/{item_id}/reserve",
        json={"quantity": 3, "idempotency_key": _u()},
    )
    assert reserve.status_code == 200, reserve.text

    item = await _get_item(journey_api, item_id)
    assert item.get("reserved_quantity", 0) >= 3

    unreserve = await journey_api.post(
        f"/items/{item_id}/unreserve",
        json={"quantity": 2, "idempotency_key": _u()},
    )
    assert unreserve.status_code == 200, unreserve.text

    item2 = await _get_item(journey_api, item_id)
    assert item2.get("reserved_quantity", 0) >= 1

    transfer = await journey_api.post(
        f"/items/{item_id}/transfer",
        json={"to_location_id": to_loc, "idempotency_key": _u()},
    )
    assert transfer.status_code == 200, transfer.text

    expire = await journey_api.post(f"/items/{item_id}/expire")
    assert expire.status_code == 200, expire.text
    expired = await _get_item(journey_api, item_id)
    assert expired.get("status") in {"expired", "disposed"} or expired.get("is_expired") is True

    dispose = await journey_api.post(f"/items/{item_id}/dispose")
    assert dispose.status_code == 200, dispose.text
    disposed = await _get_item(journey_api, item_id)
    assert disposed.get("is_available") is False
