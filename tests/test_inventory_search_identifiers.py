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


async def _render(monkeypatch, client, h, q: str) -> str:
    """Render the inventory list for ``q`` from the real GET /items response."""
    import ui.routes.inventory as inv

    api_items = await _list(client, h, q)

    async def _get_valuation(_token, _params=None):
        return {}

    async def _list_items(_token, _params):
        return api_items

    monkeypatch.setattr(inv.api, "get_valuation", _get_valuation)
    monkeypatch.setattr(inv.api, "list_items", _list_items)
    p = {"q": q, "skus": "", "page": 1, "status": "", "category": "",
         "inventory_type": "", "location_id": "", "source": "", "filter": "", "on_memo_to": "",
         "consigned_from": "", "attr_filters": {}, "sort": "", "dir": "desc", "per_page": 50, "cols": []}
    return to_xml(await inv._inventory_content(
        "tok", p, [], {}, {}, {"currency": "USD", "settings": {}}, [], [{"name": "each"}], {},
        lang="en", role="owner"))


@pytest.mark.asyncio
async def test_inventory_content_scoped_scan_shows_not_found_without_exact_rows(client, monkeypatch):
    """A scoped scan is a filter on its field, so the real API marks no row exact: the
    list names the scans that matched nothing and marks no row as an exact match."""
    h = await _owner(client)
    await _collisions(client, h)
    xml = await _render(monkeypatch, client, h, "barcode: 1042, 1099, 1100")
    assert "Not found: 1099, 1100" in xml
    assert "data-row--exact" not in xml
    assert "Exact barcode match" not in xml


@pytest.mark.asyncio
async def test_inventory_content_bare_term_marks_exact_rows(client, monkeypatch):
    """Red statement: the list had no mark on exact rows. A bare term ranks, so the row
    whose barcode is exactly the term is marked, from the real API output."""
    h = await _owner(client)
    await _collisions(client, h)
    xml = await _render(monkeypatch, client, h, "1042")
    assert "data-row--exact" in xml
    assert "Exact barcode match" in xml
    assert "Not found" not in xml


@pytest.mark.asyncio
async def test_q_exact_scoped_versus_bare_term(client):
    """The API marks an exact hit for a bare term and none for a fully scoped search,
    which the two rendering tests above rely on."""
    h = await _owner(client)
    ids = await _collisions(client, h)
    scoped = (await _list(client, h, "barcode: 1042"))["items"]
    assert [(i["id"], i.get("q_exact")) for i in scoped] == [(ids["barcode_1042"], None)]
    bare = {i["id"]: i.get("q_exact") for i in (await _list(client, h, "1042"))["items"]}
    assert bare[ids["barcode_1042"]] == "barcode"


# ── Ordering: FEFO is the primary order, exact identifier hits rank inside it ──


def _row(id_: str, *, expires_at: str | None = None, q_exact: str | None = None,
         updated_at: str = "2030-01-01T00:00:00", **fields) -> dict:
    return {"id": id_, "entity_id": id_, "name": id_, "expires_at": expires_at,
            "q_exact": q_exact, "updated_at": updated_at, **fields}


def _ordered(rows: list[dict], **kw) -> list[str]:
    from celerp_inventory.search import apply_item_order

    args = {"inventory_method": None, "sort": None, "direction": "desc", "status": None, **kw}
    apply_item_order(rows, **args)
    return [r["id"] for r in rows]


def test_fefo_wins_over_exact_identifier_tier():
    """Red statement: the exact-identifier sort ran after FEFO, so an exact barcode hit
    expiring next month was listed above a partial hit expiring tomorrow. FEFO stays the
    primary order; the tier only orders items that share an expiry date."""
    rows = [
        _row("partial_tomorrow", expires_at="2030-01-02", q_exact=None),
        _row("barcode_next_month", expires_at="2030-02-01", q_exact="barcode"),
    ]
    assert _ordered(rows, inventory_method="fefo") == ["partial_tomorrow", "barcode_next_month"]
    # The same with the available status and a column sort, which FEFO also overrides.
    rows.reverse()
    assert _ordered(rows, inventory_method="fefo", status="available", sort="name",
                    direction="asc") == ["partial_tomorrow", "barcode_next_month"]


def test_fefo_ranks_exact_hits_within_one_expiry_date():
    """Neighbour guard: inside one expiry date (and among items with no expiry, which
    FEFO lists last) the exact tier still leads."""
    rows = [
        _row("none_partial", q_exact=None),
        _row("none_sku", q_exact="sku"),
        _row("jan_partial", expires_at="2030-01-02", q_exact=None),
        _row("jan_sku", expires_at="2030-01-02", q_exact="sku"),
        _row("jan_barcode", expires_at="2030-01-02", q_exact="barcode"),
    ]
    assert _ordered(rows, inventory_method="fefo") == [
        "jan_barcode", "jan_sku", "jan_partial", "none_sku", "none_partial"]


def test_exact_tier_leads_without_expiry_policy():
    """Neighbour guard: with no expiry policy (FIFO or unset) the exact tier leads the
    default most-recently-updated order."""
    for method in ("fifo", None):
        rows = [
            _row("partial_new", q_exact=None, updated_at="2030-03-01T00:00:00"),
            _row("sku_mid", q_exact="sku", updated_at="2030-02-01T00:00:00"),
            _row("barcode_old", q_exact="barcode", updated_at="2030-01-01T00:00:00"),
        ]
        assert _ordered(rows, inventory_method=method) == ["barcode_old", "sku_mid", "partial_new"]


