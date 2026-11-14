# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Taxes: Default and Compound are Yes/No cells edited with a Yes/No select, a value
that is neither is refused with a message (never read as No), Escape on any tax cell editor
puts the shown cell back unchanged, and the Numbering tab names a timezone problem instead of
showing an empty table."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import html
import re

import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError
from ui.i18n import t


@pytest.fixture(autouse=True)
def _owner_company():
    """The numbering cells check manage_module_settings, which reads the company; an owner
    passes. A test that needs another company patches get_company inside this one."""
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"current_role": "owner", "settings": {}})):
        yield

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
    assert r.status_code == 302 and r.headers["location"].startswith("/login")

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
            # Escape on the editor puts this display cell back (celerpCellKey in celerp-page.js).
            assert "celerpCellKey(this,event," in r.text, field
            assert f"/settings/{prefix}/0/{field}/display" in r.text, field


@pytest.mark.parametrize("page", ["/settings/sales?tab=numbering", "/settings/purchasing?tab=numbering"])
async def test_numbering_tab_shows_the_timezone_error(ui_client, page):
    """The tab names the company time zone as the reason and links to the page that sets it."""
    message = t("error.company_timezone_unknown", "en")
    refusal = APIError(422, message, data={"detail": message, "code": "company_timezone"})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc_sequences", new=AsyncMock(side_effect=refusal)), \
         patch("ui.api_client.get_taxes", new=AsyncMock(side_effect=lambda *_: _taxes())), \
         patch("ui.api_client.get_purchasing_taxes", new=AsyncMock(side_effect=lambda *_: _taxes())), \
         patch("ui.api_client.get_purchasing_payment_terms", new=AsyncMock(return_value=[])), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"current_role": "owner", "settings": {}})):
        r = await ui_client.get(page)
    assert r.status_code == 200
    assert re.search(r'<p class="cell-error">' + re.escape(message) + r'</p>\s*<a href="/finance/company-details"', r.text)


@pytest.mark.parametrize("prefix", ["taxes", "purchasing-taxes"])
async def test_taxes_tab_rate_column_is_right_aligned(prefix):
    # Rate is a number column: its header and every cell sit right, over the figures.
    from fasthtml.common import to_xml
    from ui.routes.settings import _tax_display_cell, _taxes_tab
    html = to_xml(_taxes_tab(_taxes(), prefix=prefix))
    header = re.search(r"<th[^>]*>" + re.escape(t("th.rate")) + "</th>", html)
    assert header and "cell--number" in header.group(0), html
    assert 'class="cell cell--clickable cell--number"' in to_xml(_tax_display_cell(0, "rate", _taxes()[0], prefix=prefix))
    assert "cell--number" not in to_xml(_tax_display_cell(0, "name", _taxes()[0], prefix=prefix))


@pytest.mark.parametrize("rate, shown", [(0, "0%"), (0.0, "0%"), (7.0, "7%"), (7.25, "7.25%"), (None, "--"), ("", "--")])
async def test_a_zero_rate_reads_as_a_rate_not_an_empty_cell(rate, shown):
    from fasthtml.common import to_xml
    from ui.routes.settings import _tax_display_cell
    assert f">{shown}</span>" in to_xml(_tax_display_cell(0, "rate", {"rate": rate}))


@pytest.mark.parametrize("tax, label", [({"name": "Exempt", "rate": 0.0}, "Exempt (0%)"),
                                        ({"code": "GST", "rate": 18.0}, "GST (18%)"),
                                        ({"name": "VAT", "rate": 7.5}, "VAT (7.5%)")])
async def test_the_line_tax_choice_names_the_rate_without_a_trailing_zero(tax, label):
    from ui.routes.documents import _tax_label
    assert _tax_label(tax) == label


