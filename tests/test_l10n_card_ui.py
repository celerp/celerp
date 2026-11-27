# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes, Update localization: the card's panel previews what the country pack
changes, confirms it with the preview's binding and the page's key (then reloads the page),
undoes it naming the codes kept because they were edited since, and closes empty. A session
the API ended or a refusal shows in the panel."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

from ui.api_client import APIError

_URL = "/settings/sales/localization"


async def _post(ui_client, path: str, data=None, **calls):
    stubs = [patch(f"ui.api_client.{name}", new=mock) for name, mock in calls.items()]
    for stub in stubs:
        stub.start()
    try:
        return await ui_client.post(f"{_URL}/{path}", data=data)
    finally:
        for stub in stubs:
            stub.stop()


async def test_preview_shows_what_the_pack_changes_with_the_binding_to_confirm(ui_client):
    ui_client.cookies.update(_authed())
    preview = AsyncMock(return_value={"diff": {"added": [{"name": "VAT 7%"}]}, "fingerprint": "f1"})
    r = await _post(ui_client, "preview", localization_preview=preview)
    assert r.status_code == 200, r.text
    assert "Adds: VAT 7%" in r.text and 'value="f1"' in r.text and f'hx-post="{_URL}/confirm"' in r.text
    preview.assert_awaited_once()


async def test_confirm_sends_the_binding_and_key_then_reloads_and_refuses_without_a_binding(ui_client):
    ui_client.cookies.update(_authed())
    confirm = AsyncMock(return_value={"operation_id": "op-1"})
    r = await _post(ui_client, "confirm", {"binding": "f1", "idempotency_key": "k1"}, localization_confirm=confirm)
    assert r.status_code == 204 and r.headers.get("HX-Refresh") == "true"
    assert confirm.await_args.args[1:] == ("f1", "k1")

    confirm.reset_mock()
    r = await _post(ui_client, "confirm", {"idempotency_key": "k1"}, localization_confirm=confirm)
    assert "cell-error" in r.text and "Taxes changed since the preview; preview again." in r.text
    confirm.assert_not_awaited()


async def test_undo_reloads_and_names_the_codes_it_kept(ui_client):
    ui_client.cookies.update(_authed())
    undo = AsyncMock(return_value={"kept": [{"name": "VAT 7%"}]})
    r = await _post(ui_client, "undo", {"operation_id": "op-1"}, localization_undo=undo)
    assert r.status_code == 204 and r.headers.get("HX-Refresh") == "true"
    assert undo.await_args.args[1] == "op-1"
    assert "Kept as you changed them: VAT 7%" in json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]

    r = await _post(ui_client, "undo", {"operation_id": "op-1"},
                    localization_undo=AsyncMock(return_value={"kept": []}))
    assert r.status_code == 204 and r.headers.get("HX-Refresh") == "true" and "HX-Trigger" not in r.headers


async def test_panel_shows_a_session_the_api_ended_and_a_refusal(ui_client):
    ui_client.cookies.update(_authed())
    for status, detail in ((401, "expired"), (422, "No pack for this country")):
        r = await _post(ui_client, "preview", localization_preview=AsyncMock(side_effect=APIError(status, detail)))
        assert "cell-error" in r.text
        assert ("You were signed out" if status == 401 else detail) in r.text


async def test_close_empties_the_panel(ui_client):
    ui_client.cookies.update(_authed())
    r = await ui_client.get(f"{_URL}/close")
    assert r.status_code == 200 and r.text == ""
