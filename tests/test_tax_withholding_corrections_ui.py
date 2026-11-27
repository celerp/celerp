# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes > Withholding corrections: the bills waiting for their correction are ticked
and posted on one date with the bulk toolbar, the corrected bills are ticked and undone the same
way, and a refused bill is named while the rest go through."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

from ui.api_client import APIError

_URL = "/settings/taxes/withholding-corrections"
_BILL = {"doc_id": "doc:b1", "number": "BILL-7", "contact_name": "Acme", "issue_date": "2026-09-30",
         "withheld": 30.0, "currency": "THB"}


async def _send(ui_client, method: str, url: str, view: dict, data=None, **calls):
    ui_client.cookies.update(_authed())
    stubs = [patch("ui.api_client.withholding_corrections", new=AsyncMock(return_value=view)),
             patch("ui.routes.settings_sales._check_permission", new=AsyncMock(return_value=None)),
             patch("ui.routes.settings_sales._get_company", new=AsyncMock(return_value={"timezone": "UTC"})),
             *[patch(f"ui.api_client.{name}", new=mock) for name, mock in calls.items()]]
    for stub in stubs:
        stub.start()
    try:
        return await ui_client.request(method, url, data=data)
    finally:
        for stub in stubs:
            stub.stop()


async def test_page_lists_waiting_and_corrected_bills_with_their_bulk_actions(ui_client):
    r = await _send(ui_client, "GET", _URL, {"bills": [_BILL], "corrected": [{**_BILL, "doc_id": "doc:b2"}]})
    assert r.status_code == 200
    assert "BILL-7" in r.text and 'value="doc:b1"' in r.text and 'value="doc:b2"' in r.text
    assert f"{_URL}/post" in r.text and f"{_URL}/undo" in r.text and 'type="date"' in r.text


async def test_post_sends_each_ticked_bill_on_the_chosen_date_and_names_a_refusal(ui_client):
    refused = APIError(422, "BILL-8 has no withholding correction to post.")
    correct = AsyncMock(side_effect=[{"entry_id": "je:1"}, refused])
    r = await _send(ui_client, "POST", f"{_URL}/post", {"bills": [], "corrected": [_BILL]},
                    data={"selected": ["doc:b1", "doc:b3"], "date": "2026-10-15"}, withholding_correct=correct)
    assert [c.args[1:] for c in correct.await_args_list] == [("doc:b1", "2026-10-15"), ("doc:b3", "2026-10-15")]
    assert "BILL-8 has no withholding correction to post." in r.text and "No bills are waiting" in r.text


async def test_undo_reverses_each_ticked_bill(ui_client):
    undo = AsyncMock(return_value={"ok": True})
    await _send(ui_client, "POST", f"{_URL}/undo", {"bills": [_BILL], "corrected": []},
                data={"selected": ["doc:b2"]}, withholding_correction_undo=undo)
    assert [c.args[1] for c in undo.await_args_list] == ["doc:b2"]


async def test_page_keeps_the_permission_refusal(ui_client):
    from starlette.responses import RedirectResponse

    ui_client.cookies.update(_authed())
    refused = AsyncMock(return_value=RedirectResponse("/settings/general", status_code=303))
    view = AsyncMock(return_value={"bills": [], "corrected": []})
    with patch("ui.routes.settings_sales._check_permission", new=refused), \
         patch("ui.api_client.withholding_corrections", new=view):
        r = await ui_client.get(_URL, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/general"
    view.assert_not_awaited()


async def test_page_offers_the_company_business_day_as_the_posting_date(ui_client):
    from celerp.services.business_time import business_today

    ui_client.cookies.update(_authed())
    with patch("ui.routes.settings_sales._get_company", new=AsyncMock(return_value={"timezone": "Pacific/Kiritimati"})), \
         patch("ui.routes.settings_sales._check_permission", new=AsyncMock(return_value=None)), \
         patch("ui.api_client.withholding_corrections", new=AsyncMock(return_value={"bills": [_BILL], "corrected": []})):
        r = await ui_client.get(_URL)
    assert f'value="{business_today("Pacific/Kiritimati").isoformat()}"' in r.text
