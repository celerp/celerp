# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The fund controls on a document page in a company with funds in use: "Fund for this
document" in the header of a record that came from outside, a Fund cell on each line, and
"Set all outside lines to" above the lines while a line from outside has no fund to post to.
The API is mocked; the writes themselves are tested in test_outside_lines."""
from __future__ import annotations

import os
import re

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from ui.components.doc_money import EVENT
from test_ui import _company_stub, _mock_get_company  # noqa: F401  (autouse fixture: the company settings)

pytestmark = pytest.mark.asyncio

_FUNDS = {"items": [{"id": "fund:r", "name": "Building fund", "archived": False},
                    {"id": "fund:old", "name": "Old appeal", "archived": True}]}
_FROM = "From a connected company"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _bill(*, status: str = "draft", choice: str | None = None, line_choice: str | None = None,
          local_fund: str | None = None) -> dict:
    marked = {"line_id": "L1", "description": "Paper", "quantity": 1, "unit_price": 40.0, "line_total": 40.0,
              "origin": "connected_company", **({"fund_choice": line_choice} if line_choice else {})}
    local = {"line_id": "L2", "description": "Delivery", "quantity": 1, "unit_price": 10.0, "line_total": 10.0,
             **({"fund_id": local_fund} if local_fund else {})}
    return {"id": "doc:b1", "doc_type": "bill", "status": status, "currency": "USD", "version": 7,
            "origin": "connected_company", **({"fund_choice": choice} if choice else {}),
            "line_items": [marked, local], "total": 50.0}


async def _page(ui_client, doc: dict, *, settings: dict | None = None) -> str:
    company = {"id": "c1", "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01",
               "settings": {"funds_enabled": True} if settings is None else settings}
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))):
        r = await ui_client.get(f"/docs/{doc['id']}")
    assert r.status_code == 200, r.text
    return r.text


def _element(html: str, dom_id: str) -> str | None:
    """The text inside the element with ``dom_id`` (tags dropped), or None when absent."""
    found = re.search(rf'<(\w+)[^>]*id="{re.escape(dom_id)}"[^>]*>(.*?)</\1>', html, re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", found.group(2)).split()) if found else None


async def test_document_fund_cell_starts_unset_for_outside_lines(ui_client):
    html = await _page(ui_client, _bill())
    marked = _element(html, "fund-cell-L1")
    assert marked is not None and "--" in marked and _FROM in marked, marked
    assert _picked(html, "L2") == "unrestricted"
    assert 'class="badge badge--origin"' in html
    header = _element(html, "doc-fund")
    assert header is not None and "--" in header and _FROM in header, header
    assert "Set all outside lines to" in (_element(html, "fund-set-all") or ""), "Set all missing"
    finalize = re.search(r"<button[^>]*/docs/doc:b1/action/finalize[^>]*>", html)
    assert finalize and "disabled" not in finalize.group(0), "Finalize must stay enabled"
    # Only active funds are offered, after Unrestricted.
    assert "Old appeal" not in html

    # The line's own choice reads as its fund; nothing is left waiting, so Set all goes.
    chosen = await _page(ui_client, _bill(line_choice="fund:r"))
    assert "Building fund" in _element(chosen, "fund-cell-L1")
    assert not _element(chosen, "fund-set-all")

    # The document's choice covers the line, which reads -- and, muted, the fund it uses.
    covered = await _page(ui_client, _bill(choice="fund:r"))
    cell = _element(covered, "fund-cell-L1")
    assert "--" in cell and "uses Building fund" in cell, cell
    assert "Building fund" in _element(covered, "doc-fund")
    assert not _element(covered, "fund-set-all")

    # A local line shows the fund it carries.
    assert _picked(await _page(ui_client, _bill(local_fund="fund:r")), "L2") == "fund:r"


def _picked(html: str, line_id: str) -> str | None:
    """The value of the fund picker in a line's Fund cell, or None when the cell only reads."""
    cell = re.search(rf'id="fund-cell-{line_id}".*?</td>', html, re.S)
    assert cell, f"no Fund cell on {line_id}"
    found = re.search(r'<input[^>]*data-name="fund_id"[^>]*>', cell.group(0))
    return re.search(r'value="([^"]*)"', found.group(0)).group(1) if found else None