def test_user_sort_wins_over_exact_tier():
    """Neighbour guard: a column the user sorts by wins over the tier, under FIFO and
    under FEFO for a status other than available."""
    def rows():
        return [
            _row("a_partial", q_exact=None, sku="A"),
            _row("c_barcode", q_exact="barcode", sku="C"),
            _row("b_sku", q_exact="sku", sku="B"),
        ]
    assert _ordered(rows(), inventory_method="fifo", sort="sku", direction="asc") == [
        "a_partial", "b_sku", "c_barcode"]
    assert _ordered(rows(), inventory_method="fefo", status="draft", sort="sku", direction="asc") == [
        "a_partial", "b_sku", "c_barcode"]


@pytest.mark.asyncio
async def test_fefo_list_and_global_search_lead_with_soonest_expiry(client, session):
    """Red statement: in a FEFO company a bare `1042` listed the exact barcode hit that
    expires next month above the partial hit that expires tomorrow, in the list and in
    the global search bar alike."""
    from celerp.services.auth import get_token_claims
    from celerp.services.company_lock import locked_company
    from celerp_inventory.search import global_search

    r = await client.post("/auth/register", json={
        "company_name": "FefoCo", "email": f"fefo-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    company_id = get_token_claims(tok)["company_id"]
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "inventory_method": "fefo"}
    await session.flush()
    partial = await _item(client, h, "1042-A", "Partial tomorrow", status="available",
                          attributes={"expiry_date": "2030-01-02"})
    exact = await _item(client, h, "B-1", "Exact next month", barcode="1042", status="available",
                        attributes={"expiry_date": "2030-02-01"})
    listed = (await _list(client, h, "1042", status="all"))["items"]
    assert [i["id"] for i in listed] == [partial, exact]
    assert [i["q_exact"] for i in listed] == [None, "barcode"]
    found = (await global_search(session, company_id, "owner", "1042", 10))["items"]
    assert [i["id"] for i in found] == [partial, exact]


# ── q_exact comes only from the group that matched ──


def _matched(item: dict, q: str) -> list[dict]:
    from celerp_inventory.search import apply_query_match, parse_item_query

    groups = parse_item_query(q, [item], {})
    return apply_query_match([item], groups, {})


def test_failed_or_group_does_not_set_exact_tier():
    """Red statement: q_exact looked at every bare term in every group, so for
    `name: ring, all: ABC & qty: 999` the Ring with SKU ABC and quantity 1 matched the
    first group but was ranked an exact SKU hit by the failed second group."""
    ring = {"id": "r1", "name": "Ring", "sku": "ABC", "quantity": 1, "status": "available"}
    hit = _matched(ring, "name: ring, all: ABC & qty: 999")
    assert [h["id"] for h in hit] == ["r1"]
    assert hit[0]["q_exact"] is None
    assert hit[0]["q_match"] == [{"field": "name", "match": "ring"}]


def test_matched_group_bare_identifier_still_sets_exact_tier():
    """Neighbour guard: a bare identifier in the group that did match still ranks, and
    a range term in it never does."""
    ring = {"id": "r1", "name": "Ring", "sku": "ABC", "quantity": 7, "status": "available"}
    assert _matched(dict(ring), "zzz, ABC")[0]["q_exact"] == "sku"
    assert _matched(dict(ring), "all: ABC & qty: 7")[0]["q_exact"] == "sku"
    ranged = {"id": "r2", "name": "Bolt", "sku": "5-10", "quantity": 7, "status": "available"}
    assert _matched(ranged, "5-10")[0]["q_exact"] is None


def test_exact_tier_from_every_matching_group_in_any_order():
    """Red statement: only the first matching group set q_exact, so `ring, R-100` on the
    Gold ring with SKU R-100 matched `ring` by name first and lost its exact SKU rank,
    while `R-100, ring` kept it. Every group that matched counts, in either order, and
    the exact row leads a newer partial match."""
    ring = {"id": "r1", "name": "Gold ring", "sku": "R-100", "quantity": 1, "status": "available"}
    for q in ("R-100, ring", "ring, R-100"):
        hit = _matched(dict(ring), q)[0]
        assert hit["q_exact"] == "sku", q
    # q_match still names the first group that matched.
    assert _matched(dict(ring), "ring, R-100")[0]["q_match"] == [{"field": "name", "match": "ring"}]
    from celerp_inventory.search import apply_query_match, parse_item_query

    exact = {"id": "exact", "name": "Gold ring", "sku": "R-100", "status": "available",
             "updated_at": "2030-01-01"}
    newer = {"id": "newer", "name": "Gold ring", "sku": "R-1000", "status": "available",
             "updated_at": "2030-06-01"}
    rows = apply_query_match([exact, newer], parse_item_query("ring, R-100", [exact, newer], {}), {})
    assert _ordered(rows) == ["exact", "newer"]


def test_best_tier_wins_across_matching_groups():
    """Neighbour guard: when two matching groups hit different identifier fields, the
    best tier wins whatever order the groups are typed in."""
    item = {"id": "i1", "name": "Chain", "sku": "C-7", "barcode": "7001", "status": "available"}
    assert _matched(dict(item), "C-7, 7001")[0]["q_exact"] == "barcode"
    assert _matched(dict(item), "7001, C-7")[0]["q_exact"] == "barcode"
