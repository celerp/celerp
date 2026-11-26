# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes edits a code's rate in force, never a bare rate: the Rate cell shows the
rate on today's date and editing it changes the open rate version only. Every edit sends the
fingerprint of the list the page showed, so a list someone else changed since the page loaded
is refused with a message in the cell instead of being overwritten."""
from __future__ import annotations

import copy
import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest

from celerp.services.business_time import business_today

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_PREFIXES = [("taxes", "get_taxes", "patch_taxes"),
             ("purchasing-taxes", "get_purchasing_taxes", "patch_purchasing_taxes")]
_STALE = "Taxes changed since this page loaded; reload the page"


@pytest.fixture(autouse=True)
def _company():
    """A rate edit changes the version in force on the company's today, which the handler reads
    from the company."""
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"current_role": "owner", "settings": {}})):
        yield


def _rows() -> list[dict]:
    return [{"id": "u-0123456789ab", "name": "VAT", "tax_type": "both", "category": "standard",
             "versions": [{"rate": 5.0, "valid_from": None, "valid_to": "2020-12-31"},
                          {"rate": 7.0, "valid_from": "2021-01-01", "valid_to": None}],
             "is_default": True, "is_compound": False, "description": ""}]


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_tax_cell_edit_sends_rendered_fingerprint(ui_client, prefix, gname, pname):
    from fasthtml.common import to_xml
    from celerp.localization.taxcodes import TaxCode, list_fingerprint, rate_on
    from ui.routes.settings import _taxes_tab

    rendered = to_xml(_taxes_tab(_rows(), dt.date.today(), prefix=prefix))
    shown = list_fingerprint(_rows())
    assert 'name="fingerprint"' in rendered and f'value="{shown}"' in rendered
    today_rate = rate_on(TaxCode(**{k: v for k, v in _rows()[0].items() if k != "id"}), dt.date.today())
    assert f">{today_rate:g}%</span>" in rendered

    ui_client.cookies.update(_authed())
    stored = _rows()
    stored[0]["versions"][1]["rate"] = 8.0
    saved = AsyncMock(return_value={})
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=[_rows(), stored])), \
         patch(f"ui.api_client.{pname}", new=saved):
        r = await ui_client.patch(f"/settings/{prefix}/0/rate", data={"value": "8", "fingerprint": shown})
    assert r.status_code == 200, r.text
    assert ">8%</span>" in r.text
    token, taxes, fingerprint = saved.await_args.args
    assert fingerprint == shown
    assert "rate" not in taxes[0]
    assert taxes[0]["versions"] == stored[0]["versions"]

    # The fingerprint the page rendered goes up, not one computed from a fresh read
    moved = copy.deepcopy(_rows())
    moved[0]["description"] = "Changed by someone else"
    refused = AsyncMock(side_effect=APIError(409, _STALE))
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=moved)), \
         patch(f"ui.api_client.{pname}", new=refused):
        r = await ui_client.patch(f"/settings/{prefix}/0/rate", data={"value": "9", "fingerprint": shown})
    assert refused.await_args.args[2] == shown != list_fingerprint(moved)
    assert "cell-error" in r.text and _STALE in r.text

    added = AsyncMock(return_value={})
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=_rows())), \
         patch(f"ui.api_client.{pname}", new=added):
        r = await ui_client.post(f"/settings/{prefix}/new", data={"fingerprint": shown})
    assert r.status_code == 204, r.text
    _, taxes, fingerprint = added.await_args.args
    assert fingerprint == shown
    new_row = taxes[-1]
    assert "rate" not in new_row and new_row["versions"] == [{"rate": 0.0}]


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_rate_versions_listed_under_each_code(prefix, gname, pname):
    """Each code shows its rate versions under its row, each date and rate a cell to edit, with
    Add rate version and a Remove per version sending the rendered fingerprint."""
    from fasthtml.common import to_xml
    from celerp.localization.taxcodes import list_fingerprint
    from ui.routes.settings import _taxes_tab

    rendered = to_xml(_taxes_tab(_rows(), dt.date.today(), prefix=prefix))
    assert "Rate versions (2)" in rendered and "Add rate version" in rendered
    assert f'hx-get="/settings/{prefix}/0/versions/1/valid_from/edit"' in rendered
    assert ">2021-01-01</span>" in rendered and ">2020-12-31</span>" in rendered
    assert f'hx-post="/settings/{prefix}/0/versions"' in rendered
    assert f'hx-delete="/settings/{prefix}/0/versions/0"' in rendered
    assert "Remove the 5% version of VAT?" in rendered
    assert rendered.count(f'hx-include="#{prefix}-fingerprint"') >= 4
    assert f'value="{list_fingerprint(_rows())}"' in rendered


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_add_rate_version_starts_today_and_closes_the_open_one(ui_client, prefix, gname, pname):
    """A version added by hand starts on the company's today at the last version's rate, so it is
    in force the same day; the open version before it now ends the day before."""
    from celerp.localization.taxcodes import list_fingerprint

    ui_client.cookies.update(_authed())
    today = business_today(None)  # the company has no timezone set: the default business timezone
    saved = AsyncMock(return_value={})
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=_rows())), \
         patch(f"ui.api_client.{pname}", new=saved):
        r = await ui_client.post(f"/settings/{prefix}/0/versions", data={"fingerprint": list_fingerprint(_rows())})
    assert r.status_code == 204, r.text
    _, taxes, fingerprint = saved.await_args.args
    assert fingerprint == list_fingerprint(_rows())
    assert taxes[0]["versions"][1]["valid_to"] == (today - dt.timedelta(days=1)).isoformat()
    assert taxes[0]["versions"][2] == {"rate": 7.0, "valid_from": today.isoformat()}


async def test_added_version_starts_after_a_later_last_version():
    """A last version that starts or ends after today pushes the new one to the day after it."""
    from ui.routes.settings import _append_version

    today = dt.date(2026, 3, 1)
    ending = [{"rate": 7.0, "valid_from": None, "valid_to": "2026-06-30"}]
    _append_version(ending, today)
    assert ending == [{"rate": 7.0, "valid_from": None, "valid_to": "2026-06-30"},
                      {"rate": 7.0, "valid_from": "2026-07-01"}]
    starting = [{"rate": 7.0, "valid_from": "2026-03-01", "valid_to": None}]
    _append_version(starting, today)
    assert starting == [{"rate": 7.0, "valid_from": "2026-03-01", "valid_to": "2026-03-01"},
                        {"rate": 7.0, "valid_from": "2026-03-02"}]


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_version_cell_edit_saves_that_version(ui_client, prefix, gname, pname):
    """Editing a version's dates or rate changes that version only and sends the rendered
    fingerprint; the save also refreshes the rate in force shown in the row above. An empty
    date clears it; a date that is not one is refused in the cell without a save."""
    from celerp.localization.taxcodes import list_fingerprint

    ui_client.cookies.update(_authed())
    shown = list_fingerprint(_rows())
    stored = _rows()
    stored[0]["versions"][1]["rate"] = 9.0
    saved = AsyncMock(return_value={})
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=[_rows(), stored])), \
         patch(f"ui.api_client.{pname}", new=saved):
        r = await ui_client.patch(f"/settings/{prefix}/0/versions/1/rate", data={"value": "9", "fingerprint": shown})
    assert r.status_code == 200, r.text
    _, taxes, fingerprint = saved.await_args.args
    assert fingerprint == shown
    assert taxes[0]["versions"] == stored[0]["versions"]
    assert f'id="{prefix}-0-rate"' in r.text and 'hx-swap-oob="true"' in r.text
    assert r.text.count(">9%</span>") == 2

    saved = AsyncMock(return_value={})
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=_rows())), \
         patch(f"ui.api_client.{pname}", new=saved):
        await ui_client.patch(f"/settings/{prefix}/0/versions/0/valid_to", data={"value": "2020-06-30",
                                                                               "fingerprint": shown})
        assert saved.await_args.args[1][0]["versions"][0]["valid_to"] == "2020-06-30"
        await ui_client.patch(f"/settings/{prefix}/0/versions/1/valid_to", data={"value": "", "fingerprint": shown})
        assert saved.await_args.args[1][0]["versions"][1]["valid_to"] is None
        saved.reset_mock()
        r = await ui_client.patch(f"/settings/{prefix}/0/versions/1/valid_from", data={"value": "31/12/2020",
                                                                                      "fingerprint": shown})
    assert "cell-error" in r.text and "31/12/2020" in r.text
    saved.assert_not_awaited()

    refused = AsyncMock(side_effect=APIError(409, _STALE))
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=_rows())), \
         patch(f"ui.api_client.{pname}", new=refused):
        r = await ui_client.patch(f"/settings/{prefix}/0/versions/1/rate", data={"value": "9", "fingerprint": shown})
    assert "cell-error" in r.text and _STALE in r.text


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_main_rate_edit_refreshes_its_version_cell(ui_client, prefix, gname, pname):
    """The Rate cell edits the version in force today, so its save also swaps that version's
    cell in the rate versions below."""
    from celerp.localization.taxcodes import list_fingerprint

    ui_client.cookies.update(_authed())
    stored = _rows()
    stored[0]["versions"][1]["rate"] = 8.0
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=[_rows(), stored])), \
         patch(f"ui.api_client.{pname}", new=AsyncMock(return_value={})):
        r = await ui_client.patch(f"/settings/{prefix}/0/rate", data={"value": "8",
                                                                     "fingerprint": list_fingerprint(_rows())})
    assert f'id="{prefix}-0-v1-rate"' in r.text and r.text.count(">8%</span>") == 2


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_remove_rate_version(ui_client, prefix, gname, pname):
    """Remove drops that version and sends the rendered fingerprint (htmx sends a DELETE's values
    in the query string); the server's refusal, such as for a code's last version, comes back."""
    from celerp.localization.taxcodes import list_fingerprint

    ui_client.cookies.update(_authed())
    shown = list_fingerprint(_rows())
    saved = AsyncMock(return_value={})
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=_rows())), \
         patch(f"ui.api_client.{pname}", new=saved):
        r = await ui_client.delete(f"/settings/{prefix}/0/versions/0", params={"fingerprint": shown})
    assert r.status_code == 204, r.text
    _, taxes, fingerprint = saved.await_args.args
    assert fingerprint == shown and taxes[0]["versions"] == _rows()[0]["versions"][1:]

    refused = AsyncMock(side_effect=APIError(422, "VAT: versions: List should have at least 1 item"))
    with patch(f"ui.api_client.{gname}", new=AsyncMock(return_value=_rows())), \
         patch(f"ui.api_client.{pname}", new=refused):
        r = await ui_client.delete(f"/settings/{prefix}/0/versions/1", params={"fingerprint": shown})
    assert r.status_code != 204
