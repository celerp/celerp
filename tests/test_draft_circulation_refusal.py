# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Moving a draft lot is refused the same way wherever it is tried: 409, naming the lot and
telling the user to make it available first. A draft is not stock yet."""
from __future__ import annotations

import pytest

from mfg_runs import issue, product, receive, run
from test_cost_restatement import _item
from test_helpers import merge_items


async def _draft(client, auth, lot: str) -> None:
    r = await client.post("/items/bulk/revert-to-draft", headers=auth["headers"], json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text


def _refused_as_draft(r, sku: str) -> None:
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "item.draft" and detail["params"] == {"sku": sku}, detail
    assert detail["message"] == f"{sku} is a draft, not stock yet: make it available first."


async def _sku(client, auth, lot: str) -> str:
    return (await client.get(f"/items/{lot}", headers=auth["headers"])).json()["sku"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reserve", "expire", "merge"])
async def test_an_inventory_movement_of_a_draft_is_refused(client, session, auth, action):
    h = auth["headers"]
    lot, other = await _item(client, auth, 10.0, qty=3), await _item(client, auth, 10.0, qty=3)
    await _draft(client, auth, lot)
    if action == "reserve":
        r = await client.post(f"/items/{lot}/reserve", headers=h, json={"quantity": 1})
    elif action == "expire":
        r = await client.post(f"/items/{lot}/expire", headers=h)
    else:
        r = await merge_items(client, headers=h, json={"source_entity_ids": [other, lot], "target_sku_from": other})
    _refused_as_draft(r, await _sku(client, auth, lot))


@pytest.mark.asyncio
async def test_issuing_a_draft_component_is_refused(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    order = await run(client, auth, await product(client, auth, [(raw, 5)]), 1)
    await _draft(client, auth, raw)
    _refused_as_draft(await issue(client, auth, order, [(raw, 5)]), await _sku(client, auth, raw))


@pytest.mark.asyncio
async def test_building_or_receiving_into_a_draft_product_is_refused(client, session, auth):
    raw = await _item(client, auth, 100.0, qty=10)
    item = await product(client, auth, [(raw, 5)])
    order = await run(client, auth, item, 1)
    assert (await issue(client, auth, order, [(raw, 5)])).status_code == 200
    await _draft(client, auth, item)
    sku = await _sku(client, auth, item)
    _refused_as_draft(await receive(client, auth, order, 1), sku)
    _refused_as_draft(await client.post(f"/manufacturing/items/{item}/build", headers=auth["headers"],
                                        json={"quantity": 1}), sku)


def test_the_draft_refusal_is_shown_in_the_users_language():
    import json
    from pathlib import Path

    from ui import i18n

    locales = Path(__file__).resolve().parents[1] / "ui" / "locales"
    for path in locales.glob("*.json"):
        assert "{sku}" in json.loads(path.read_text(encoding="utf-8")).get("item.draft", ""), path.stem
    i18n.set_lang("de")
    try:
        assert i18n.refusal_text({"message": "x", "message_key": "item.draft", "params": {"sku": "RAW-1"}}) == (
            "RAW-1 ist ein Entwurf und noch kein Bestand: Machen Sie es zuerst verfügbar.")
    finally:
        i18n.set_lang("en")
