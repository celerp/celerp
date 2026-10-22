# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes: Default and Compound are Yes/No cells edited with a Yes/No select, a value
that is neither is refused with a message (never read as No), Escape on any tax cell editor
puts the shown cell back unchanged, and the Numbering tab names a timezone problem instead of
showing an empty table."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError
from ui.i18n import t

pytestmark = pytest.mark.asyncio

_PREFIXES = [("taxes", "get_taxes", "patch_taxes"),
             ("purchasing-taxes", "get_purchasing_taxes", "patch_purchasing_taxes")]


def _taxes() -> list[dict]:
    return [{"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True, "is_compound": False,
             "description": "Standard rate"}]


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
@pytest.mark.parametrize("field", ["is_default", "is_compound"])
async def test_tax_bool_cell_refuses_other_values(ui_client, prefix, gname, pname, field):
    saved = AsyncMock(return_value={})
    ui_client.cookies.update(_authed())
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=lambda *_: _taxes())), \
         patch(f"ui.api_client.{pname}", new=saved):
        r = await ui_client.patch(f"/settings/{prefix}/0/{field}", data={"value": "maybe"})
        assert r.status_code == 200
        assert "cell-error" in r.text
        assert t("settings.value_must_be_yes_or_no") in r.text
        assert t("settings.value_must_be_yes_or_no") != "settings.value_must_be_yes_or_no"
        saved.assert_not_awaited()

        for value, expected in [("Yes", True), ("no", False), ("TRUE", True), ("0", False)]:
            r = await ui_client.patch(f"/settings/{prefix}/0/{field}", data={"value": value})
            assert r.status_code == 200
            assert saved.await_args.args[1][0][field] is expected


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
@pytest.mark.parametrize("field", ["is_default", "is_compound"])
async def test_taxes_tab_bool_fields_edit_as_yes_no(ui_client, prefix, gname, pname, field):
    ui_client.cookies.update(_authed())
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=lambda *_: _taxes())):
        r = await ui_client.get(f"/settings/{prefix}/0/{field}/edit")
    assert r.status_code == 200
    assert "<select" in r.text and "<input" not in r.text
    assert f'hx-patch="/settings/{prefix}/0/{field}"' in r.text
    assert ">Yes</option>" in r.text and ">No</option>" in r.text
    from bs4 import BeautifulSoup
    chosen = BeautifulSoup(r.text, "html.parser").select_one("option[selected]")
    assert chosen is not None and chosen["value"] == str(_taxes()[0][field]).lower()

    stored = {**_taxes()[0], field: not _taxes()[0][field]}
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=[_taxes(), [stored]])), \
         patch(f"ui.api_client.{pname}", new=AsyncMock(return_value={})):
        r = await ui_client.patch(f"/settings/{prefix}/0/{field}", data={"value": str(stored[field]).lower()})
    assert r.status_code == 200
    assert f">{'Yes' if stored[field] else 'No'}</span>" in r.text


async def test_taxes_tab_shows_a_compound_column(ui_client):
    from ui.routes.settings import _taxes_tab

    from fasthtml.common import to_xml
    html = to_xml(_taxes_tab([{**_taxes()[0], "is_compound": True}], prefix="taxes"))
    assert ">Compound<" in html
    assert 'hx-get="/settings/taxes/0/is_compound/edit"' in html
    assert html.index("/0/is_default/edit") < html.index("/0/is_compound/edit")


async def test_tax_cell_display_get_refuses_bad_input_and_shows_api_error(ui_client):
    url = "/settings/taxes/{idx}/{field}/display"
    r = await ui_client.get(url.format(idx=0, field="name"))
    assert "cell-error" in r.text and t("error.session_expired") in r.text

    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_taxes", new=AsyncMock(side_effect=APIError(503, "Taxes are unavailable."))):
        r = await ui_client.get(url.format(idx=0, field="name"))
    assert r.status_code == 200
    assert "cell-error" in r.text and "Taxes are unavailable." in r.text

    read = AsyncMock(side_effect=lambda *_: _taxes())
    with patch("ui.api_client.get_taxes", new=read):
        r = await ui_client.get(url.format(idx=-1, field="name"))
        assert "cell-error" in r.text and "VAT 7%" not in r.text
        r = await ui_client.get("/settings/taxes/-1/name/edit")
        assert "cell-error" in r.text and "VAT 7%" not in r.text
        r = await ui_client.get(url.format(idx=5, field="name"))
        assert r.status_code == 200 and ">--</span>" in r.text
        read.reset_mock()
        r = await ui_client.get(url.format(idx=0, field="secret"))
        assert "cell-error" in r.text
        read.assert_not_awaited()


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
async def test_tax_cell_escape_restores_the_display_cell(ui_client, prefix, gname, pname):
    ui_client.cookies.update(_authed())
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=lambda *_: _taxes())):
        r = await ui_client.get(f"/settings/{prefix}/0/name/display")
        assert r.status_code == 200
        assert ">VAT 7%</span>" in r.text and "cell--clickable" in r.text
        assert f'hx-get="/settings/{prefix}/0/name/edit"' in r.text
        for field in ["name", "rate", "tax_type", "is_default", "is_compound", "description"]:
            r = await ui_client.get(f"/settings/{prefix}/0/{field}/edit")
            assert "Escape" in r.text, field
            assert f"/settings/{prefix}/0/{field}/display" in r.text, field


@pytest.mark.parametrize("page", ["/settings/sales?tab=numbering", "/settings/purchasing?tab=numbering"])
async def test_numbering_tab_shows_the_timezone_error(ui_client, page):
    message = "Mars/Olympus isn't a time zone Celerp recognizes. Choose one from the list."
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc_sequences", new=AsyncMock(side_effect=APIError(422, message))), \
         patch("ui.api_client.get_taxes", new=AsyncMock(side_effect=lambda *_: _taxes())), \
         patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(side_effect=lambda *_: _taxes())), \
         patch("ui.api_client.get_purchasing_payment_terms", new=AsyncMock(return_value=[])), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"current_role": "owner", "settings": {}})):
        r = await ui_client.get(page)
    assert r.status_code == 200
    assert message in r.text
    assert "cell-error" in r.text
