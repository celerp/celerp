# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""After a write on a document page, no money figure older than the write stays on screen. Each
write route of the page either reloads the page, asks the page to redraw every figure marked
data-doc-money from the renderer it loads with, or answers the line editor, which puts in the
stored lines and their money; the rest change no money. The routes are found from the app, so a
new write route with no answer here turns this red."""

from __future__ import annotations

import inspect
import os
import re
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.components.doc_money import EVENT

DOC = "doc:inv-1"

# Writes that answer the line editor (stored lines and their money, installed together).
_LINE_EDITOR = {"/docs/{entity_id}/lines", "/docs/{entity_id}/items/csv", "/docs/{entity_id}/scan"}
# Writes whose page script reloads the page once they answer (the price-list change).
_CLIENT_RELOADS = {"/docs/{entity_id}/reprice"}
# Writes that change no money: notes, files, sharing, previews, text fields of a finalized line.
_NO_MONEY = {
    "/docs/{entity_id}/notes", "/docs/{entity_id}/notes/{note_id}", "/docs/{entity_id}/files",
    "/docs/{entity_id}/files/{file_id}", "/docs/{entity_id}/files/{file_id}/tag",
    "/docs/{entity_id}/files/{file_id}/description", "/docs/{entity_id}/share",
    "/docs/{entity_id}/field/{field}:POST", "/docs/{entity_id}/line/{li_index}/field/{field}",
    "/docs/{entity_id}/tax-rate/preview", "/docs/{entity_id}/invoice-lines/preview",
}


def _handler(endpoint):
    """The route's own function inside the app's request wrapper."""
    for cell in endpoint.__closure__ or ():
        fn = cell.cell_contents
        if inspect.isfunction(fn) and fn.__module__.startswith("ui."):
            return fn
    return endpoint


def _with_helpers(source: str, module_source: str) -> str:
    """``source`` with the source of each private helper of the module it awaits, so a route that
    hands its answer to a shared helper is judged by what that helper answers."""
    for name in set(re.findall(r"await (_[a-z_]+)\(", source)):
        found = re.search(rf"^( *)(async )?def {name}\(.*?(?=^\1(?:async def |def |@)|\Z)", module_source, re.M | re.S)
        if found:
            source += found.group(0)
    return source


def _page_writes():
    from ui.app import app
    for route in app.routes:
        path = getattr(route, "path", "")
        for method in sorted(set(getattr(route, "methods", None) or ()) - {"GET", "HEAD", "OPTIONS"}):
            if path.startswith("/docs/{entity_id}"):
                yield path, method, _handler(route.endpoint)


def test_every_document_page_write_leaves_no_older_money_on_screen():
    from ui.routes import documents

    module_source = inspect.getsource(documents)
    unanswered = []
    for path, method, endpoint in _page_writes():
        assert endpoint.__module__.startswith("ui."), (path, endpoint)
        source = _with_helpers(inspect.getsource(endpoint), module_source)
        if path in _LINE_EDITOR | _CLIENT_RELOADS or path in _NO_MONEY or f"{path}:{method}" in _NO_MONEY:
            continue
        if "HX-Redirect" in source or "HX-Refresh" in source or "doc_money_redraw" in source:
            continue
        unanswered.append(f"{method} {path}")
    assert unanswered == [], f"document page writes that leave older money figures on screen: {unanswered}"


def test_every_money_header_field_is_redrawn():
    """Every header field the page edits in place whose value goes into the document's money is
    one whose save redraws the money figures."""
    from celerp_docs.routes import _DOC_MONEY_INPUTS
    from ui.routes import documents

    edited = set(re.findall(r'_cell\("([a-z_]+)"', inspect.getsource(documents))) | {"tax_rate"}
    money = (_DOC_MONEY_INPUTS | {"conversion_rate"}) & edited
    assert money and money <= documents.DOC_MONEY_FIELDS, money - documents.DOC_MONEY_FIELDS
    assert documents.DOC_MONEY_FIELDS <= _DOC_MONEY_INPUTS | {"conversion_rate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["currency", "conversion_rate", "tax_rate", "due_date"])
async def test_a_header_save_redraws_the_money_only_when_it_changes_it(field):
    from ui.app import app as ui_app
    from ui.routes.documents import DOC_MONEY_FIELDS

    doc = {"id": DOC, "doc_type": "invoice", "status": "draft", "currency": "USD", "version": 2, "line_items": []}
    client = AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui")
    client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
            patch("ui.api_client.patch_doc", new=AsyncMock(return_value={})):
        r = await client.patch(f"/docs/{DOC}/field/{field}", data={"value": "7"})
    assert r.status_code == 200, r.text
    assert (EVENT in r.headers.get("HX-Trigger", "")) == (field in DOC_MONEY_FIELDS)


def test_the_page_marks_every_money_figure_it_redraws():
    """The totals panel, Outstanding and the payments are each marked, under one id, on the page."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_detail

    doc = {"id": DOC, "doc_type": "invoice", "status": "final", "currency": "USD", "version": 2,
           "subtotal": 100.0, "total": 100.0, "amount_outstanding": 100.0, "amount_paid": 0,
           "line_items": [{"line_id": "l-1", "description": "Fee", "quantity": 1, "unit_price": 100.0}]}
    html = to_xml(_doc_detail(doc))
    marked = re.findall(r'<[a-z]+[^>]*data-doc-money="true"[^>]*>', html)
    ids = [re.search(r'id="([^"]+)"', tag).group(1) for tag in marked]
    assert {"doc-total-panel", "doc-outstanding", "doc-payments"} <= set(ids) and len(ids) == len(set(ids)), ids
