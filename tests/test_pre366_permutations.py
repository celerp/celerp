# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One company an older release left with everything at once (``pre366`` company ``mixed``):
BOM history from before recipes, a run issued more of a component than was on hand and a
component it did not list, expecting two products, and marked received without making a lot;
and a run built from a recipe, issued in full, that the first one's books hold back.

After the first start of this release the first run is unwound (its receipt discarded, what
left the shelf returned, cancelled) and the second, no longer held back, goes on to make its
product, with the stock and the books agreeing after every step.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import pre366
from test_pre366_runs import _books, _held, _items, _new_item, _post, _row, _state, _stock

pytestmark = pytest.mark.asyncio


async def test_an_older_company_with_everything_at_once_unwinds_and_settles(client, session):
    old = await pre366.upgraded(session, "mixed")
    H, J, K = (old["items"][k] for k in "HJK")
    tangle = await _state(session, old, "tangle")
    assert tangle["wip_unresolved"] == "received before tracking", tangle
    assert (await _state(session, old, "recipe"))["wip_unresolved"] == "books disagree"
    # Until they settle, the inventory accounts still carry what left the shelf for them,
    # and the balance sheet counts it as in production: 2 of H, 1 of J, 2 of K.
    from celerp_manufacturing.movements import legacy_in_production
    assert await legacy_in_production(session=session, company_id=old["company_id"]) == Decimal("15")

    # Reconciling it would record what its history shows left the shelf: 2 of H (5 asked for)
    # and the 1 of J it never listed.
    needs = (await client.get(f"/manufacturing/{old['runs']['tangle']}/reconcile", headers=old["headers"])).json()
    assert needs["unlotted"] == 1.0, needs
    assert {c["item_id"]: c["quantity"] for c in needs["components"]} == {H: 2.0, J: 1.0}, needs

    # Two products expected: it cannot go ahead or be given one.
    product = await _new_item(client, old, "GEN-1")
    r = await _post(client, old, "tangle", "repair-output", {"output_item_id": product})
    assert r.json()["detail"]["message_key"] == "mfg.output_shape", r.text
    r = await _post(client, old, "tangle", "issue")
    assert r.json()["detail"]["message_key"] == "mfg.output_shape", r.text

    items = await _items(session, old)
    r = await _post(client, old, "tangle", "repair-output", {})
    assert r.status_code == 200 and r.json()["discarded"] == 1.0, r.text
    assert await _items(session, old) == items  # no lot made from the receipt it recorded
    tangle, recipe = await _state(session, old, "tangle"), await _state(session, old, "recipe")
    assert _held(tangle, H) == (2.0, Decimal("8")) and _held(tangle, J) == (1.0, Decimal("3")), tangle
    assert Decimal(tangle["wip_issued"]) == Decimal("11") and not tangle.get("wip_unresolved"), tangle
    assert _held(recipe, K) == (2.0, Decimal("4")) and not recipe.get("wip_unresolved"), recipe
    await _books(session, old)

    h, j = await _stock(session, old, "H"), await _stock(session, old, "J")
    r = await _post(client, old, "tangle", "return")
    assert r.status_code == 200, r.text
    assert await _stock(session, old, "H") == (h[0] + 2, h[1] + Decimal("8"))
    assert await _stock(session, old, "J") == (j[0] + 1, j[1] + Decimal("3"))
    r = await _post(client, old, "tangle", "cancel", {"reason": "unwound"})
    assert r.status_code == 200, r.text
    await _books(session, old)

    r = await _post(client, old, "recipe", "receive")
    assert r.status_code == 200, r.text
    assert (await _state(session, old, "recipe"))["status"] == "completed"
    made = (await _row(session, old, r.json()["lot_item_id"])).state
    assert (made["parent_item_id"], made["quantity"], Decimal(str(made["cost_total"]))) == (
        old["items"]["FG"], 1.0, Decimal("4")), made
    await _books(session, old)
    for k in ("BOM_KEPT", "BOM_DROPPED"):  # BOM history untouched by all of it
        assert (await _row(session, old, old["items"][k])).state["entity_type"] == "bom", k


async def test_reconciling_the_held_back_run_first_records_what_its_history_holds(client, session):
    old = await pre366.upgraded(session, "mixed")
    H, J, K = (old["items"][k] for k in "HJK")
    r = await client.get(f"/manufacturing/{old['runs']['recipe']}/reconcile", headers=old["headers"])
    assert r.json()["components"][0]["quantity"] == 2.0, r.text

    from celerp.accounting_roles import LOT_ACCOUNT_FIELD

    account = (await _row(session, old, K)).state[LOT_ACCOUNT_FIELD]
    r = await client.post(f"/manufacturing/{old['runs']['recipe']}/reconcile", headers=old["headers"],
                          json={"components": [{"item_id": K, "value": "4"}], "account": account})
    assert r.status_code == 200, r.text
    assert r.json()["components"] == [{"item_id": K, "quantity": 2.0, "value": "4.00"}], r.text
    recipe = await _state(session, old, "recipe")
    assert _held(recipe, K) == (2.0, Decimal("4")) and not recipe.get("wip_unresolved"), recipe

    r = await _post(client, old, "tangle", "repair-output", {})
    assert r.status_code == 200, r.text
    tangle = await _state(session, old, "tangle")
    assert _held(tangle, H) == (2.0, Decimal("8")) and _held(tangle, J) == (1.0, Decimal("3")), tangle
    await _books(session, old)