@pytest.mark.parametrize("prefix, gname, pname", _PREFIXES)
@pytest.mark.parametrize("field", ["name", "rate", "description"])
async def test_a_typed_tax_cell_saves_on_enter_and_leaves_untouched_without_a_write(ui_client, prefix, gname, pname, field):
    ui_client.cookies.update(_authed())
    with patch(f"ui.api_client.{gname}", new=AsyncMock(side_effect=lambda *_: _taxes())):
        r = await ui_client.get(f"/settings/{prefix}/0/{field}/edit")
    text = html.unescape(r.text)
    assert f'celerpCellKey(this,event,{{"url": "/settings/{prefix}/0/{field}/display", "enter": "trigger"}})' in text, field
    # Blur saves only a changed value; an untouched editor restores the shown cell.
    assert "this.value!==this._v" in text, field
    assert '"when": "unchanged"' in text, field


@pytest.mark.parametrize("prefix", ["taxes", "purchasing-taxes"])
async def test_new_tax_waits_for_a_pending_cell_save(prefix):
    # The tab's requests run one after another, and New Tax is sent only after a blurred
    # editor's save has been sent, so a rate typed just before the click is not overwritten.
    from fasthtml.common import to_xml
    from ui.routes.settings import _taxes_tab
    html = to_xml(_taxes_tab(_taxes(), prefix=prefix))
    assert re.search(r'class="settings-card"[^>]*hx-sync="this:queue all"|hx-sync="this:queue all"[^>]*class="settings-card"', html), html
    button = re.search(r'<button[^>]*hx-post="/settings/' + prefix + r'/new"[^>]*>', html).group(0)
    assert 'hx-trigger="click delay:' in button, button


# A company still on the shared List counter: its Transfer row shows that counter's values.
_LEGACY_TRANSFER = {"doc_type": "transfer", "prefix": "LST", "pattern": "{PREFIX}-{YY}{MM}-{####}",
                    "next": 41, "preview": "LST-2611-0041"}


@pytest.mark.parametrize("field, shown", [("prefix", "LST"), ("pattern", "{PREFIX}-{YY}{MM}-{####}"),
                                          ("next", "41")])
async def test_numbering_cell_left_unchanged_saves_nothing(ui_client, field, shown):
    """Clicking into a Numbering cell and leaving it changes nothing: no save is sent, so the
    type stays on the counter it draws from (here the shared one)."""
    saved = AsyncMock(return_value={})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"current_role": "owner", "settings": {}})), \
         patch("ui.api_client.get_doc_sequences", new=AsyncMock(return_value=[_LEGACY_TRANSFER])), \
         patch("ui.api_client.patch_doc_sequence", new=saved):
        r = await ui_client.patch(f"/settings/numbering/transfer/{field}", data={"value": f" {shown} "})
    assert r.status_code == 200 and "cell-error" not in r.text
    assert "LST-2611-0041" in r.text
    saved.assert_not_awaited()


async def test_numbering_cell_changed_is_saved(ui_client):
    saved = AsyncMock(return_value={})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"current_role": "owner", "settings": {}})), \
         patch("ui.api_client.get_doc_sequences", new=AsyncMock(return_value=[_LEGACY_TRANSFER])), \
         patch("ui.api_client.patch_doc_sequence", new=saved):
        r = await ui_client.patch("/settings/numbering/transfer/prefix", data={"value": "QUOTE"})
    assert r.status_code == 200
    saved.assert_awaited_once()
    assert saved.await_args.args[1:] == ("transfer", {"prefix": "QUOTE"})


async def test_numbering_reset_shows_the_row_as_saved(ui_client):
    """Reset sends next 1 and redraws the row from the sequences read after it, so the row shows
    what the counter now holds."""
    after = {**_LEGACY_TRANSFER, "next": 1, "preview": "LST-2611-0001"}
    saved = AsyncMock(return_value={})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc_sequences", new=AsyncMock(side_effect=[[_LEGACY_TRANSFER], [after]])), \
         patch("ui.api_client.patch_doc_sequence", new=saved):
        r = await ui_client.post("/settings/numbering/transfer/reset", headers={"HX-Request": "true"})
    assert saved.await_args.args[1:] == ("transfer", {"next": 1})
    assert r.status_code == 200 and r.text.lstrip().startswith("<tr")
    assert "LST-2611-0001" in r.text and "LST-2611-0041" not in r.text


