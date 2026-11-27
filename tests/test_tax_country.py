# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company names its tax country at setup or in Settings > General, and Update localization
adds that country's tax rates to its list.

Applying a pack adds the codes the list lacks and keeps every rate the company set itself.
A location's country is part of its address only: saving the address text or the country
keeps the other, and neither touches the company's taxes.
"""
from __future__ import annotations

import re
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from celerp.localization import lock_codes
from celerp.tax_regimes import get_regime, tax_name_key
from test_helpers import make_test_token, new_rows, rate_today, save_taxes

pytestmark = pytest.mark.asyncio


async def _taxes(client, auth) -> list[dict]:
    r = await client.get("/companies/me/taxes", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _names(taxes: list[dict]) -> list[str]:
    return [tax["name"] for tax in taxes]


async def _update_localization(client, auth, country: str) -> dict:
    """Name the company's country, then preview and confirm Update localization; returns the diff."""
    r = await client.patch("/companies/me", headers=auth["headers"], json={"settings": {"country": country}})
    assert r.status_code == 200, r.text
    r = await client.post("/settings/taxes/localization/preview", headers=auth["headers"])
    assert r.status_code == 200, r.text
    preview = r.json()
    r = await client.post("/settings/taxes/localization/confirm", headers=auth["headers"],
                          json={"binding": preview["fingerprint"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return preview["diff"]


async def test_a_later_country_adds_its_taxes_and_keeps_the_first_countrys(client, auth):
    await _update_localization(client, auth, "TH")
    await _update_localization(client, auth, "IN")
    names = {tax_name_key(name) for name in _names(await _taxes(client, auth))}
    for country in ("TH", "IN"):
        assert {tax_name_key(row["name"]) for row in get_regime(country)["taxes"]} <= names


async def test_a_company_with_its_own_rate_keeps_it_and_the_preview_names_it(client, auth):
    own = new_rows(get_regime("TH")["taxes"])
    own[0]["versions"] = [{"rate": 8.0}]
    r = await save_taxes(client, auth["headers"], own)
    assert r.status_code == 200, r.text
    diff = await _update_localization(client, auth, "TH")
    assert own[0]["name"] in [row["name"] for row in diff["kept"]]
    assert rate_today((await _taxes(client, auth))[0]) == 8.0


# ── The page: setup and Settings > Locations ─────────────────────────────────

@pytest.fixture()
def ui_client():
    from ui.app import app as ui_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://testserver",
                             follow_redirects=False, cookies={"celerp_token": make_test_token(role="owner")})


def _stub(stack: ExitStack, name: str, **kw) -> AsyncMock:
    mock = AsyncMock(**kw)
    stack.enter_context(patch(f"ui.api_client.{name}", new=mock))
    return mock


_LOC = {"id": "loc1", "name": "Main", "type": "warehouse", "is_default": True,
        "address": {"text": "12 MG Road, Bengaluru", "country": "IN"}}


async def test_setup_offers_every_tax_country_and_other(ui_client):
    from ui.routes.setup import company_choice_fields
    from fasthtml.common import Div, to_xml
    html = to_xml(Div(*company_choice_fields("", "", "")))
    country_box = html[html.index('name="country"'):]
    offered = re.findall(r'data-value="([^"]*)" role="option"', country_box.split('name="timezone"')[0])
    assert {code for code in offered if code} >= set(lock_codes())
    assert "" in offered  # Other country


async def test_setup_refuses_a_country_without_tax_rates():
    from ui.routes.setup import company_setup_error
    assert company_setup_error("EUR", "blank", "XX")[0] == "country"
    assert company_setup_error("EUR", "blank", "") is None


@pytest.mark.parametrize("field,value,stored,shown", [
    ("country", "TH", {"text": "12 MG Road, Bengaluru", "country": "TH"}, "Thailand"),
    ("address", "1 Silom Road", {"text": "1 Silom Road", "country": "IN"}, "1 Silom Road"),
    ("country", "", {"text": "12 MG Road, Bengaluru"}, "Other country"),
])
async def test_saving_address_or_country_keeps_the_other(ui_client, field, value, stored, shown):
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [_LOC]})
        patch_location = _stub(stack, "patch_location", return_value={**_LOC, "address": stored})
        r = await ui_client.patch(f"/settings/locations/loc1/{field}", data={"value": value})
    assert r.status_code == 200, r.text
    patch_location.assert_awaited_once()
    assert patch_location.await_args.args[2] == {"address": stored}
    assert "HX-Trigger" not in r.headers
    # The saved cell shows what was saved.
    assert re.search(r'<span class="cell-text">([^<]*)</span>', r.text).group(1) == shown


async def test_the_company_country_cell_names_the_country():
    from fasthtml.common import to_xml
    from ui.routes.settings import _company_display_cell

    def shown(field, value):
        return re.search(r'<span class="cell-text">([^<]*)</span>', to_xml(_company_display_cell(field, value))).group(1)

    assert shown("country", "TH") == "Thailand"
    assert shown("country", "") == "Other country"
    assert shown("name", "TH") == "TH"


async def test_the_address_cell_edits_the_text_not_the_record(ui_client):
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [_LOC]})
        r = await ui_client.get("/settings/locations/loc1/address/edit")
    assert 'value="12 MG Road, Bengaluru"' in r.text
    assert "{" not in re.search(r'<input[^>]*name="value"[^>]*value="([^"]*)"', r.text).group(1)
