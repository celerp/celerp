# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An item never takes a value for a category attribute its own category does not define.

The rule lives at the item write boundary (events.engine._item_applied), so it holds
for every writer: the tests call that boundary directly, as any writer could, and then
go through each writer path (edit, create, import, split). The resulting category is
the one judged; clearing is always allowed, and an item already holding such a value
keeps taking every other edit."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from celerp.events.engine import emit_event
from test_cost_restatement import _state

pytestmark = pytest.mark.asyncio

GEMS, BOXES = "cat_gems", "cat_boxes"


async def _categories(client, h) -> None:
    """GEMS defines the attribute ``origin``; BOXES defines only ``material``."""
    for category, key in ((GEMS, "origin"), (BOXES, "material")):
        r = await client.patch(f"/companies/me/category-schema/{category}", headers=h,
                               json={"fields": [{"key": key, "label": key.title(), "type": "text"}]})
        assert r.status_code == 200, r.text


async def _item(client, h, category: str, **attributes) -> str:
    r = await client.post("/items", headers=h, json={
        "sku": f"CA-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 4, "sell_by": "piece",
        "status": "available", "allow_splitting": True, "category": category,
        **({"attributes": attributes} if attributes else {})})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _patch(client, h, item_id: str, **fields):
    return await client.patch(f"/items/{item_id}", headers=h, json={
        "fields_changed": {k: {"old": None, "new": v} for k, v in fields.items()}})


def _refused(r) -> None:
    assert r.status_code == 422, r.text
    body = r.json()
    detail = body.get("detail", body)
    assert detail["message_key"] == "item.attribute_not_in_category", body
    assert "origin" in detail["message"] and BOXES in detail["message"]
    assert "Settings > Inventory > Categories" in detail["message"]


async def _legacy(client, h) -> str:
    """A BOXES item holding an ``origin`` from the time it was a GEMS item."""
    item_id = await _item(client, h, GEMS, origin="Burma")
    r = await _patch(client, h, item_id, category=BOXES)
    assert r.status_code == 200, r.text
    return item_id


async def test_the_boundary_refuses_any_writer(client, session, auth):
    h = auth["headers"]
    await _categories(client, h)
    item_id = await _item(client, h, BOXES)
    with pytest.raises(HTTPException) as refused:
        await emit_event(
            session, company_id=auth["company_id"], entity_id=item_id, entity_type="item",
            event_type="item.updated", data={"fields_changed": {"origin": {"old": None, "new": "Burma"}}},
            actor_id=None, location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
    assert refused.value.status_code == 422
    assert refused.value.detail["message_key"] == "item.attribute_not_in_category"


async def test_an_edit_is_judged_against_the_resulting_category(client, session, auth):
    h = auth["headers"]
    await _categories(client, h)
    item_id = await _item(client, h, BOXES)
    _refused(await _patch(client, h, item_id, origin="Burma"))
    assert "origin" not in ((await _state(session, auth, item_id)).get("attributes") or {})
    assert (await _patch(client, h, item_id, material="Oak")).status_code == 200
    r = await _patch(client, h, item_id, category=GEMS, origin="Burma")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, item_id))["attributes"]["origin"] == "Burma"
    _refused(await _patch(client, h, item_id, category=BOXES, origin="Ceylon"))


async def test_a_create_in_the_wrong_category_is_refused(client, auth):
    h = auth["headers"]
    await _categories(client, h)
    r = await client.post("/items", headers=h, json={
        "sku": "CA-NEW", "name": "Lot", "quantity": 1, "sell_by": "piece", "status": "available",
        "category": BOXES, "attributes": {"origin": "Burma"}})
    _refused(r)


async def test_an_item_holding_such_a_value_keeps_every_other_edit_and_can_clear_it(client, session, auth):
    h = auth["headers"]
    await _categories(client, h)
    item_id = await _legacy(client, h)
    assert (await _patch(client, h, item_id, name="Renamed")).status_code == 200
    _refused(await _patch(client, h, item_id, origin="Ceylon"))
    assert (await _patch(client, h, item_id, origin="")).status_code == 200
    assert not ((await _state(session, auth, item_id)).get("attributes") or {}).get("origin")