@pytest.mark.parametrize("lang", ["en", "th"])
async def test_a_refused_numbering_save_keeps_the_row_open_with_the_reason(ui_client, lang):
    """A refused save leaves the whole row in place, the typed value still open in its cell,
    the reason in the user's language and Reset beside it, so the user can choose again."""
    from celerp.accounting_roles import catalog_refusal
    detail = catalog_refusal("documents.err_numbering_overlap", doc_type="quotation")
    ui_client.cookies.update({**_authed(), "celerp_lang": lang})
    with patch("ui.api_client.get_doc_sequences", new=AsyncMock(return_value=[_LEGACY_TRANSFER])), \
         patch("ui.api_client.patch_doc_sequence", new=AsyncMock(side_effect=APIError(422, detail))):
        htmx = {"HX-Request": "true"}
        r = await ui_client.patch("/settings/numbering/transfer/prefix", data={"value": "QUO"}, headers=htmx)
        reset = await ui_client.post("/settings/numbering/transfer/reset", headers=htmx)
    assert r.status_code == 200 and r.text.lstrip().startswith("<tr")
    assert 'name="value"' in r.text and 'value="QUO"' in r.text
    assert 'hx-patch="/settings/numbering/transfer/prefix"' in r.text
    assert "/settings/numbering/transfer/reset" in r.text and "LST-2611-0041" in r.text
    message = t("documents.err_numbering_overlap", lang, doc_type=t("enum.doc_type.quotation", lang))
    assert message in r.text and "{" not in message
    assert reset.text.lstrip().startswith("<tr") and message in reset.text
    assert 'name="value"' not in reset.text


def _unknown_type_refusal() -> dict:
    """The API's refusal of a doc type it does not number, as the API builds it."""
    from celerp.accounting_roles import Refused
    from celerp_docs.sequences import require_doc_type
    with pytest.raises(Refused) as refused:
        require_doc_type("bogus")
    return refused.value.detail


@pytest.mark.parametrize("method, url, value", [
    ("patch", "/settings/numbering/bogus/prefix", "QUO"),
    ("patch", "/settings/numbering/bogus/prefix", ""),
    ("patch", "/settings/numbering/bogus/pattern", "{PREFIX}-{####}"),
    ("patch", "/settings/numbering/bogus/next", "5"),
    ("post", "/settings/numbering/bogus/reset", None),
])
async def test_a_numbering_save_for_an_unknown_doc_type_shows_the_api_refusal(ui_client, method, url, value):
    """A save or Reset for a doc type the API does not list answers the API's refusal in the
    row, not an error page, even when the typed value matches nothing saved."""
    detail = _unknown_type_refusal()
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc_sequences", new=AsyncMock(return_value=[_LEGACY_TRANSFER])), \
         patch("ui.api_client.patch_doc_sequence", new=AsyncMock(side_effect=APIError(422, detail))):
        kwargs = {"data": {"value": value}} if value is not None else {}
        r = await getattr(ui_client, method)(url, headers={"HX-Request": "true"}, **kwargs)
    assert r.status_code == 200 and r.text.lstrip().startswith("<tr")
    from html import escape
    from ui.i18n import refusal_text
    assert "cell-error" in r.text and escape(refusal_text(detail), quote=False) in r.text


@pytest.mark.parametrize("url", ["/settings/numbering/bogus/prefix/edit", "/settings/numbering/bogus/prefix/display"])
async def test_opening_a_numbering_cell_for_an_unknown_doc_type_does_not_fail(ui_client, url):
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc_sequences", new=AsyncMock(return_value=[_LEGACY_TRANSFER])):
        r = await ui_client.get(url)
    assert r.status_code == 200 and "editable-cell" in r.text
