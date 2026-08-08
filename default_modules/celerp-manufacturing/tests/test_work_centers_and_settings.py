# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Work Centers (relational CRUD, like locations) and hours-per-day sourcing.

Hours per day lives on each work center (column work_centers.hours_per_day) and the
To-Make est-hours column reads the company's default work center. Each company has
exactly one default center (partial unique index uq_work_center_one_default), seeded
on company creation and backfilled for existing companies.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from celerp.models.company import Company, WorkCenter


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@wc.test"
    r = await client.post("/auth/register", json={"company_name": "WC Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _company_id(session):
    return (await session.execute(select(Company.id).where(Company.name == "WC Co"))).scalars().first()


@pytest.fixture()
def mfg_provision_slots():
    """Register the manufacturing lifecycle hooks for the duration of a test.

    Under the httpx ASGI harness the module loader (which registers these slots
    from PLUGIN_MANIFEST in production) never runs, so the hooks are registered
    here and the exact prior slot lists restored on teardown so nothing leaks
    into other tests.
    """
    from celerp.modules import slots
    before_cc = slots.get("on_company_created")
    before_mr = slots.get("on_modules_ready")
    slots.register("on_company_created", {
        "handler": "celerp_manufacturing.routes:provision_default_work_center_hook",
        "_module": "celerp-manufacturing"})
    slots.register("on_modules_ready", {
        "handler": "celerp_manufacturing.routes:backfill_default_work_center_hook",
        "_module": "celerp-manufacturing"})
    yield
    slots._slots["on_company_created"] = before_cc
    slots._slots["on_modules_ready"] = before_mr


# --- Work Centers CRUD ------------------------------------------------------

@pytest.mark.asyncio
async def test_work_center_crud(client):
    token = await _register(client)
    # No default is seeded in the unit harness (the provisioning slot is not
    # registered here), so the list starts empty.
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"] == []

    # Create - the first center for a company becomes its default.
    wc = (await client.post("/manufacturing/work-centers", headers=_h(token),
                            json={"name": "Polishing Bench", "labor_rate": 45.0})).json()
    wc_id = wc["id"]
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    assert len(items) == 1 and items[0]["name"] == "Polishing Bench" and items[0]["labor_rate"] == 45.0
    assert items[0]["is_default"] is True

    # Duplicate name rejected.
    assert (await client.post("/manufacturing/work-centers", headers=_h(token),
                              json={"name": "Polishing Bench"})).status_code == 409
    # Blank name rejected.
    assert (await client.post("/manufacturing/work-centers", headers=_h(token),
                              json={"name": "  "})).status_code == 422

    # Patch.
    assert (await client.patch(f"/manufacturing/work-centers/{wc_id}", headers=_h(token),
                               json={"name": "Bench 1", "labor_rate": 50.0})).status_code == 200
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    by_name = {w["name"]: w for w in items}
    assert by_name["Bench 1"]["labor_rate"] == 50.0

    # A second, non-default center.
    wc2 = (await client.post("/manufacturing/work-centers", headers=_h(token),
                             json={"name": "Bench 2"})).json()
    wc2_id = wc2["id"]

    # Deleting the default while others exist is refused.
    assert (await client.delete(f"/manufacturing/work-centers/{wc_id}", headers=_h(token))).status_code == 409
    # Deleting a non-default center succeeds.
    assert (await client.delete(f"/manufacturing/work-centers/{wc2_id}", headers=_h(token))).status_code == 200
    # Deleting the last remaining center is refused.
    assert (await client.delete(f"/manufacturing/work-centers/{wc_id}", headers=_h(token))).status_code == 409

    # Patch / delete on a missing one -> 404.
    missing = str(uuid.uuid4())
    assert (await client.patch(f"/manufacturing/work-centers/{missing}", headers=_h(token),
                               json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/manufacturing/work-centers/{missing}", headers=_h(token))).status_code == 404


@pytest.mark.asyncio
async def test_wc_first_is_default(client):
    token = await _register(client)
    (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "A"}))
    (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "B"}))
    items = {w["name"]: w for w in (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]}
    assert items["A"]["is_default"] is True
    assert items["B"]["is_default"] is False
    assert sum(1 for w in items.values() if w["is_default"]) == 1


