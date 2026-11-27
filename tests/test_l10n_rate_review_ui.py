# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes, Review item rates: after an update whose pack moves HS codes to another tax
code, the localization card offers the review; the panel lists each item with its current and
proposed code, posts only the ticked ids with the list's binding, names the items left as they
are because they were edited since, and undoes what the review applied. The update preview names
the HS codes the review will offer."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fasthtml.common import to_xml

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

_OP = "op-1"
_URL = f"/settings/sales/localization/rate-review/{_OP}"
_TAXES = [{"id": "u-gst18", "name": "GST 18%"}, {"id": "u-gst5", "name": "GST 5%"}]


def _proposal(*ids: str) -> dict:
    rows = {"i-1": {"item_id": "i-1", "sku": "TEA-1", "name": "Green tea", "hs_code": "0902.10",
                    "old": ["u-gst18"], "new": ["u-gst5"], "code_name": "GST 5%", "valid_from": "2026-01-01"},
            "i-2": {"item_id": "i-2", "sku": "TEA-2", "name": "Black tea", "hs_code": "0902.30",
                    "old": ["u-gst18"], "new": ["u-gst5"], "code_name": "GST 5%", "valid_from": "2026-01-01"}}
    return {"items": [rows[i] for i in ids], "fingerprint": "r42"}


def _stubs(proposal: dict, **calls):
    return [patch("ui.api_client.rate_review_get", new=AsyncMock(return_value=proposal)),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=_TAXES)),
            patch("ui.api_client.get_item", new=AsyncMock(side_effect=lambda token, i: {"name": f"Item {i}"})),
            *[patch(f"ui.api_client.{name}", new=mock) for name, mock in calls.items()]]


async def _send(ui_client, method: str, url: str, proposal: dict, data=None, **calls):
    ui_client.cookies.update(_authed())
    stubs = _stubs(proposal, **calls)
    for stub in stubs:
        stub.start()
    try:
        return await ui_client.request(method, url, data=data)
    finally:
        for stub in stubs:
            stub.stop()


def test_card_offers_review_only_after_an_update_with_an_item_rate_table():
    from ui.routes.settings import _localization_card
    applied = {"country": "IN", "version": "2026.2", "applied_at": "2026-11-20", "operation_id": _OP}
    assert _URL not in to_xml(_localization_card({"country": "IN", "localization": applied}))
    table = [{"hs_prefix": "0902", "code_id": "u-gst5", "valid_from": "2026-01-01"}]
    card = to_xml(_localization_card({"country": "IN", "localization": {**applied, "item_rate_table": table}}))
    assert f'hx-get="{_URL}"' in card and "Review item rates" in card


def test_preview_names_the_hs_codes_the_review_will_offer():
    from ui.routes.settings import _localization_preview
    rows = [{"hs_prefix": "0902", "code_id": "u-gst5", "valid_from": "2026-01-01"},
            {"hs_prefix": "1006", "code_id": "u-gst5", "valid_from": "2026-01-01"}]
    rendered = to_xml(_localization_preview({"diff": {"item_rates": rows}, "fingerprint": "f"}))
    assert "for you to review after you confirm: 0902, 1006" in rendered


async def test_panel_lists_current_and_proposed_codes_with_the_list_binding(ui_client):
    r = await _send(ui_client, "GET", _URL, _proposal("i-1", "i-2"))
    assert r.status_code == 200, r.text
    for text in ("Green tea", "TEA-2", "0902.10", "GST 18%", "GST 5%", "Apply proposed code"):
        assert text in r.text
    assert "u-gst18" not in r.text
    assert f"{_URL}/apply?binding=r42" in r.text
    assert 'value="i-1"' in r.text and 'value="i-2"' in r.text


async def test_panel_with_nothing_to_review_says_so(ui_client):
    r = await _send(ui_client, "GET", _URL, _proposal())
    assert r.status_code == 200, r.text
    assert "No items need a different tax code." in r.text
    assert "Apply proposed code" not in r.text


async def test_apply_posts_only_the_ticked_ids_and_names_the_items_edited_since(ui_client):
    applied = AsyncMock(return_value={"skipped": ["i-2"]})
    r = await _send(ui_client, "POST", f"{_URL}/apply?binding=r42", _proposal("i-2"),
                    data={"selected": ["i-1", "i-2", "i-1"]}, rate_review_apply=applied)
    assert r.status_code == 200, r.text
    assert applied.await_args.args[1:] == (_OP, ["i-1", "i-2"], "r42")
    assert "edited after this list opened: Item i-2" in r.text


async def test_apply_refusal_shows_in_the_panel(ui_client):
    from ui.api_client import APIError
    refused = AsyncMock(side_effect=APIError(422, "This item list is out of date. Open Review item rates again."))
    r = await _send(ui_client, "POST", f"{_URL}/apply?binding=r1", _proposal("i-1"),
                    data={"selected": ["i-1"]}, rate_review_apply=refused)
    assert r.status_code == 200, r.text
    assert "out of date" in r.text and "cell-error" in r.text


async def test_undo_reshows_the_list_and_names_the_items_kept(ui_client):
    undone = AsyncMock(return_value={"kept": ["i-1"]})
    r = await _send(ui_client, "POST", f"{_URL}/undo", _proposal("i-2"), rate_review_undo=undone)
    assert r.status_code == 200, r.text
    assert undone.await_args.args[1:] == (_OP,)
    assert "Kept as you changed them: Item i-1" in r.text and "Black tea" in r.text


async def test_cancel_empties_the_panel(ui_client):
    """The panel's Cancel swaps the panel for nothing: an empty answer, not a page or an error."""
    ui_client.cookies.update(_authed())
    r = await ui_client.get("/settings/sales/localization/close")
    assert r.status_code == 200 and r.text == ""