def _local(doc_type: str = "bill", *, status: str = "draft", **line) -> dict:
    """A document the company entered itself, of one line."""
    li = {"line_id": "L2", "description": "Delivery", "quantity": 1, "unit_price": 10.0, "line_total": 10.0, **line}
    return {"id": "doc:b1", "doc_type": doc_type, "status": status, "currency": "USD", "version": 7,
            "line_items": [li], "total": 10.0}


async def test_fund_controls_absent_without_funds(ui_client):
    off = await _page(ui_client, _bill(), settings={})
    assert _element(off, "fund-cell-L1") is None and _element(off, "doc-fund") is None
    assert 'class="col-fund"' not in await _page(ui_client, _local(), settings={})


@pytest.mark.parametrize("doc_type", ["invoice", "bill", "credit_note", "debit_note", "purchase_order"])
async def test_a_local_draft_line_picks_its_fund_in_the_row(ui_client, doc_type):
    """A document the company entered itself has a Fund column on each line, Unrestricted
    until a fund is picked; the pick is a searchable select of the line editor's row, saved
    with the lines. A local document has no "Fund for this document" or Set all."""
    html = await _page(ui_client, _local(doc_type))
    assert _picked(html, "L2") == "unrestricted"
    assert re.search(r'<th[^>]*class="col-fund"', html), "Fund header missing"
    assert _element(html, "doc-fund") is None and not _element(html, "fund-set-all")
    # The empty row a new line starts from offers the same picker.
    assert len(re.findall(r'<input[^>]*data-name="fund_id"', html)) == 2
    assert "Building fund" in html and "Old appeal" not in html
    # The row editor sends the pick with the line.
    assert "fund_id: fundEl.value || null" in html
    # A fund the line carries reads by name, an archived one too.
    assert _picked(await _page(ui_client, _local(doc_type, fund_id="fund:r")), "L2") == "fund:r"
    archived = await _page(ui_client, _local(doc_type, fund_id="fund:old"))
    assert _picked(archived, "L2") == "fund:old" and "Old appeal" in archived


async def test_a_local_line_fund_only_reads_where_it_cannot_change(ui_client):
    # Issued: the fund it posted with, as text.
    html = await _page(ui_client, _local(status="final", fund_id="fund:r"))
    assert _picked(html, "L2") is None and _element(html, "fund-cell-L2") == "Building fund"
    # A credit note line takes the fund of the invoice line it credits.
    note = await _page(ui_client, _local("credit_note", source_line_id="X1", fund_id="fund:r"))
    assert _picked(note, "L2") is None and _element(note, "fund-cell-L2") == "Building fund"
    # A document whose lines post to no fund has no Fund column.
    assert 'class="col-fund"' not in await _page(ui_client, _local("quotation"))


async def test_a_line_save_stores_unrestricted_as_no_fund(ui_client):
    """The row's Unrestricted is a local line with no fund; a picked fund goes as picked, and a
    line the row sends no pick for (a line from outside) keeps what it holds."""
    patch_doc = AsyncMock(return_value={"version": 8})
    lines = [{"line_id": "L1", "description": "Paper", "quantity": 1, "unit_price": 40.0},
             {"line_id": "L2", "description": "Delivery", "quantity": 1, "unit_price": 10.0, "fund_id": "unrestricted"},
             {"line_id": "L3", "description": "Hall", "quantity": 1, "unit_price": 5.0, "fund_id": "fund:r"}]
    with patch("ui.api_client.patch_doc", new=patch_doc), \
         patch("ui.api_client.get_doc", new=AsyncMock(return_value=_bill())):
        r = await ui_client.post("/docs/doc:b1/lines", json={"line_items": lines, "expected_version": 7})
    assert r.status_code == 200, r.text
    sent = patch_doc.await_args.args[2]["line_items"]
    assert "fund_id" not in sent[0] and sent[1]["fund_id"] is None and sent[2]["fund_id"] == "fund:r"


