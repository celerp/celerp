# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company names its tax country at setup or on its default location, and starts with
the tax rates in force there.

The country is stored as a code on the default location's address, beside the address
text: saving either one keeps the other. Naming a country replaces only taxes Celerp set
up itself (the generic list or a country's), never a company's own, and the page says
when it did.
"""
from __future__ import annotations

import copy
import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from celerp.tax_regimes import TAX_COUNTRIES, TAX_REGIMES
from test_helpers import make_test_token

pytestmark = pytest.mark.asyncio


async def _default_location(client, auth) -> dict:
    """The company's default location, as a new company has one, with no address yet."""
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": "Head office", "type": "office", "is_default": True})
    assert r.status_code == 200, r.text
    return r.json()


async def _taxes(client, auth) -> list[dict]:
    r = await client.get("/companies/me/taxes", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _names(taxes: list[dict]) -> list[str]:
    return [tax["name"] for tax in taxes]


async def test_naming_a_country_on_the_default_location_seeds_its_taxes_and_says_so(client, auth):
    loc = await _default_location(client, auth)
    r = await client.patch(f"/companies/me/locations/{loc['id']}", headers=auth["headers"],
                           json={"address": {"text": "12 MG Road, Bengaluru", "country": "IN"}})
    assert r.status_code == 200, r.text
    assert r.json()["taxes_from"] == "IN"
    assert _names(await _taxes(client, auth)) == _names(TAX_REGIMES["IN"]["taxes"])


async def test_another_country_before_any_change_replaces_the_first_countrys_taxes(client, auth):
    loc = await _default_location(client, auth)
    for country in ("TH", "IN"):
        r = await client.patch(f"/companies/me/locations/{loc['id']}", headers=auth["headers"],
                               json={"address": {"country": country}})
        assert r.json()["taxes_from"] == country
    assert _names(await _taxes(client, auth)) == _names(TAX_REGIMES["IN"]["taxes"])


async def test_a_company_with_its_own_taxes_keeps_them_and_hears_nothing(client, auth):
    own = copy.deepcopy(TAX_REGIMES["TH"]["taxes"])
    own[0]["rate"] = 8.0
    r = await client.patch("/companies/me/taxes", headers=auth["headers"], json={"taxes": own})
    assert r.status_code == 200, r.text
    loc = await _default_location(client, auth)
    r = await client.patch(f"/companies/me/locations/{loc['id']}", headers=auth["headers"],
                           json={"address": {"country": "IN"}})
    assert r.json()["taxes_from"] is None
    assert [tax["rate"] for tax in await _taxes(client, auth)][0] == 8.0


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
    assert {code for code in offered if code} >= set(TAX_COUNTRIES)
    assert "" in offered  # Other country


async def test_setup_names_the_country_on_the_default_location(ui_client):
    from ui.routes.setup import apply_company_setup
    with ExitStack() as stack:
        _stub(stack, "patch_company", return_value={})
        _stub(stack, "get_locations", return_value={"items": [{**_LOC, "address": None}]})
        patch_location = _stub(stack, "patch_location", return_value={})
        _stub(stack, "set_business_type", return_value={"restart_required": False})
        assert await apply_company_setup("tok", "INR", "Asia/Kolkata", "blank", "in") == "/dashboard"
    patch_location.assert_awaited_once_with("tok", "loc1", {"address": {"country": "IN"}})


async def test_setup_refuses_a_country_without_tax_rates():
    from ui.routes.setup import company_setup_error
    assert company_setup_error("EUR", "blank", "XX")[0] == "country"
    assert company_setup_error("EUR", "blank", "") is None


@pytest.mark.parametrize("field,value,stored", [
    ("country", "TH", {"text": "12 MG Road, Bengaluru", "country": "TH"}),
    ("address", "1 Silom Road", {"text": "1 Silom Road", "country": "IN"}),
    ("country", "", {"text": "12 MG Road, Bengaluru"}),
])
async def test_saving_address_or_country_keeps_the_other(ui_client, field, value, stored):
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [_LOC]})
        patch_location = _stub(stack, "patch_location", return_value={**_LOC, "address": stored,
                                                                       "taxes_from": None})
        r = await ui_client.patch(f"/settings/locations/loc1/{field}", data={"value": value})
    assert r.status_code == 200, r.text
    patch_location.assert_awaited_once()
    assert patch_location.await_args.args[2] == {"address": stored}
    assert "HX-Trigger" not in r.headers


async def test_a_country_that_set_the_taxes_is_announced_with_a_link_to_them(ui_client):
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [_LOC]})
        _stub(stack, "patch_location", return_value={**_LOC, "address": {"country": "TH"}, "taxes_from": "TH"})
        r = await ui_client.patch("/settings/locations/loc1/country", data={"value": "TH"})
    assert "Thailand" in r.text
    trigger = r.headers["HX-Trigger"]
    assert "Thailand" in trigger and "/settings/sales?tab=taxes" in trigger


async def test_the_address_cell_edits_the_text_not_the_record(ui_client):
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [_LOC]})
        r = await ui_client.get("/settings/locations/loc1/address/edit")
    assert 'value="12 MG Road, Bengaluru"' in r.text
    assert "{" not in re.search(r'<input[^>]*name="value"[^>]*value="([^"]*)"', r.text).group(1)
