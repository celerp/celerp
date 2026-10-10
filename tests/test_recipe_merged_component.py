# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A recipe naming an item that was merged into another.

The merged-away item holds no stock of its own any more. Saving a recipe that names it is
refused, and a run whose recipe was saved before the merge is refused at Issue; both say
which item it was merged into, so the user knows what to name instead.
"""
from __future__ import annotations

import pytest

from mfg_runs import issue, product, refusal, run, snapshot
from test_cost_restatement import _item, _merge, _state

pytestmark = pytest.mark.asyncio


async def _merged_pair(client, session, auth):
    a, b = await _item(client, auth, 10.0, qty=5), await _item(client, auth, 10.0, qty=5)
    into_sku = (await _state(session, auth, b))["sku"]
    return a, b, into_sku


async def test_saving_a_recipe_that_names_a_merged_item_is_refused_naming_where_it_went(client, session, auth):
    a, b, into_sku = await _merged_pair(client, session, auth)
    await _merge(client, auth, [a, b], target_sku_from=b)
    fg = await _item(client, auth, 0.0, qty=0)

    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": a, "quantity": 1}], "labor": [], "overhead": []})

    detail = refusal(r, 422, "component_merged")
    assert detail["params"]["merged_into"] == into_sku
    assert "recipe" not in await _state(session, auth, fg)


async def test_a_run_whose_component_was_merged_since_is_refused_naming_where_it_went(client, session, auth):
    a, b, into_sku = await _merged_pair(client, session, auth)
    order = await run(client, auth, await product(client, auth, [(a, 1)]), 1)
    await _merge(client, auth, [a, b], target_sku_from=b)
    before = await snapshot(session, auth, a, order)

    detail = refusal(await issue(client, auth, order, key="merged"), 409, "component_merged")

    assert detail["params"]["merged_into"] == into_sku
    assert await snapshot(session, auth, a, order) == before