def _tag(html: str, dom_id: str) -> str:
    """The opening tag of the element with ``dom_id``, whatever order its attributes are in."""
    return re.search(rf'<[^>]*\bid="{re.escape(dom_id)}"[^>]*>', html).group(0)


async def test_posted_outside_document_changes_fund_with_a_reason(ui_client):
    """A posted record from outside keeps its fund fields click to edit for a user who manages
    accounting: the editor is Change fund, a fund with a required reason and the form's own key,
    saved to the change route; its local lines and Set all stay read only, and a refusal keeps
    the selection, the reason and the key beside Try again."""
    from ui.api_client import APIError

    posted = _bill(status="final", line_choice="fund:r")
    html = await _page(ui_client, posted)
    assert "Building fund" in _element(html, "fund-cell-L1")
    assert 'hx-get="/docs/doc:b1/funds/L1/edit"' in _tag(html, "fund-cell-L1") and "hx-get" in _tag(html, "doc-fund")
    assert "hx-get" not in _tag(html, "fund-cell-L2") and "Set all outside lines to" not in html

    company = {"id": "c1", "name": "Test Corp", "currency": "USD", "settings": {"funds_enabled": True}}

    async def _call(method: str, url: str, docs: list[dict], change: AsyncMock | None = None, data=None):
        with patch("ui.api_client.get_doc", new=AsyncMock(side_effect=docs)), \
             patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
             patch("ui.api_client.change_doc_fund", new=change or AsyncMock()), \
             patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))):
            return await (ui_client.get(url) if method == "GET" else ui_client.post(url, data=data))

    r = await _call("GET", "/docs/doc:b1/funds/L1/edit", [posted])
    form = re.search(r"<form.*?</form>", r.text, re.S).group(0)
    assert 'hx-post="/docs/doc:b1/funds/change"' in form and 'hx-trigger="submit"' in form
    assert re.search(r'<input[^>]*name="reason"[^>]*required', form)
    assert re.search(r'<input[^>]*type="hidden"[^>]*name="idempotency_key"', form)
    assert "posts them again on the new fund" in form

    change = AsyncMock(return_value={"copies": {"doc:b1": "doc:b1"}})
    moved = {**_bill(status="final", line_choice="unrestricted"), "version": 9}
    r = await _call("POST", "/docs/doc:b1/funds/change", [posted, moved], change,
                    {"target": "L1", "value": "unrestricted", "reason": "Wrong grant", "expected_version": "7",
                     "idempotency_key": "k1"})
    assert r.status_code == 200, r.text
    assert change.await_args.args[1:] == ("doc:b1", {"expected_version": 7, "request_key": "k1",
                                                     "reason": "Wrong grant", "lines": {"L1": "unrestricted"}})
    assert '"version": 9' in r.headers["HX-Trigger"] and "Unrestricted" in _element(r.text, "fund-cell-L1")
    assert EVENT in r.headers["HX-Trigger"]  # the open balance by fund on the payment form is redrawn
    change = AsyncMock(return_value={"copies": {}})
    await _call("POST", "/docs/doc:b1/funds/change", [posted, posted], change,
                {"target": "document", "value": "fund:r", "reason": "Grant", "expected_version": "7",
                 "idempotency_key": "k2"})
    assert change.await_args.args[2] == {"expected_version": 7, "request_key": "k2", "reason": "Grant",
                                         "document": "fund:r"}

    refused = AsyncMock(side_effect=APIError(422, "This period is locked."))
    r = await _call("POST", "/docs/doc:b1/funds/change", [posted], refused,
                    {"target": "L1", "value": "unrestricted", "reason": "Wrong grant", "expected_version": "7",
                     "idempotency_key": "k3"})
    assert "This period is locked." in r.text and "Try again" in r.text
    assert 'value="Wrong grant"' in r.text and 'value="k3"' in r.text and 'value="unrestricted"' in r.text

    ui_client.cookies.update({"celerp_token": make_test_token(role="viewer")})
    html = await _page(ui_client, posted)
    assert "hx-get" not in _tag(html, "fund-cell-L1") and "hx-get" not in _tag(html, "doc-fund")


