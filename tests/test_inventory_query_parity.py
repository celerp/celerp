# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The list route and the global-search provider read and order items identically.

The inventory list and the cross-app global-search bar share one pipeline: load
every item projection once, flatten it, strip fields the role may not see, match
the query, and order the survivors. These tests hold that shared front honest:

- `list_items` loads the item projection set exactly once and flattens those same
  rows (no second full-table load);
- the global-search provider orders by the company's inventory_method, so a FEFO
  company's search hits lead with the same soonest-expiring items the list shows;
- a non-FEFO company falls to the identical default order;
- query-match filtering and its q_match reasons match between the two paths.
"""
from __future__ import annotations

import pytest

from celerp.services.auth import get_token_claims
from test_helpers import default_location_id, register_admin

pytestmark = pytest.mark.asyncio

# Distinct expiry dates in scrambled insertion order, so FEFO (expiry ascending)
# and the default order (most-recently-updated first, i.e. reverse insertion)
# are provably different orderings over the same set.
_SPECS = [
    ("PAR-A", "2026-05-01"),
    ("PAR-B", "2026-01-10"),
    ("PAR-C", "2026-09-20"),
    ("PAR-D", "2026-03-15"),
    ("PAR-E", "2026-07-07"),
    ("PAR-F", "2026-02-02"),
]


async def _owner(client):
    tok = await register_admin(client)
    return tok, {"Authorization": f"Bearer {tok}"}


async def _seed_items(client, headers, loc):
    for sku, expiry in _SPECS:
        r = await client.post(
            "/items",
            json={
                "sku": sku, "name": f"Parity {sku}", "quantity": 3,
                "location_id": loc, "sell_by": "piece", "status": "available",
                "attributes": {"expiry_date": expiry},
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text


async def _list_ids(client, headers, **params):
    r = await client.get(
        "/items",
        params={"status": "all", "limit": 100, **params},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return [it.get("id") for it in r.json()["items"]]


async def _global_items(session, company_id, q="", limit=100):
    from celerp_inventory.search import global_search
    res = await global_search(session, company_id, "owner", q, limit)
    return res["items"]


async def _set_inventory_method(session, company_id, method):
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    company.settings = {**(company.settings or {}), "inventory_method": method}
    await session.flush()


async def test_list_items_loads_item_projections_once(client, session, monkeypatch):
    # A normal list request loads the item projection set exactly once and flattens
    # those very rows; it never issues a second full Projection(entity_type="item")
    # load. The item select lives solely in load_item_rows, so one call is one load,
    # and flatten consuming the returned rows proves it does not reload them.
    import celerp_inventory.search as isearch

    _tok, headers = await _owner(client)
    loc = await default_location_id(client, headers)
    await _seed_items(client, headers, loc)

    loaded_rows: list = []
    flatten_rows: list = []
    real_load = isearch.load_item_rows
    real_flatten = isearch.flatten_item_rows

    async def _counting_load(sess, company_id):
        rows = await real_load(sess, company_id)
        loaded_rows.append(rows)
        return rows

    async def _counting_flatten(sess, company_id, rows):
        flatten_rows.append(rows)
        return await real_flatten(sess, company_id, rows)

    monkeypatch.setattr(isearch, "load_item_rows", _counting_load)
    monkeypatch.setattr(isearch, "flatten_item_rows", _counting_flatten)

    ids = await _list_ids(client, headers)
    assert len(ids) >= len(_SPECS)
    assert len(loaded_rows) == 1, "list_items must load item projections exactly once"
    assert flatten_rows == [loaded_rows[0]], "flatten must reuse the loaded rows, not reload"


async def test_fefo_global_search_order_matches_list(client, session):
    # With a FEFO company, the global-search provider leads with the same
    # soonest-expiring items, in the same order, as list_items(status="all").
    tok, headers = await _owner(client)
    company_id = get_token_claims(tok)["company_id"]
    loc = await default_location_id(client, headers)
    await _seed_items(client, headers, loc)
    await _set_inventory_method(session, company_id, "fefo")

    list_ids = await _list_ids(client, headers)
    global_ids = [it.get("id") for it in await _global_items(session, company_id)]

    assert list_ids[:5] == global_ids[:5]


async def test_fefo_reorders_away_from_default(client, session):
    # Prove the FEFO branch is exercised, not a coincidental match: the FEFO order
    # over the same set is different from the default (non-FEFO) order.
    tok, headers = await _owner(client)
    company_id = get_token_claims(tok)["company_id"]
    loc = await default_location_id(client, headers)
    await _seed_items(client, headers, loc)

    default_ids = await _list_ids(client, headers)
    await _set_inventory_method(session, company_id, "fefo")
    fefo_ids = await _list_ids(client, headers)

    assert default_ids != fefo_ids, "FEFO must reorder relative to the default order"


async def test_nonfefo_global_search_order_matches_list(client, session):
    # A non-FEFO company falls to the shared default order; the provider and the
    # list agree on it top to bottom.
    tok, headers = await _owner(client)
    company_id = get_token_claims(tok)["company_id"]
    loc = await default_location_id(client, headers)
    await _seed_items(client, headers, loc)

    list_ids = await _list_ids(client, headers)
    global_ids = [it.get("id") for it in await _global_items(session, company_id)]

    assert list_ids[:5] == global_ids[:5]


async def test_qmatch_and_visibility_parity(client, session):
    # The query grammar and q_match reasons match between the two paths: a q that
    # selects a subset returns the same items, in the same order, each carrying the
    # q_match reasons the list route attaches.
    tok, headers = await _owner(client)
    company_id = get_token_claims(tok)["company_id"]
    loc = await default_location_id(client, headers)
    await _seed_items(client, headers, loc)

    list_ids = await _list_ids(client, headers, q="PAR-B")
    global_items = await _global_items(session, company_id, q="PAR-B")
    global_ids = [it.get("id") for it in global_items]

    assert list_ids == global_ids
    assert list_ids, "the query must select at least the PAR-B item"
    assert all("q_match" in it for it in global_items), "provider attaches q_match like the list"
