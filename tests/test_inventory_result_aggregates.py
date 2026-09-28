# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GET /items totals describe exactly the rows the filters return.

The aggregates come from the same filtered set that `total` counts, so every
filter (and any combination) moves rows and totals together, pagination never
changes them, incompatible units are never added, unpriced rows are counted
rather than read as zero, and cost totals follow cost visibility.
"""
from __future__ import annotations

import datetime as _dt
import html
import re
import uuid

import pytest
from sqlalchemy import select

from celerp.services.auth import get_token_claims
from test_helpers import create_location, perm_setup

pytestmark = pytest.mark.asyncio

_PRICE_LISTS = [{"name": "Cost"}, {"name": "Retail"}, {"name": "Trade", "multiplier": 0.5}]


def _items(loc1: str, loc2: str) -> list[tuple[str, str, dict]]:
    return [
        ("item:agg-ring-1", loc1, {
            "sku": "RING-1", "name": "Gold ring", "category": "ring", "status": "available",
            "sell_by": "piece", "quantity": 2, "weight": 1.5, "weight_unit": "carat",
            "cost_total": 100, "retail_price": 300, "inventory_type": "stocked",
            "attributes": {"pieces": 2, "metal": "gold", "stone": "ruby"},
        }),
        ("item:agg-ring-2", loc2, {
            "sku": "RING-2", "name": "Silver ring", "category": "ring", "status": "available",
            "sell_by": "piece", "quantity": 3, "weight": 2.0, "weight_unit": "gram",
            "cost_total": 60, "retail_price": 50, "inventory_type": "stocked",
            "attributes": {"pieces": 3, "metal": "silver", "stone": "ruby"},
        }),
        ("item:agg-gem-1", loc1, {
            "sku": "GEM-1", "name": "Loose ruby", "category": "gem", "status": "available",
            "sell_by": "carat", "quantity": 1.25, "weight": 1.25, "weight_unit": "carat",
            "cost_total": 500, "inventory_type": "stocked",
            "attributes": {"stone": "ruby"},
        }),
        ("item:agg-gem-2", loc2, {
            "sku": "GEM-2", "name": "Loose sapphire", "category": "gem", "status": "available",
            "sell_by": "carat", "quantity": 0.75, "weight": 0.75, "weight_unit": "carat",
            "cost_total": 200, "retail_price": 900, "inventory_type": "component",
            "reorder_point": 5, "external_links": {"shopify": {"product_id": "9"}},
            "attributes": {"stone": "sapphire"},
        }),
        ("item:agg-sold-1", loc1, {
            "sku": "SOLD-1", "name": "Sold ring", "category": "ring", "status": "sold",
            "sell_by": "piece", "quantity": 1, "cost_total": 40, "retail_price": 80,
            "inventory_type": "stocked", "attributes": {"metal": "gold", "stone": "ruby"},
        }),
    ]


@pytest.fixture
async def seeded(client, session):
    from celerp.models.company import Company
    from celerp.models.projections import Projection

    ctx = await perm_setup(client, session)
    admin_h = ctx["admin_h"]
    company_id = uuid.UUID(str(get_token_claims(admin_h["Authorization"].split()[1])["company_id"]))
    # Items created by registration and perm_setup are not part of these fixtures; keep
    # them out of the default view.
    existing = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
    ))).scalars().all()
    for row in existing:
        row.state = {**row.state, "status": "archived"}
    preexisting_skus = {row.state.get("sku") for row in existing}
    loc1 = ctx["location_id"]
    loc2 = await create_location(client, admin_h, "Branch")
    now = _dt.datetime.now(_dt.timezone.utc)
    for entity_id, loc, state in _items(loc1, loc2):
        session.add(Projection(
            company_id=company_id, entity_id=entity_id, entity_type="item", state=state,
            version=1, location_id=uuid.UUID(loc), created_at=now, updated_at=now,
        ))
    company = await session.get(Company, company_id)
    company.settings = {**(company.settings or {}), "price_lists": _PRICE_LISTS, "base_price_list": "Retail"}
    await session.flush()
    return {**ctx, "loc1": loc1, "loc2": loc2, "preexisting_skus": preexisting_skus}


async def _list(client, headers, **params) -> dict:
    r = await client.get("/items", params={"limit": 500, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _expected(rows: list[dict], price_lists: list[str]) -> dict:
    """The contract, computed independently from the rows the same request returned."""
    qty: dict[str, float] = {}
    weight: dict[str, float] = {}
    pieces = None
    totals = {n: 0.0 for n in price_lists}
    missing = {n: 0 for n in price_lists}
    for r in rows:
        if r.get("quantity") is not None:
            qty[r.get("sell_by") or ""] = qty.get(r.get("sell_by") or "", 0.0) + float(r["quantity"])
        if r.get("weight") is not None:
            weight[r.get("weight_unit") or ""] = weight.get(r.get("weight_unit") or "", 0.0) + float(r["weight"])
        if r.get("pieces") is not None:
            pieces = (pieces or 0.0) + float(r["pieces"])
        for n in price_lists:
            if n == "Cost" and r.get("cost_total") is not None:
                totals[n] += float(r["cost_total"])
                continue
            unit = r.get(f"{n.lower()}_price")
            if unit is None or r.get("quantity") is None:
                missing[n] += 1
            else:
                totals[n] += float(unit) * float(r["quantity"])
    return {
        "item_count": len(rows),
        "quantity_by_unit": {k: pytest.approx(v) for k, v in qty.items()},
        "weight_by_unit": {k: pytest.approx(v) for k, v in weight.items()},
        "pieces_total": None if pieces is None else pytest.approx(pieces),
        "price_totals": {k: pytest.approx(v) for k, v in totals.items()},
        "price_missing": missing,
    }


def _filters(s: dict) -> dict[str, dict]:
    return {
        "none": {},
        "q": {"q": "ruby"},
        "q_and": {"q": "ring & gold"},
        "skus": {"skus": "RING-1,GEM-2"},
        "category": {"category": "ring"},
        "status_sold": {"status": "sold"},
        "status_all": {"status": "all"},
        "inventory_type": {"inventory_type": "component"},
        "location": {"location_id": s["loc2"]},
        "source": {"source": "shopify"},
        "low_stock": {"filter": "low_stock"},
        "attr_one": {"attr.metal": "gold"},
        "attr_two": {"attr.metal": "gold,silver", "attr.stone": "ruby"},
        "combo": {"category": "ring", "location_id": s["loc2"], "q": "ruby", "attr.metal": "silver"},
    }


_EXPECTED_SKUS = {
    "none": {"RING-1", "RING-2", "GEM-1", "GEM-2"},
    "q": {"RING-1", "RING-2", "GEM-1"},
    "q_and": {"RING-1"},
    "skus": {"RING-1", "GEM-2"},
    "category": {"RING-1", "RING-2"},
    "status_sold": {"SOLD-1"},
    "status_all": {"RING-1", "RING-2", "GEM-1", "GEM-2", "SOLD-1"},
    "inventory_type": {"GEM-2"},
    "location": {"RING-2", "GEM-2"},
    "source": {"GEM-2"},
    "low_stock": {"GEM-2"},
    "attr_one": {"RING-1"},
    "attr_two": {"RING-1", "RING-2"},
    "combo": {"RING-2"},
}


@pytest.mark.parametrize("case", list(_EXPECTED_SKUS))
async def test_aggregates_describe_exactly_the_filtered_rows(client, seeded, case):
    body = await _list(client, seeded["admin_h"], **_filters(seeded)[case])
    expected = _EXPECTED_SKUS[case] | (seeded["preexisting_skus"] if case == "status_all" else set())
    assert {r["sku"] for r in body["items"]} == expected
    assert body["aggregates"]["item_count"] == body["total"] == len(body["items"])
    assert body["aggregates"] == _expected(body["items"], ["Cost", "Retail", "Trade"])


async def test_default_view_totals(client, seeded):
    agg = (await _list(client, seeded["admin_h"]))["aggregates"]
    # Units are grouped, never added across: pieces and carats stay apart.
    assert agg["quantity_by_unit"] == {"piece": 5, "carat": 2}
    assert agg["weight_by_unit"] == {"carat": 3.5, "gram": 2}
    assert agg["pieces_total"] == 5
    assert agg["price_totals"]["Cost"] == 860
    # Retail: 300x2 + 50x3 + 900x0.75; GEM-1 has no retail price and is counted, not zeroed.
    assert agg["price_totals"]["Retail"] == 1425
    assert agg["price_missing"]["Retail"] == 1
    # Derived list priced through the pricing machinery (half of Retail).
    assert agg["price_totals"]["Trade"] == 712.5
    assert agg["price_missing"]["Trade"] == 1
    assert agg["price_missing"]["Cost"] == 0


async def test_pagination_does_not_change_aggregates(client, seeded):
    full = (await _list(client, seeded["admin_h"]))["aggregates"]
    for limit, offset in ((1, 0), (1, 3), (2, 2), (50, 100)):
        page = await _list(client, seeded["admin_h"], limit=limit, offset=offset)
        assert page["aggregates"] == full


async def test_cost_totals_hidden_without_cost_visibility(client, seeded):
    agg = (await _list(client, seeded["operator_h"]))["aggregates"]
    assert "Cost" not in agg["price_totals"]
    assert "Cost" not in agg["price_missing"]
    assert agg["price_totals"]["Retail"] == 1425


async def test_holdings_and_sold_totals_unchanged(client, seeded):
    body = await _list(client, seeded["admin_h"], status="sold")
    assert "sold_total" in body and "sold_total_missing" in body
    assert body["aggregates"]["item_count"] == 1


# UI: the chip bar renders the list endpoint's aggregates for the current search.

_UI_AGGREGATES = {
    "item_count": 1234,
    "quantity_by_unit": {"piece": 47, "carat": 18.35},
    "weight_by_unit": {"carat": 21.4, "gram": 12},
    "pieces_total": 342,
    "price_totals": {"Cost": 12450, "Retail": 21900.5},
    "price_missing": {"Cost": 0, "Retail": 3},
}


async def _render_chips(monkeypatch, p: dict, aggregates: dict) -> tuple[list[str], dict]:
    from fasthtml.common import to_xml

    import ui.routes.inventory as inv
    from test_inventory_content_degradation import _COMPANY, _params

    sent: dict = {}

    async def _get_valuation(_token, **_kw):
        # Navigation counts only; its store-wide price totals must not reach the bar.
        return {"item_count": 9, "category_counts": {"ring": 9}, "price_totals": {"Retail": 999999}}

    async def _list_items(_token, params):
        sent.update(params)
        return {"items": [], "total": aggregates["item_count"], "aggregates": aggregates}

    monkeypatch.setattr(inv.api, "get_valuation", _get_valuation)
    monkeypatch.setattr(inv.api, "list_items", _list_items)
    ft = await inv._inventory_content(
        "tok", _params(**p), [], {}, {}, _COMPANY, [], [{"name": "piece"}], {},
        lang="en", role="owner",
    )
    bar = re.search(r'<div class="valuation-bar">(.*?)</div>', to_xml(ft), re.S)
    chips = [html.unescape(c).strip() for c in re.findall(r'<span class="val-chip">(.*?)</span>', bar.group(1), re.S)]
    return chips, sent


async def test_ui_chip_bar_renders_list_aggregates(monkeypatch):
    chips, sent = await _render_chips(
        monkeypatch, {"q": "ruby", "category": "ring", "attr_filters": {"metal": "gold"}}, _UI_AGGREGATES,
    )
    # The totals come from the same request that fetched the rows for this search.
    assert sent["q"] == "ruby" and sent["category"] == "ring" and sent["attr.metal"] == "gold"
    assert chips == [
        "Items: 1,234",
        "Quantity: 47 piece",
        "Quantity: 18.35 carat",
        "Weight: 21.4 carat",
        "Weight: 12 gram",
        "Pieces: 342",
        "Cost: $12,450.00",
        "Retail: $21,900.50 (3 without a price)",
    ]


async def test_ui_chip_bar_single_unit_and_no_pieces(monkeypatch):
    chips, _ = await _render_chips(monkeypatch, {}, {
        "item_count": 2, "quantity_by_unit": {"piece": 5}, "weight_by_unit": {},
        "pieces_total": None, "price_totals": {"Retail": 750}, "price_missing": {"Retail": 0},
    })
    assert chips == ["Items: 2", "Quantity: 5 piece", "Retail: $750.00"]