@pytest.mark.asyncio
async def test_wc_make_default_unsets_prior(client):
    token = await _register(client)
    (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "A"}))
    b = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "B"})).json()
    # Patching B default unsets A in the same transaction.
    assert (await client.patch(f"/manufacturing/work-centers/{b['id']}", headers=_h(token),
                               json={"is_default": True})).status_code == 200
    items = {w["name"]: w for w in (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]}
    assert items["B"]["is_default"] is True
    assert items["A"]["is_default"] is False
    assert sum(1 for w in items.values() if w["is_default"]) == 1


@pytest.mark.asyncio
async def test_wc_delete_default_guard(client):
    token = await _register(client)
    a = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "A"})).json()
    b = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "B"})).json()
    # A is the default (first center); deleting it while B exists is refused.
    assert (await client.delete(f"/manufacturing/work-centers/{a['id']}", headers=_h(token))).status_code == 409
    # B is not default; it deletes.
    assert (await client.delete(f"/manufacturing/work-centers/{b['id']}", headers=_h(token))).status_code == 200
    # A is now the last center; deleting it is refused.
    assert (await client.delete(f"/manufacturing/work-centers/{a['id']}", headers=_h(token))).status_code == 409


@pytest.mark.asyncio
async def test_wc_hours_edit_positive_persists(client):
    token = await _register(client)
    a = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "A"})).json()
    assert (await client.patch(f"/manufacturing/work-centers/{a['id']}", headers=_h(token),
                               json={"hours_per_day": 6})).status_code == 200
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    assert items[0]["hours_per_day"] == 6.0


@pytest.mark.asyncio
async def test_wc_negative_hours_floored(client):
    token = await _register(client)
    a = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "A"})).json()
    assert (await client.patch(f"/manufacturing/work-centers/{a['id']}", headers=_h(token),
                               json={"hours_per_day": -5})).status_code == 200
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    assert items[0]["hours_per_day"] is None
    # A -5 stored as None means the board coalesces to the 8.0 default, never a negative.
    ring = await _ring_with_daily_labor(client, token)
    assert await _est_hours(client, token, ring) == 8.0


@pytest.mark.asyncio
async def test_wc_make_default_race_correct_message(client, monkeypatch):
    token = await _register(client)
    (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "A"}))
    b = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "B"})).json()
    # Simulate a race: the prior-default unset does not run (as if another request
    # committed its default between our read and write). The partial unique index
    # then rejects the second default; the handler must disambiguate the message.
    import celerp_manufacturing.routes as mfg_routes

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(mfg_routes, "_unset_other_defaults", _noop)
    r = await client.patch(f"/manufacturing/work-centers/{b['id']}/is_default", headers=_h(token))
    assert r.status_code == 409
    assert r.json()["detail"] == "Another work center is already the default"


@pytest.mark.asyncio
async def test_wc_set_default_requires_manage_manufacturing(client, session):
    from test_helpers import invite_user
    owner = await _register(client)
    a = (await client.post("/manufacturing/work-centers", headers=_h(owner), json={"name": "A"})).json()
    b = (await client.post("/manufacturing/work-centers", headers=_h(owner), json={"name": "B"})).json()
    viewer_token = await invite_user(client, session, _h(owner),
                                     f"viewer-{uuid.uuid4().hex[:8]}@wc.test", "viewer")
    r = await client.patch(f"/manufacturing/work-centers/{b['id']}/is_default", headers=_h(viewer_token))
    assert r.status_code == 403
    # The default is unchanged: A is still the default.
    items = {w["name"]: w for w in (await client.get("/manufacturing/work-centers", headers=_h(owner))).json()["items"]}
    assert items["A"]["is_default"] is True and items["B"]["is_default"] is False