async def _post(ui_client, doc_after: dict, choose: AsyncMock, data: dict, *, doc: dict | None = None):
    company = {"id": "c1", "name": "Test Corp", "currency": "USD", "settings": {"funds_enabled": True}}
    with patch("ui.api_client.get_doc", new=AsyncMock(side_effect=[doc or _bill(), doc_after])), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.choose_doc_funds", new=choose), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))):
        return await ui_client.post("/docs/doc:b1/funds", data=data)


async def test_fund_save_redraws_every_control_and_keeps_a_refused_choice(ui_client):
    from ui.api_client import APIError

    choose = AsyncMock(return_value={"version": 8})
    r = await _post(ui_client, _bill(choice="fund:r"), choose,
                    {"target": "document", "value": "fund:r", "expected_version": "7"})
    assert r.status_code == 200, r.text
    assert choose.await_args.args[1:] == ("doc:b1", {"expected_version": 7, "document": "fund:r"})
    assert '"version": 8' in r.headers["HX-Trigger"]
    # The saved control first, then the line cell and Set all redrawn out of band.
    assert r.text.index('id="doc-fund"') < r.text.index('id="fund-cell-L1"')
    assert "uses Building fund" in r.text and 'id="fund-set-all"' in r.text
    assert r.text.count('hx-swap-oob="true"') == 3  # line 1, line 2, Set all
    assert "Set all outside lines to" not in r.text

    # One line's choice and Set all go to the route as lines and all.
    choose = AsyncMock(return_value={"version": 8})
    await _post(ui_client, _bill(line_choice="unrestricted"), choose,
                {"target": "L1", "value": "unrestricted", "expected_version": "7"})
    assert choose.await_args.args[2] == {"expected_version": 7, "lines": {"L1": "unrestricted"}}
    choose = AsyncMock(return_value={"version": 8})
    await _post(ui_client, _bill(line_choice="fund:r"), choose, {"target": "all", "value": "fund:r", "expected_version": "7"})
    assert choose.await_args.args[2] == {"expected_version": 7, "all": "fund:r"}

    # A stale save keeps the selection beside the reason, with Try again.
    stale = AsyncMock(side_effect=APIError(409, "This document changed. Reload it."))
    r = await _post(ui_client, _bill(), stale, {"target": "L1", "value": "fund:r", "expected_version": "6"})
    assert "Try again" in r.text and 'value="fund:r"' in r.text and 'id="fund-cell-L1"' in r.text


async def test_fund_cell_read_only_without_edit_permission(ui_client):
    company = {"id": "c1", "name": "Test Corp", "currency": "USD", "settings": {"funds_enabled": True}}
    ui_client.cookies.update({"celerp_token": make_test_token(role="viewer")})
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=_bill())), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))):
        r = await ui_client.get("/docs/doc:b1/funds/L1/edit")
    assert 'id="fund-cell-L1"' in r.text and "<form" not in r.text and "hx-get" not in r.text


async def test_a_line_fund_change_is_named_in_the_history():
    from ui.components.activity import _fields_changed_summary
    from ui.i18n import t

    line = {"line_id": "L1", "name": "Library gift", "quantity": 1, "unit_price": 300.0}
    picked = _fields_changed_summary({"line_items": {"old": [line], "new": [{**line, "fund_id": "fund:r"}]}}, "USD")
    assert t("activity.changed", item=f"Library gift {t('th.fund')}") in picked, picked
    unrestricted = {**line, "fund_id": None}
    assert _fields_changed_summary({"line_items": {"old": [line], "new": [unrestricted]}}, "USD") == ""


async def test_a_document_fund_change_is_named_in_the_history():
    """The document's own fund choice is named in its history, never by its raw fund id; who set
    it and why are not shown as changed fields."""
    from ui.components.activity import _fields_changed_summary
    from ui.i18n import t

    picked = _fields_changed_summary({"fund_choice": {"old": None, "new": "fund:r"},
                                      "fund_choice_source": {"old": None, "new": "user"},
                                      "fund_choice_reason": {"old": None, "new": "Board minute"}}, "USD")
    assert picked == t("activity.changed", item=t("fund.document_label")), picked
