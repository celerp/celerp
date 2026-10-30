# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill refused going back to its order because its freight became part of a goods cost says
why for each item, in the user's language, in a toast that stays, with a link to each item's
page, where its merge or transform is undone."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from celerp.accounting_roles import refusal
from ui.api_client import APIError

_LOTS = [refusal("docs.revert_folded_merged", "SKU 'RAW-1' was merged, or made into another product, without its "
                 "freight following it.", sku="RAW-1", id="item:a"),
         refusal("docs.revert_folded_counted", "SKU 'RAW-2' went down to zero while it held the freight and came "
                 "back up.", sku="RAW-2", id="item:b")]
_REFUSED = refusal("docs.revert_lineage_folded", "This bill cannot go back to its order.", lots=_LOTS, runs=[])


async def _revert(ui_client, refused: dict) -> dict:  # noqa: F811
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.revert_doc_to_draft", new=AsyncMock(side_effect=APIError(409, "", refused))):
        r = await ui_client.post("/docs/doc:1/action/revert_to_draft", content=b"reason=",
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200, r.text
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


async def test_each_item_named_links_to_its_page(ui_client):  # noqa: F811
    toast = await _revert(ui_client, _REFUSED)
    assert toast["persist"] is True
    assert toast["action"] == [{"href": "/inventory/item:a", "label": "RAW-1"},
                               {"href": "/inventory/item:b", "label": "RAW-2"}]
    for reason in ("SKU 'RAW-1' was merged", "SKU 'RAW-2' went down to zero"):
        assert reason in toast["message"], toast
    assert toast["message"].startswith("This bill cannot go back to its order: part of its freight"), toast


async def test_another_refusal_keeps_its_plain_toast(ui_client):  # noqa: F811
    toast = await _revert(ui_client, refusal("docs.revert_locked", "The period is locked."))
    assert "action" not in toast and "persist" not in toast
