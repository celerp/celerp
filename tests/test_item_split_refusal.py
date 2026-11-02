"""The item's Split card says why it cannot split: the server's refusal (nothing left,
item gone) or a one-piece item, and the Allow Splitting hint only when splitting is off."""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

import ui.api_client as api
from ui.i18n import t
from ui.routes.inventory import _advanced_panel, _item_split_preview

_ITEM = {"sku": "1000", "name": "Parcel", "quantity": 0, "sell_by": "piece",
         "allow_splitting": True, "status": "available"}


@pytest.mark.asyncio
async def test_a_refused_preview_keeps_the_server_reason(monkeypatch):
    reason = t("inventory.err_split_nothing_left")

    async def _refuse(token, entity_id, child_sku=None):
        raise api.APIError(422, reason)

    monkeypatch.setattr(api, "split_preview", _refuse)
    preview = await _item_split_preview("tok", "item:p")
    html = to_xml(_advanced_panel("item:p", _ITEM, preview))
    assert reason in html
    assert t("inv.splitting_disabled_hint") not in html


def test_a_one_piece_item_says_it_is_one_piece():
    html = to_xml(_advanced_panel("item:p", {**_ITEM, "quantity": 1}, {"cannot_split": True}))
    assert t("inventory.cannot_split_one_piece") in html
    assert t("inv.splitting_disabled_hint") not in html


def test_the_allow_splitting_hint_shows_only_when_splitting_is_off():
    html = to_xml(_advanced_panel("item:p", {**_ITEM, "allow_splitting": False}, {"cannot_split": True}))
    assert t("inv.splitting_disabled_hint") in html