async def test_a_split_child_may_keep_its_lots_value_but_not_add_one(client, auth):
    h = auth["headers"]
    await _categories(client, h)
    item_id = await _legacy(client, h)
    r = await client.post(f"/items/{item_id}/split", headers=h, json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text
    fresh = await _item(client, h, BOXES)
    r = await client.post(f"/items/{fresh}/split", headers=h,
                          json={"children": [{"quantity": 1, "attributes": {"origin": "Burma"}}]})
    _refused(r)


async def _location(client, h) -> None:
    r = await client.post("/companies/me/locations", headers=h,
                          json={"name": "Head office", "type": "office", "is_default": True})
    assert r.status_code == 200, r.text


async def _import(client, h, key: str, rows: list[dict]):
    preview = await client.post("/items/import/rows/preview", headers=h, json={
        "rows": rows, "upsert": False, "idempotency_key": key, "decisions": {}})
    assert preview.status_code == 200, preview.text
    return await client.post("/items/import/rows", headers=h, json={
        "rows": rows, "upsert": False, "idempotency_key": key, "decisions": {},
        "preview_hash": preview.json()["preview_hash"]})


async def test_an_import_by_someone_who_cannot_change_categories_is_refused(client, session, auth):
    from test_helpers import invite_user

    h = auth["headers"]
    await _categories(client, h)
    await _location(client, h)
    token = await invite_user(client, session, h, f"mgr-{uuid.uuid4().hex[:6]}@test.test", "manager")
    rows = [{"sku": "CA-IMP", "name": "Lot", "quantity": "1", "sell_by": "piece", "category": BOXES, "origin": "Burma",
             "location_name": "Head office"}]
    r = await _import(client, {"Authorization": f"Bearer {token}"}, "ca-mgr", rows)
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    [error] = r.json()["errors"]
    assert error["params"]["refusal"]["message_key"] == "item.attribute_not_in_category", r.text
    items = (await client.get("/items", headers=h, params={"q": "CA-IMP"})).json()
    assert not [i for i in items.get("items", []) if i.get("sku") == "CA-IMP"], items


async def test_an_admin_import_adds_the_attribute_to_the_category_first(client, session, auth):
    """An import by someone who may change categories grows the category's schema with
    the columns it brings (as it always has), before its items are written."""
    from celerp.models.company import Company

    h = auth["headers"]
    await _categories(client, h)
    await _location(client, h)
    rows = [{"sku": "CA-ADM", "name": "Lot", "quantity": "1", "sell_by": "piece", "category": BOXES, "origin": "Burma",
             "location_name": "Head office"}]
    r = await _import(client, h, "ca-adm", rows)
    assert r.status_code == 200 and not r.json().get("errors"), r.text
    session.expire_all()
    settings = (await session.get(Company, auth["company_id"])).settings
    assert "origin" in {f["key"] for f in settings["category_schemas"][BOXES]}


async def test_every_vertical_demo_item_holds_only_its_own_categorys_attributes():
    """A starter item is seeded with only the attributes its category defines, so a
    shipped value outside its category would never reach the demo company. Judged against
    the categories of every preset at once, as a company that has tried each type holds."""
    from celerp.services.demo import _VERTICAL_ITEMS
    from celerp.services.field_schema import out_of_category_keys
    from celerp.services.vertical_presets import load_preset, merge_missing_preset_categories

    settings: dict = {}
    for vertical in _VERTICAL_ITEMS:
        settings = merge_missing_preset_categories(settings, load_preset(vertical, allow_hidden=True))[0]
    misfits = [(item["sku"], item["category"], sorted(out_of_category_keys(settings, item["category"])
                                                      & (item.get("attributes") or {}).keys()))
               for items in _VERTICAL_ITEMS.values() for item in items]
    assert [m for m in misfits if m[2]] == []
