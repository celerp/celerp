# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Extras on screen and for the assistant: the recipe editor's "Also produces" card, the
quantity inputs in a run's own row, what Complete sends, and the tool schemas."""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml

from test_ui import _authed, _mock_get_company, ui_client  # noqa: F401  (fixtures)
from ui import i18n
from ui.routes.inventory import _production_block, _recipe_api_payload, _recipe_section

CAKE = {"item_id": "item:cake", "sku": "CAKE", "unit": "kg", "quantity": 20.0, "cost_share": 20.0}
ISSUED = [{"item_id": "item:seed", "sku": "SEED", "quantity": 30, "issued_qty": 30.0}]
RECIPE = {"output_qty": 1, "components": [{"item_id": "item:seed", "sku": "SEED", "quantity": 3, "unit": "kg"}],
          "labor": [], "overhead": [],
          "also_produces": [{"item_id": "item:cake", "sku": "CAKE", "unit": "kg", "quantity": 2, "cost_share": 20,
                             "unit_cost": 0.6}]}


@pytest.fixture(autouse=True)
def _lang():
    yield
    i18n.set_lang("en")


def _block(*runs: dict) -> str:
    return to_xml(_production_block("item:oil", {"id": "item:oil"}, {"runs": list(runs)}, "$"))


def _row(html: str, run_id: str) -> str:
    start = html.index(f'data-run-id="{run_id}"') if f'data-run-id="{run_id}"' in html else html.index(run_id)
    row_start = html.rindex("<tr", 0, start)
    return html[row_start:html.index("</tr>", start)]


def test_recipe_api_payload_keeps_extras():
    """UI recipe edits send the stored extras back (no derived cost), and no key without them."""
    payload = _recipe_api_payload(RECIPE)
    assert payload["also_produces"] == [{"item_id": "item:cake", "sku": "CAKE", "unit": "kg", "quantity": 2.0,
                                         "cost_share": 20.0}]
    assert "also_produces" not in _recipe_api_payload({k: v for k, v in RECIPE.items() if k != "also_produces"})


def test_recipe_editor_extras():
    """The card lists each extra with its quantity and share, offers a picker to add one and a
    remove per row, and hints the main product's share."""
    item = {"id": "item:oil", "sku": "OIL", "recipe": RECIPE}
    items = [{"id": "item:seed", "sku": "SEED"}, {"id": "item:cake", "sku": "CAKE"}, {"id": "item:x", "sku": "X"}]
    html = to_xml(_recipe_section("item:oil", item, items, "USD"))
    assert "Also produces" in html
    assert "CAKE" in html and "action=add_also" in html and "action=remove_also" in html
    assert "Main product: 80%" in html
    empty = to_xml(_recipe_section("item:oil", {"id": "item:oil", "recipe": {**RECIPE, "also_produces": []}}, items,
                                   "USD"))
    assert "Also produces" in empty and "Main product: 100%" in empty


@pytest.mark.parametrize("action, query, form, expect", [
    ("add", "action=add_also", {"also_new": "item:x"}, ["item:cake", "item:x"]),
    ("remove", "action=remove_also&index=0", {}, []),
    ("clear", "action=clear", {}, []),
    ("component", "action=add_component", {"comp_new": "item:x"}, ["item:cake"]),
])
async def test_recipe_editor_extras_edits(ui_client, action, query, form, expect):
    ui_client.cookies.update(_authed())
    with (patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "item:oil", "recipe": copy.deepcopy(RECIPE)})),
          patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "USD"})),
          patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": []})),
          patch("ui.api_client.set_item_recipe", new=AsyncMock(return_value={})) as put):
        r = await ui_client.post(f"/api/items/item:oil/recipe-section?{query}", data=form)
    assert r.status_code == 200, r.text
    sent = put.await_args.args[2]
    assert [x["item_id"] for x in sent.get("also_produces", [])] == expect


def test_block_extras_in_row():
    """An open run's extras are number inputs inside its own row's actions cell, prefilled with
    what is still to come; a completed run shows them read-only; no other row is added."""
    open_run = {"id": "mfg:1", "status": "in_progress", "inputs": ISSUED, "also_produces": [CAKE], "received_qty": 0}
    done = {"id": "mfg:2", "status": "completed", "inputs": ISSUED, "extras_completed": True,
            "also_produces": [CAKE], "actual_outputs": [{"item_id": "item:cake", "sku": "CAKE", "quantity": 18.0}]}
    plain = {"id": "mfg:3", "status": "in_progress", "inputs": ISSUED}
    html = _block(open_run, done, plain)
    rows = html.count('class="data-row')
    assert rows == 3, html
    row = _row(html, "mfg:1")
    assert 'name="also:item:cake"' in row and 'value="20' in row and "<label" in row
    assert 'hx-include="closest tr"' in row
    assert "CAKE 18" in _row(html, "mfg:2") and 'name="also:' not in _row(html, "mfg:2")
    assert 'name="also:' not in _row(html, "mfg:3")
    # Two runs with the same extra keep their own inputs.
    twin = {**open_run, "id": "mfg:4"}
    html = _block(open_run, twin)
    assert _row(html, "mfg:1").count('name="also:item:cake"') == 1
    assert _row(html, "mfg:4").count('name="also:item:cake"') == 1


def _hub():
    return (patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "item:oil", "sku": "OIL"})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "USD"})),
            patch("ui.api_client.manufacturing_item_hub", new=AsyncMock(return_value={})))


@pytest.mark.parametrize("form, sent", [
    ({"also:item:cake": "18"}, {"also_produces": [{"item_id": "item:cake", "quantity": 18.0}]}),
    ({}, {"also_produces": []}),
])
async def test_block_complete_posts_extras(ui_client, form, sent):
    a, b, c = _hub()
    ui_client.cookies.update(_authed())
    with a, b, c, patch("ui.api_client.complete_mfg_order", new=AsyncMock(return_value={})) as op:
        r = await ui_client.post("/api/items/item:oil/runs/mfg:1/act",
                                 data={"action": "complete", "idempotency_key": "k", **form})
    assert r.status_code == 200, r.text
    assert op.await_args.args[2] == sent or op.await_args.kwargs.get("data") == sent


@pytest.mark.parametrize("value", ["", "lots"])
async def test_block_bad_extra_flashes(ui_client, value):
    a, b, c = _hub()
    ui_client.cookies.update(_authed())
    with a, b, c, patch("ui.api_client.complete_mfg_order", new=AsyncMock(return_value={})) as op:
        r = await ui_client.post("/api/items/item:oil/runs/mfg:1/act",
                                 data={"action": "complete", "idempotency_key": "k", "also:item:cake": value})
    assert r.status_code == 200, r.text
    assert op.await_count == 0 and "flash--error" in r.text


_MANUFACTURING = {"name": "celerp-manufacturing", "api_routes": "celerp_manufacturing.routes", "first_party": True}


def test_ai_tools_extras(monkeypatch):
    """The recipe, create and complete tools offer also_produces."""
    from celerp.ai import tools as ai_tools
    from celerp.main import app

    monkeypatch.setattr(ai_tools, "loaded_modules", lambda: [_MANUFACTURING])
    caps = ai_tools.compile_agent_capabilities(app, {})
    wanted = {("PUT", "/manufacturing/items/{item_id}/recipe"), ("POST", "/manufacturing"),
              ("POST", "/manufacturing/{order_id}/complete")}
    seen = set()
    for cap in caps.values():
        key = (cap["method"], cap["path"])
        if key in wanted:
            seen.add(key)
            assert "also_produces" in str(cap), key
    assert seen == wanted
