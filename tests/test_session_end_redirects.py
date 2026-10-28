# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An ended session sends the browser to sign in, from every route behind the tax settings cells, the
tax CSV import and the document editor's own requests: never a cell error, an import failure, a
refused save or a save reported as done.

The API answers an ended session with 401. Each of these routes lets it reach the app's handler
(ui.app._401_redirect), which clears the session cookies and answers a page navigation with a 302 to
/login and an htmx or editor request with HX-Redirect. The editor's requests go through celerpFetch
(ui/components/shell.py), which follows that redirect as htmx does, so its callers never read the
reply as a saved or refused edit.
"""
from __future__ import annotations

import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _authed, _mock_get_company, _mock_get_modules, ui_client  # noqa: F401  (fixtures)
from ui.api_client import APIError

_ENDED = APIError(401, "Token expired")
_HX = {"HX-Request": "true", "HX-Current-URL": "http://ui/docs/doc%3AINV-1"}
_TAX = {"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True, "description": ""}
_DOC = {"id": "doc:INV-1", "doc_type": "invoice", "status": "draft", "currency": "USD", "version": 3,
        "line_items": [], "total": 0}


def _ended_after(n: int, value):
    """A mock answering ``value`` for its first ``n`` calls, then an ended session."""
    calls = iter([value] * n)

    async def _call(*_a, **_k):
        try:
            return next(calls)
        except StopIteration:
            raise _ENDED from None
    return AsyncMock(side_effect=_call)


def _ended():
    return AsyncMock(side_effect=_ENDED)


# (method, url, request kwargs, the API calls the route makes as {name: mock})
_CASES = {
    "tax-cell-edit": ("GET", "/settings/taxes/0/rate/edit", {}, {"get_taxes": _ended}),
    "tax-cell-display": ("GET", "/settings/taxes/0/rate/display", {}, {"get_taxes": _ended}),
    "purchase-tax-cell-edit": ("GET", "/settings/purchasing-taxes/0/rate/edit", {},
                               {"get_purchasing_taxes": _ended}),
    "tax-cell-patch-read": ("PATCH", "/settings/taxes/0/rate", {"data": {"value": "8"}}, {"get_taxes": _ended}),
    "tax-cell-patch-write": ("PATCH", "/settings/taxes/0/rate", {"data": {"value": "8"}},
                             {"get_taxes": lambda: AsyncMock(return_value=[dict(_TAX)]), "patch_taxes": _ended}),
    "tax-cell-patch-reread": ("PATCH", "/settings/taxes/0/rate", {"data": {"value": "8"}},
                              {"get_taxes": lambda: _ended_after(1, [dict(_TAX)]),
                               "patch_taxes": lambda: AsyncMock(return_value=None)}),
    "tax-csv-preview": ("POST", "/settings/import/taxes/preview",
                        {"files": {"csv_file": ("taxes.csv", b"name,rate\nVAT 7%,7\n", "text/csv")}},
                        {"get_company": _ended}),
    "tax-csv-confirm": ("POST", "/settings/import/taxes/confirm", {"data": {"csv_ref": "r"}},
                        {"batch_import": _ended}),
    "doc-lines-save": ("POST", "/docs/doc:INV-1/lines", {"json": {"line_items": [], "expected_version": 3}},
                       {"patch_doc": _ended}),
    "doc-lines-reread": ("POST", "/docs/doc:INV-1/lines", {"json": {"line_items": [], "expected_version": 3}},
                         {"patch_doc": lambda: AsyncMock(return_value={"version": 4}), "get_doc": _ended}),
    "list-lines-save": ("POST", "/lists/list:Q-1/lines",
                        {"json": {"line_items": [], "offset": 0, "original_count": 0, "expected_version": 3}},
                        {"patch_list_line_page": _ended}),
    "doc-reprice": ("POST", "/docs/doc:INV-1/reprice", {"json": {"price_list": "Retail", "expected_version": 3}},
                    {"reprice_doc": _ended}),
    "doc-items-csv": ("POST", "/docs/doc:INV-1/items/csv",
                      {"files": {"file": ("lines.csv", b"sku,quantity\nA1,1\n", "text/csv")}}, {"get_doc": _ended}),
    "doc-scan": ("POST", "/docs/doc:INV-1/scan", {"data": {"barcode": "A1"}}, {"scan_codes": _ended}),
    "list-set-scanned": ("POST", "/lists/list:Q-1/set-scanned", {"data": {"scanned": "1"}},
                         {"set_scanned": _ended}),
    "doc-page": ("GET", "/docs/doc:INV-1", {}, {"get_doc": _ended}),
    "list-page": ("GET", "/lists/list:Q-1", {}, {"get_list_page": _ended}),
}


async def _send(ui_client, case: str, headers: dict):
    method, url, kwargs, mocks = _CASES[case]
    with ExitStack() as stack:
        for name, make in mocks.items():
            stack.enter_context(patch(f"ui.api_client.{name}", new=make()))
        if case == "tax-csv-confirm":
            stack.enter_context(patch("ui.routes.settings_import.resolve_import_csv",
                                      new=AsyncMock(return_value="name,rate\nVAT 7%,8\n")))
        ui_client.cookies.update(_authed())
        return await ui_client.request(method, url, headers=headers, **kwargs)


def _cleared(r) -> bool:
    cookies = r.headers.get_list("set-cookie")
    return any(re.match(r'celerp_token="?"?;', c) for c in cookies)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_CASES))
async def test_an_htmx_or_editor_request_with_an_ended_session_goes_to_sign_in(ui_client, case):
    r = await _send(ui_client, case, _HX)
    assert r.status_code == 200, r.text
    assert r.headers.get("HX-Redirect", "").startswith("/login?reason=expired"), r.headers
    assert _cleared(r)
    assert '"ok"' not in r.text and "cell-error" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_CASES))
async def test_a_page_request_with_an_ended_session_goes_to_sign_in(ui_client, case):
    r = await _send(ui_client, case, {})
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith("/login?reason=expired")
    assert _cleared(r)


@pytest.mark.asyncio
async def test_the_editor_returns_to_its_document_after_signing_in(ui_client):
    r = await _send(ui_client, "doc-lines-save", _HX)
    assert r.headers["HX-Redirect"] == "/login?reason=expired&next=%2Fdocs%2Fdoc%253AINV-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "kwargs"), [
    ("/settings/taxes/0/rate/edit", {}),
    ("/docs/doc:INV-1/lines", {"json": {"line_items": []}}),
])
async def test_a_request_with_no_session_goes_to_sign_in(ui_client, url, kwargs):
    method = "POST" if kwargs else "GET"
    r = await ui_client.request(method, url, headers=_HX, **kwargs)
    assert r.headers.get("HX-Redirect", "").startswith("/login?reason=expired")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 422])
async def test_a_refused_line_save_keeps_the_refusals_own_status(ui_client, status):
    with patch("ui.api_client.patch_doc", new=AsyncMock(side_effect=APIError(status, "Refused"))):
        ui_client.cookies.update(_authed())
        r = await ui_client.post("/docs/doc:INV-1/lines", json={"line_items": [], "expected_version": 3}, headers=_HX)
    assert r.status_code == status and r.json() == {"error": "Refused"}


@pytest.mark.asyncio
async def test_a_tax_cell_refusal_stays_in_the_cell(ui_client):
    with patch("ui.api_client.get_taxes", new=AsyncMock(side_effect=APIError(503, "Unavailable"))):
        ui_client.cookies.update(_authed())
        r = await ui_client.get("/settings/taxes/0/rate/edit", headers=_HX)
    assert r.status_code == 200 and "cell-error" in r.text and "Unavailable" in r.text


def test_every_editor_request_follows_the_sign_in_redirect():
    """Class guard: the document editor calls its own routes only through celerpFetch, so a reply
    to an ended session is followed to sign in rather than read as a saved or refused edit."""
    import inspect

    import ui.routes.documents as documents

    source = inspect.getsource(documents)
    bare = [m.group(0) for m in re.finditer(r"(?<![\w.])fetch\(", source)]
    assert bare == [], f"{len(bare)} editor request(s) bypass celerpFetch"