@pytest.mark.asyncio
async def test_new_company_seeds_default_wc(client, mfg_provision_slots):
    token = await _register(client)
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    defaults = [w for w in items if w["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "Default"
    assert defaults[0]["hours_per_day"] == 8.0


@pytest.mark.asyncio
async def test_backfill_default_wc_for_existing_mfg_company(client, session, mfg_provision_slots):
    # A company created without the provisioning hook firing (simulate an existing
    # company from before this deploy) has no default center...
    from celerp.modules import slots
    # Temporarily drop the on_company_created hook so registration seeds nothing.
    saved_cc = slots.get("on_company_created")
    slots._slots["on_company_created"] = [c for c in saved_cc if c.get("_module") != "celerp-manufacturing"]
    try:
        token = await _register(client)
    finally:
        slots._slots["on_company_created"] = saved_cc
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"] == []

    # ...until on_modules_ready backfills exactly one default center.
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle("on_modules_ready", session=session)
    await session.commit()
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    defaults = [w for w in items if w["is_default"]]
    assert len(defaults) == 1 and defaults[0]["name"] == "Default" and defaults[0]["hours_per_day"] == 8.0

    # Re-firing is a no-op (idempotent).
    await fire_lifecycle("on_modules_ready", session=session)
    await session.commit()
    items = (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"]
    assert len([w for w in items if w["is_default"]]) == 1


@pytest.mark.asyncio
async def test_provision_hook_failure_does_not_abort_registration(client, mfg_provision_slots):
    import celerp_manufacturing.routes as mfg_routes
    from unittest.mock import patch

    async def _boom(*a, **k):
        raise RuntimeError("seed failure")
    # fire_lifecycle logs-and-swallows a failing hook, so registration completes
    # and no half-created default work center is left behind.
    with patch.object(mfg_routes, "provision_default_work_center_hook", _boom):
        token = await _register(client)
    assert token
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"] == []


# --- Hours per day sourced from the default work center ---------------------

async def _ring_with_daily_labor(client, token):
    gold = (await client.post("/items", headers=_h(token),
                              json={"sku": f"G-{uuid.uuid4().hex[:6]}", "name": "Gold", "quantity": 100,
                                    "sell_by": "gram", "cost_total": 8000})).json()["id"]
    ring = (await client.post("/items", headers=_h(token),
                              json={"sku": f"R-{uuid.uuid4().hex[:6]}", "name": "Ring", "quantity": 0,
                                    "sell_by": "piece"})).json()["id"]
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 1}],
                           "labor": [{"operation": "Cast", "kind": "daily", "hours": 1, "rate": 100}], "overhead": []})
    # Finalize the invoice so it counts as demand. A draft invoice is a pro-forma (tentative)
    # and is intentionally excluded from the To-Make board.
    inv = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "line_items": [
        {"item_id": ring, "sku": "R", "name": "Ring", "quantity": 1, "unit_price": 1}], "total": 1})).json()
    assert (await client.post(f"/docs/{inv['id']}/finalize", headers=_h(token))).status_code == 200
    return ring


async def _est_hours(client, token, ring) -> float:
    board = {r["item_id"]: r for r in (await client.get("/manufacturing/to-make", headers=_h(token))).json()["items"]}
    return board[ring]["est_hours"]


@pytest.mark.asyncio
async def test_hours_per_day_setting_drives_est_hours(client):
    """The default work center's hours_per_day drives the To-Make est-hours column
    (the old company-level setting was moved onto the center)."""
    token = await _register(client)
    ring = await _ring_with_daily_labor(client, token)
    a = (await client.post("/manufacturing/work-centers", headers=_h(token), json={"name": "Default"})).json()
    # No hours set on the default center -> board falls back to 8: 1 day x 8 = 8.
    assert await _est_hours(client, token, ring) == 8.0
    # Raise the default center's hours to 10 -> 1 day x 10 = 10.
    assert (await client.patch(f"/manufacturing/work-centers/{a['id']}", headers=_h(token),
                               json={"hours_per_day": 10})).status_code == 200
    assert await _est_hours(client, token, ring) == 10.0


