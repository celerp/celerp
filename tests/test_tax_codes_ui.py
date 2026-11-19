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

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_PREFIXES = [("taxes", "get_taxes", "patch_taxes"),
             ("purchasing-taxes", "get_purchasing_taxes", "patch_purchasing_taxes")]
_STALE = "Taxes changed since this page loaded; reload the page"


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

    rendered = to_xml(_taxes_tab(_rows(), prefix=prefix))
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
