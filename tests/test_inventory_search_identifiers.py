# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Identifier-aware inventory search, end to end through GET /items and the CSV export.

A scanner types each label followed by Enter, which the search box turns into a comma,
so `barcode: ` followed by five scans is `barcode: a,b,c,d,e,`. That search must list
exactly the scanned items, name the scans that matched nothing, and rank exact
identifier hits above partial ones for a bare term. The demo company supplies the
scanned labels; a few extra items create the collisions the demo data does not have.
"""
from __future__ import annotations

import csv
import io
import uuid

import pytest
from fasthtml.common import to_xml

from test_helpers import merge_items


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "ScanCo", "email": f"scan-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/companies/me/business-type", json={"vertical": "agricultural"}, headers=h)
    assert r.status_code == 200, r.text
    return h


async def _item(client, h, sku: str, name: str, **fields) -> str:
    r = await client.post("/items", headers=h, json={
        "sku": sku, "name": name, "sell_by": "piece", "quantity": 1, **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _list(client, h, q: str, **params) -> dict:
    r = await client.get("/items", headers=h, params={"q": q, "limit": 500, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def _collisions(client, h) -> dict[str, str]:
    """An exact barcode 1042, two lots sharing SKU 1042, SKUs that start with 1042 and a
    barcode that starts with 1042. All are drafts."""
    return {
        "barcode_1042": await _item(client, h, "RING-A", "Gold ring", barcode="1042"),
        "lot_1": await _item(client, h, "1042", "Ring lot one", barcode="50001"),
        "lot_2": await _item(client, h, "1042", "Ring lot two", barcode="50002"),
        "sku_1042_1": await _item(client, h, "1042.1", "Ring variant one", barcode="50003"),
        "sku_1042_2": await _item(client, h, "1042.2", "Ring variant two", barcode="50004"),
        "barcode_10420": await _item(client, h, "D-1", "Chain", barcode="10420"),
    }


@pytest.mark.asyncio
async def test_scoped_barcode_is_exact(client):
    """Red statement: `barcode: 1042` was a substring match, so it also listed the item
    with barcode 10420."""
    h = await _owner(client)
    ids = await _collisions(client, h)
    got = await _list(client, h, "barcode: 1042")
    assert [i["id"] for i in got["items"]] == [ids["barcode_1042"]]


@pytest.mark.asyncio
async def test_scan_five_codes_not_found(client):
    """Red statement: the scanned codes after the first were searched in every field and
    a scan that matched nothing simply did not appear; there was no not_found list."""
    h = await _owner(client)
    demo = {i["sku"]: i for i in (await _list(client, h, ""))["items"] if i["sku"].startswith("DEMO-AGR-")}
    scanned = [demo[s]["barcode"] for s in ("DEMO-AGR-001", "DEMO-AGR-002", "DEMO-AGR-003", "DEMO-AGR-004")]
    unknown = "2099999999999"
    q = "barcode: " + ",".join(scanned[:2] + [unknown] + scanned[2:]) + ","
    got = await _list(client, h, q)
    assert sorted(i["barcode"] for i in got["items"]) == sorted(scanned)
    assert got["not_found"] == [unknown]
    # Every scan found: the list is empty, not absent.
    assert (await _list(client, h, "barcode: " + scanned[0]))["not_found"] == []


@pytest.mark.asyncio
async def test_unscoped_ranking_tiers(client):
    """Red statement: a bare 1042 listed every hit newest first, so the item whose barcode
    is exactly 1042 came last. Now exact barcode hits come first, then the lots whose SKU
    is exactly 1042, then partial matches; each item once."""
    h = await _owner(client)
    ids = await _collisions(client, h)
    items = (await _list(client, h, "1042"))["items"]
    order = [i["id"] for i in items]
    assert len(order) == len(set(order)) == 6
    assert order[0] == ids["barcode_1042"]
    assert set(order[1:3]) == {ids["lot_1"], ids["lot_2"]}
    assert set(order[3:]) == {ids["sku_1042_1"], ids["sku_1042_2"], ids["barcode_10420"]}
    exact = {i["id"]: i.get("q_exact") for i in items}
    assert exact[ids["barcode_1042"]] == "barcode"
    assert exact[ids["lot_1"]] == exact[ids["lot_2"]] == "sku"
    assert exact[ids["barcode_10420"]] is None
    # all: ranks the same way as a bare term.
    assert [i["id"] for i in (await _list(client, h, "all: 1042"))["items"]] == order


@pytest.mark.asyncio
async def test_explicit_sort_overrides_tiers(client):
    """A sort column the user picked wins over the tiers (least surprise). The demo
    company picks by expiry, which already overrides a column sort on the default view,
    so the guard sorts the drafts view, where the column sort applies."""
    h = await _owner(client)
    await _collisions(client, h)
    items = (await _list(client, h, "1042", status="draft", sort="sku", dir="asc"))["items"]
    assert [i["sku"] for i in items] == ["1042", "1042", "1042.1", "1042.2", "D-1", "RING-A"]


@pytest.mark.asyncio
async def test_merged_lot_not_in_exact_tier(client):
    """A merged source keeps its barcode but is no longer a physical lot, so it is
    shown as a partial match (when its status is listed), never in the exact tier."""
    h = await _owner(client)
    a = await _item(client, h, "M-1", "Merge me", barcode="60001", status="available")
    b = await _item(client, h, "M-1", "Merge me too", barcode="60002", status="available")
    r = await merge_items(client, headers=h, json={"source_entity_ids": [a, b], "target_sku_from": a})
    assert r.status_code == 200, r.text
    merged = (await client.get(f"/items/{r.json()['id']}", headers=h)).json()
    # The merged source is still listed under every status, but never as an exact hit.
    by_id = {i["id"]: i for i in (await _list(client, h, "60001", status="all"))["items"]}
    assert by_id[a]["status"] == "merged"
    assert by_id[a].get("q_exact") is None
    # The lot the merge produced is a live exact barcode hit.
    hits = (await _list(client, h, merged["barcode"], status="all"))["items"]
    assert hits[0]["id"] == merged["id"] and hits[0]["q_exact"] == "barcode"


@pytest.mark.asyncio
async def test_csv_export_matches_list_for_scan(client):
    """Red statement: the export used the old meaning too, so `barcode: 1042, 50003`
    exported the 10420 item as well. The export and the list return one set."""
    h = await _owner(client)
    ids = await _collisions(client, h)
    q = "barcode: 1042, 50003"
    listed = {i["id"] for i in (await _list(client, h, q))["items"]}
    r = await client.get("/items/export/csv", headers=h, params={"q": q})
    assert r.status_code == 200, r.text
    exported = {row["id"] for row in csv.DictReader(io.StringIO(r.text))}
    assert listed == exported == {ids["barcode_1042"], ids["sku_1042_1"]}


@pytest.mark.asyncio
async def test_inventory_content_shows_not_found_and_exact_rows(monkeypatch):
    """Red statement: the list had no Not found line and no mark on exact rows."""
    import ui.routes.inventory as inv

    async def _get_valuation(_token, _params=None):
        return {}

    async def _list_items(_token, _params):
        return {"items": [{"id": "i1", "entity_id": "i1", "sku": "RING-A", "name": "Gold ring",
                           "barcode": "1042", "status": "available", "q_exact": "barcode"}],
                "total": 1, "not_found": ["1099", "1100"]}

    monkeypatch.setattr(inv.api, "get_valuation", _get_valuation)
    monkeypatch.setattr(inv.api, "list_items", _list_items)
    p = {"q": "barcode: 1042, 1099, 1100", "skus": "", "page": 1, "status": "", "category": "",
         "inventory_type": "", "location_id": "", "source": "", "filter": "", "on_memo_to": "",
         "consigned_from": "", "attr_filters": {}, "sort": "", "dir": "desc", "per_page": 50, "cols": []}
    xml = to_xml(await inv._inventory_content(
        "tok", p, [], {}, {}, {"currency": "USD", "settings": {}}, [], [{"name": "each"}], {},
        lang="en", role="owner"))
    assert "Not found: 1099, 1100" in xml
    assert "data-row--exact" in xml
    assert "Exact barcode match" in xml
