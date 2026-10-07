# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GET /items?filter=demo lists setup's samples that are still removable: seeded by
setup and never edited or used since, the same set a first product import removes.
The dashboard's "Remove demo items" link opens that list and offers Delete on it, so
an edited sample, a renamed one, one used on a document and the owner's own item
named "[DEMO] ..." must never be on it."""
from __future__ import annotations

import re
import uuid

import pytest

from ui.components.demo_items import DEMO_ITEMS_FILTER


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "DemoCo", "email": f"demo-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/companies/me/business-type", json={"vertical": "agricultural"}, headers=h)
    assert r.status_code == 200, r.text
    return h


async def _skus(client, h, **params) -> dict[str, dict]:
    r = await client.get("/items", headers=h, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return {i["sku"]: i for i in r.json()["items"]}


@pytest.mark.asyncio
async def test_demo_filter_lists_only_untouched_samples_and_delete_keeps_the_rest(client):
    """Red statement: before the change ?filter=demo is ignored and the link searched
    name:[DEMO], so the list held the edited and used samples and MINE-1, and Delete
    on it removed them with their history."""
    h = await _owner(client)
    demo = {s: i for s, i in (await _skus(client, h)).items() if s.startswith("DEMO-AGR-")}
    assert len(demo) == 5, sorted(demo)
    edited, renamed, used = demo["DEMO-AGR-001"], demo["DEMO-AGR-005"], demo["DEMO-AGR-002"]
    r = await client.patch(f"/items/{edited['id']}", headers=h, json={
        "fields_changed": {"quantity": {"old": edited.get("quantity"), "new": 999}}})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/items/{renamed['id']}", headers=h, json={
        "fields_changed": {"name": {"old": renamed["name"], "new": "House cattle feed"}}})
    assert r.status_code == 200, r.text
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c:1", "contact_name": "Buyer",
        "line_items": [{"item_id": used["id"], "sku": used["sku"], "description": "Tomatoes",
                        "quantity": 1, "unit_price": 5, "line_total": 5}],
    })
    assert r.status_code == 200, r.text
    r = await client.post("/items", headers=h, json={
        "sku": "MINE-1", "name": "[DEMO] My own showroom piece", "sell_by": "piece", "quantity": 1})
    assert r.status_code == 200, r.text

    listed = await _skus(client, h, filter=DEMO_ITEMS_FILTER)
    assert set(listed) == {"DEMO-AGR-003", "DEMO-AGR-004"}, sorted(listed)

    # Select-all plus Delete on that list: exactly the listed ids, as untouched samples.
    r = await client.post("/items/bulk/delete", headers=h, json={
        "entity_ids": [i["id"] for i in listed.values()], "untouched_samples_only": True})
    assert r.status_code == 200, r.text
    left = await _skus(client, h, status="all")
    assert {"DEMO-AGR-001", "DEMO-AGR-002", "DEMO-AGR-005", "MINE-1"} <= set(left)
    assert left["DEMO-AGR-001"]["quantity"] == 999
    assert not {"DEMO-AGR-003", "DEMO-AGR-004"} & set(left)
    assert await _skus(client, h, filter=DEMO_ITEMS_FILTER) == {}


async def _demo_list_delete(owner_ui, ids: list[str], page: str) -> str:
    """Delete from an inventory list page, the way its bulk Delete posts (htmx names the
    page it came from)."""
    r = await owner_ui.post("/api/items/bulk/delete", data={"selected": ids},
                            headers={"HX-Request": "true", "HX-Current-URL": f"http://ui{page}"})
    assert r.status_code == 200, r.text
    return r.text


async def _agricultural_samples(owner_ui) -> list[dict]:
    r = await owner_ui.api.post("/companies/me/business-type", json={"vertical": "agricultural"})
    assert r.status_code == 200, r.text
    r = await owner_ui.api.get("/items", params={"filter": DEMO_ITEMS_FILTER, "limit": 500})
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.mark.asyncio
async def test_demo_list_delete_keeps_a_sample_edited_after_the_list_was_shown(owner_ui):
    """A stale demo list (or a second tab) posts a sample edited since it was shown: it is
    no longer a sample, so the list's Delete keeps it and says so. Red statement: before
    the change bulk delete removed every posted id, so the edited sample was hard-deleted
    and the message read "Deleted: 5."."""
    from ui.i18n import t
    listed = await _agricultural_samples(owner_ui)
    assert len(listed) == 5, [i["sku"] for i in listed]
    edited = listed[0]
    r = await owner_ui.api.patch(f"/items/{edited['id']}", json={
        "fields_changed": {"name": {"old": edited["name"], "new": "Kept: edited in another tab"}}})
    assert r.status_code == 200, r.text
    html = await _demo_list_delete(owner_ui, [i["id"] for i in listed], "/inventory?filter=demo")
    left = (await owner_ui.api.get("/items", params={"status": "all", "limit": 500})).json()["items"]
    assert [i["name"] for i in left] == ["Kept: edited in another tab"]
    assert t("inventory.bulk_deleted", n=4) in html
    assert t("settings.business_type_changes.demo_kept", count=1) in html


@pytest.mark.asyncio
async def test_inventory_list_delete_refuses_an_edited_sample_that_is_stock(owner_ui):
    """Outside the demo list Delete removes only draft mistakes: an edited sample is stock,
    so it is kept and the answer names it."""
    listed = await _agricultural_samples(owner_ui)
    edited = listed[0]
    r = await owner_ui.api.patch(f"/items/{edited['id']}", json={
        "fields_changed": {"name": {"old": edited["name"], "new": "Edited"}}})
    assert r.status_code == 200, r.text
    html = await _demo_list_delete(owner_ui, [edited["id"]], "/inventory")
    assert "flash--error" in html and "Nothing was deleted." in html and edited["sku"] in html
    left = (await owner_ui.api.get("/items", params={"status": "all", "limit": 500})).json()["items"]
    assert edited["id"] in {i["id"] for i in left}


@pytest.mark.asyncio
async def test_searching_the_demo_list_stays_on_the_demo_list(owner_ui):
    """A search typed on the demo list searches the samples, keeping its hint and Delete.
    Red statement: the search box asked for /inventory/content with no filter, so a search
    left the demo list."""
    await _agricultural_samples(owner_ui)
    page = (await owner_ui.get(f"/inventory?filter={DEMO_ITEMS_FILTER}")).text
    box = re.search(r'<input[^>]*id="search-input"[^>]*>', page)
    assert box and f"filter={DEMO_ITEMS_FILTER}" in box.group(0), box and box.group(0)