@pytest.mark.asyncio
async def test_tomake_est_hours_parity(client, session):
    """Est-hours sourced from the default WC (hours=10) equals the pre-change
    company-setting result (hours=10 gave 10 est-hours for a 1-day labour line)."""
    token = await _register(client)
    ring = await _ring_with_daily_labor(client, token)
    a = (await client.post("/manufacturing/work-centers", headers=_h(token),
                           json={"name": "Default", "hours_per_day": 10})).json()
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"][0]["is_default"] is True
    assert await _est_hours(client, token, ring) == 10.0
    # The board helper reads the default center's hours directly.
    from celerp_manufacturing.routes import _default_hours_per_day
    cid = await _company_id(session)
    assert await _default_hours_per_day(session, cid) == 10.0
    assert a["id"]


@pytest.mark.asyncio
async def test_tomake_est_hours_no_default_falls_back_to_8(client, session):
    """With no default work center, the board helper returns DEFAULT_HOURS_PER_DAY
    (8.0) and est-hours compute with 8.0 without raising."""
    token = await _register(client)
    ring = await _ring_with_daily_labor(client, token)
    # No work center created -> no default row.
    assert (await client.get("/manufacturing/work-centers", headers=_h(token))).json()["items"] == []
    assert await _est_hours(client, token, ring) == 8.0
    from celerp_manufacturing.routes import _default_hours_per_day
    cid = await _company_id(session)
    assert await _default_hours_per_day(session, cid) == 8.0


# --- Settings: require_issued_before_complete -------------------------------

@pytest.mark.asyncio
async def test_require_issued_before_complete_setting(client):
    token = await _register(client)
    gold = (await client.post("/items", headers=_h(token),
                              json={"sku": "GREQ", "name": "Gold", "quantity": 100, "sell_by": "gram",
                                    "cost_total": 8000})).json()["id"]
    ring = (await client.post("/items", headers=_h(token),
                              json={"sku": "RREQ", "name": "Ring", "quantity": 0, "sell_by": "piece"})).json()["id"]
    await client.put(f"/manufacturing/items/{ring}/recipe", headers=_h(token),
                     json={"output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})

    # With the guard on, completing a run whose components are not issued is rejected.
    assert (await client.patch("/companies/me", headers=_h(token),
                               json={"settings": {"manufacturing": {"require_issued_before_complete": True}}})).status_code == 200
    run = (await client.post(f"/manufacturing/items/{ring}/build", headers=_h(token), json={"quantity": 1})).json()["id"]
    assert (await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={})).status_code == 409
    # Issue first, then completion succeeds.
    assert (await client.post(f"/manufacturing/{run}/issue", headers=_h(token))).status_code == 200
    assert (await client.post(f"/manufacturing/{run}/complete", headers=_h(token), json={})).status_code == 200


# --- Cruft: the company-level hours_per_day setting is gone -----------------

def test_hours_per_day_setting_removed() -> None:
    """The company-wide settings.manufacturing.hours_per_day is neither read nor
    written anywhere in the live code; only the WorkCenter column and the
    labor_hours() param survive."""
    root = Path(__file__).resolve().parents[3]
    targets = [
        root / "ui/routes/settings_manufacturing.py",
        root / "default_modules/celerp-manufacturing/celerp_manufacturing/routes.py",
    ]
    offenders: list[str] = []
    for f in targets:
        text = f.read_text(encoding="utf-8")
        if "hours_per_day" in text and f.name == "settings_manufacturing.py":
            offenders.append(f"{f.name}: references hours_per_day")
        # The module routes keep the WorkCenter column/DTO usage but must not read
        # or write the old company setting.
        if "_hours_per_day" in text and "_default_hours_per_day" not in text:
            offenders.append(f"{f.name}: still defines/uses _hours_per_day helper")
        for pat in ('settings.get("hours_per_day")', '"hours_per_day": hours'):
            if pat in text:
                offenders.append(f"{f.name}: still reads/writes the old hours_per_day setting ({pat})")
    assert offenders == [], offenders
