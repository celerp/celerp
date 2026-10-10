# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A recipe that uses its own product, or nests sub-assemblies too deep, is refused in the
user's language, naming the product by its SKU (or "This product" when it has none), never
by an internal id."""

from __future__ import annotations

import pytest

from celerp_manufacturing import costing
from celerp_manufacturing.costing import RecipeError, roll_up_cost
from celerp_manufacturing.expansion import explode_demand
from mfg_runs import product
from test_cost_restatement import _item
from ui.i18n import refusal_text, set_lang

pytestmark = pytest.mark.asyncio


def _in(detail, lang: str) -> str:
    set_lang(lang)
    try:
        return refusal_text(detail)
    finally:
        set_lang("en")


async def _sku(client, auth, item_id: str) -> str:
    r = await client.get(f"/items/{item_id}", headers=auth["headers"])
    return r.json()["sku"]


async def test_a_recipe_using_its_own_product_is_refused_by_sku(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    inner = await product(client, auth, [(raw, 1)])
    outer = await product(client, auth, [(inner, 1)])
    inner_sku = await _sku(client, auth, inner)

    r = await client.put(f"/manufacturing/items/{inner}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": outer, "quantity": 1}], "labor": [], "overhead": []})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    english, german = _in(detail, "en"), _in(detail, "de")
    assert english == f"{inner_sku} is used in its own recipe, directly or through a sub-assembly", english
    assert inner_sku in german and "eigenen Rezept" in german, german
    for text in (english, german, r.text):
        assert inner not in text and outer not in text, text


async def test_a_recipe_nested_too_deep_is_refused_in_the_users_language(client, session, auth, monkeypatch):
    monkeypatch.setattr(costing, "MAX_RECIPE_DEPTH", 2)
    raw = await _item(client, auth, 100.0, qty=10)
    inner = await product(client, auth, [(raw, 1)])
    middle = await product(client, auth, [(inner, 1)])
    upper = await product(client, auth, [(middle, 1)])
    top = await _item(client, auth, 0.0, qty=0)

    r = await client.put(f"/manufacturing/items/{top}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": upper, "quantity": 1}], "labor": [], "overhead": []})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert _in(detail, "en") == "Sub-assemblies are nested more than 2 levels deep", detail
    assert "verschachtelt" in _in(detail, "de"), _in(detail, "de")


def test_a_cycle_through_a_product_without_a_sku_names_it_this_product():
    graph = {"A": {"recipe": {"components": [{"item_id": "B", "quantity": 1}]}},
             "B": {"recipe": {"components": [{"item_id": "A", "quantity": 1}]}}}

    with pytest.raises(RecipeError) as costed:
        roll_up_cost(graph["A"]["recipe"], graph.get, _path=frozenset({"A"}))
    with pytest.raises(RecipeError) as exploded:
        explode_demand([("A", 1)], graph.get)

    assert _in(costed.value.detail, "en") == "This product is used in its own recipe, directly or through a sub-assembly"
    assert _in(costed.value.detail, "de").startswith("Dieses Produkt ")
    assert _in(exploded.value.detail, "de").startswith("Dieses Produkt: Dieses Produkt ")
    for exc in (costed.value, exploded.value):
        assert " A" not in str(exc) and " B" not in str(exc), str(exc)


def test_a_zero_component_without_a_sku_is_never_named_by_its_id():
    with pytest.raises(RecipeError) as refused:
        roll_up_cost({"components": [{"item_id": "item:7f3a", "quantity": 0}]}, {}.get)

    assert "item:7f3a" not in str(refused.value)
    assert "item:7f3a" not in _in(refused.value.detail, "de")
