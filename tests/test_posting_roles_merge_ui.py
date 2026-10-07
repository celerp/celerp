# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""How the inventory screens word a merge that moves value between inventory accounts."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.routes.inventory import _merge_reclass_sentence

_TWO = {"destination": "1130-P", "destination_name": "Inventory A", "currency": "USD",
        "moves": [{"account": "1131", "name": "Inventory B", "amount": 250.0},
                  {"account": "1132", "name": "Inventory C", "amount": 75.5}]}


def test_the_sentence_names_every_account_and_amount():
    assert _merge_reclass_sentence(_TWO) == ("These items are held in different inventory accounts. "
                                             "Merging will move $250.00 from Inventory B, $75.50 from Inventory C "
                                             "to Inventory A.")
    assert _merge_reclass_sentence(_TWO, done=True) == ("These items were held in different inventory accounts. "
                                                        "$250.00 from Inventory B, $75.50 from Inventory C "
                                                        "moved to Inventory A.")


def test_a_role_that_cannot_see_cost_is_told_the_accounts_only():
    hidden = {**_TWO, "moves": [{**m, "amount": None} for m in _TWO["moves"]]}
    assert _merge_reclass_sentence(hidden) == ("These items are held in different inventory accounts. Merging will "
                                               "move their value from Inventory B, Inventory C to Inventory A.")
    assert "$" not in _merge_reclass_sentence(hidden, done=True)


def test_a_merge_within_one_account_says_nothing_extra():
    assert _merge_reclass_sentence(None) == ""


@pytest.mark.asyncio
async def test_the_bulk_merge_sends_one_key_and_raises_the_moved_value_as_a_toast(ui_client):
    merge = AsyncMock(return_value={"id": "item:new", "inventory_reclassification": _TWO})
    with (
        patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "item:a", "sku": "A", "quantity": 1})),
        patch("ui.api_client.merge_items", new=merge),
    ):
        r = await ui_client.post(
            "/api/items/bulk/merge",
            content=b"selected=item%3Aa&selected=item%3Ab&target_sku_from=item%3Aa&idempotency_key=merge-k1",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
    assert r.status_code == 200
    assert merge.await_args.kwargs["idempotency_key"] == "merge-k1"
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["celerpToast"]["message"].endswith("$75.50 from Inventory C moved to Inventory A.")
    assert trigger["celerpToast"]["persist"] is True
    assert "celerpSelectionClear" in trigger


@pytest.mark.asyncio
async def test_the_merge_preview_returns_the_sentence(ui_client):
    with patch("ui.api_client.preview_merge", new=AsyncMock(
            return_value={"inventory_reclassification": _TWO, "plan_fingerprint": "fp-1"})):
        r = await ui_client.post("/api/items/merge/preview", data={"selected": ["item:a", "item:b"],
                                                                   "target_sku_from": "item:a"}, cookies=_authed())
    assert r.json() == {"message": _merge_reclass_sentence(_TWO), "plan_fingerprint": "fp-1"}


@pytest.mark.asyncio
async def test_the_merge_preview_asks_for_the_typed_sku(ui_client):
    preview = AsyncMock(return_value={"inventory_reclassification": None, "plan_fingerprint": "fp-2"})
    with patch("ui.api_client.preview_merge", new=preview):
        r = await ui_client.post("/api/items/merge/preview", data={
            "selected": ["item:a", "item:b"], "target_sku_from": "item:a", "resulting_sku": "NEW-1"},
            cookies=_authed())
    assert r.json()["plan_fingerprint"] == "fp-2"
    assert preview.await_args.args[1:] == (["item:a", "item:b"], "item:a", "NEW-1")


@pytest.mark.asyncio
async def test_the_preview_and_the_confirmation_send_the_same_merge():
    from ui import api_client

    sent = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, path, json):
            sent[path] = json
            return httpx.Response(200, json={"plan_fingerprint": "fp"}, request=httpx.Request("POST", path))

    with patch("ui.api_client._api_client", new=lambda token: _Client()):
        await api_client.preview_merge("t", ["item:a", "item:b"], "item:a", "NEW-1")
        await api_client.merge_items("t", ["item:a", "item:b"], "item:a", "fp", "NEW-1", "key-1")
    confirmed = {k: v for k, v in sent["/items/merge"].items() if k not in ("plan_fingerprint", "idempotency_key")}
    assert confirmed == sent["/items/merge/preview"]


_STALE = "The merge or its items changed since it was reviewed. Review the merge again."


@pytest.mark.asyncio
async def test_the_confirmed_merge_carries_the_preview_and_a_stale_one_is_explained(ui_client):
    from ui.api_client import APIError
    merge = AsyncMock(side_effect=APIError(409, _STALE))
    with (
        patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "item:a", "sku": "A", "quantity": 1})),
        patch("ui.api_client.merge_items", new=merge),
    ):
        r = await ui_client.post(
            "/api/items/bulk/merge",
            content=b"selected=item%3Aa&selected=item%3Ab&target_sku_from=item%3Aa&plan_fingerprint=fp-1",
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=_authed(),
        )
    assert merge.await_args.kwargs["plan_fingerprint"] == "fp-1"
    # The quantity is the merge's own sum, never a figure the screen read earlier.
    assert "resulting_quantity" not in merge.await_args.kwargs
    assert json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"] == _STALE


@pytest.mark.asyncio
async def test_an_undo_the_api_refuses_is_explained(ui_client):
    from ui.api_client import APIError
    with patch("ui.api_client.undo_merge", new=AsyncMock(side_effect=APIError(409, "The merged item has changed."))):
        r = await ui_client.post("/api/items/item:new/undo-merge", cookies=_authed())
    assert json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"] == "The merged item has changed."
