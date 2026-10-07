# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every record that names a deleted item shows it as "<SKU> [Deleted]".

Item lists leave deleted items out, so each surface reads the items it names on
their own: the production worksheet, the run sheet, the production-run queue, the
reconcile screen, and the split or transform parent on an item's activity.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _COMPANY, _ITEM, _SCHEMA, _authed, ui_client  # noqa: F401  (ui_client is a fixture)

pytestmark = pytest.mark.asyncio

FLOUR = {"id": "item:flour", "sku": "FLOUR", "name": "Flour", "status": "deleted"}
CAKE = {"id": "item:cake", "sku": "CAKE", "name": "Cake", "status": "deleted"}
RUN = {"id": "mfg:abc12345", "status": "planned", "output_item_id": "item:cake",
       "expected_outputs": [{"item_id": "item:cake", "sku": "CAKE", "quantity": 1}],
       "inputs": [{"item_id": "item:flour", "quantity": 2}]}


def _items(*found: dict):
    """The item read the surfaces use, answering with *found* and nothing else."""
    by_id = {it["id"]: it for it in found}
    return patch("ui.api_client.get_items_metadata",
                 new=AsyncMock(side_effect=lambda _tok, ids: {i: by_id[i] for i in ids if i in by_id}))


def _unlisted():
    return patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0}))


async def test_the_worksheet_marks_a_deleted_component(ui_client):
    recipe_item = {**_ITEM, "id": "item:cake", "recipe": {"components": [{"item_id": "item:flour", "quantity": 2}]}}
    with (patch("ui.api_client.get_item", new=AsyncMock(return_value=recipe_item)), _unlisted(), _items(FLOUR),
          patch("ui.api_client.get_category_labels", new=AsyncMock(return_value={}))):
        r = await ui_client.get("/inventory/item:cake/worksheet/print", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "FLOUR [Deleted]" in r.text


async def test_the_run_sheet_marks_a_deleted_input(ui_client):
    with patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=RUN)), _unlisted(), _items(FLOUR):
        r = await ui_client.get(f"/manufacturing/{RUN['id']}/run-sheet/print", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "FLOUR [Deleted]" in r.text


async def test_the_run_queue_marks_a_deleted_product(ui_client):
    with patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": [RUN]})), _items(CAKE):
        r = await ui_client.get("/manufacturing/production", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "CAKE [Deleted]" in r.text


async def test_the_run_queue_leaves_a_product_that_is_not_deleted_unmarked(ui_client):
    with (patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": [RUN]})),
          _items({**CAKE, "status": "draft"})):
        r = await ui_client.get("/manufacturing/production", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "CAKE" in r.text and "[Deleted]" not in r.text


async def test_the_reconcile_screen_marks_a_deleted_component_and_product(ui_client):
    needs = {"reason": "books disagree", "components": [{"item_id": "item:flour", "quantity": 2.5, "sku": "FLOUR",
                                                          "name": "Flour"}]}
    with (patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=RUN)),
          patch("ui.api_client.mfg_reconcile_needs", new=AsyncMock(return_value=needs)),
          patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value={"roles": [], "older_stock": {}})),
          _items(FLOUR, CAKE)):
        r = await ui_client.get(f"/manufacturing/runs/{RUN['id']}/reconcile", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "FLOUR [Deleted]" in r.text
    assert "CAKE [Deleted]" in r.text


_SPLIT_FROM = {"id": 7, "event_type": "item.split_from", "entity_id": "gc:123", "ts": "2026-10-01T10:00:00+00:00",
               "data": {"parent_id": "item:mum", "parent_sku": "MUM", "qty": 1}}
_MUM = {"id": "item:mum", "sku": "MUM", "name": "Mother", "status": "deleted"}


async def test_the_activity_tab_marks_a_deleted_split_parent(ui_client):
    with (
        patch("ui.api_client.get_item_schema", new=AsyncMock(return_value=_SCHEMA)),
        patch("ui.api_client.get_item", new=AsyncMock(return_value={**_ITEM, "split_from": "item:mum"})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})),
        patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={})),
        patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [_SPLIT_FROM], "total": 1})),
        patch("ui.api_client.get_locations", new=AsyncMock(return_value={"items": [], "total": 0})),
        patch("ui.api_client.list_import_batches", new=AsyncMock(return_value={"batches": []})),
        patch("ui.api_client.get_units", new=AsyncMock(return_value=[])),
        patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])),
        _items(_MUM),
    ):
        r = await ui_client.get("/inventory/gc:123?tab=activity", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "MUM [Deleted]" in r.text


async def test_the_full_history_marks_a_deleted_transform_parent(ui_client):
    event = {**_SPLIT_FROM, "event_type": "item.transformed_from"}
    with (
        patch("ui.api_client.get_item", new=AsyncMock(return_value={**_ITEM, "transformed_from": "item:mum"})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.get_category_labels", new=AsyncMock(return_value={})),
        patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": [event], "total": 1})),
        _items(_MUM),
    ):
        r = await ui_client.get("/inventory/gc:123/history", cookies=_authed())
    assert r.status_code == 200, r.text
    assert "MUM [Deleted]" in r.text
