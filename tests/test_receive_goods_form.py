# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Receive Goods form on an order or bill brings the goods in as the page builds it.

The page lists the company's locations; picking one and pressing Receive Goods records
the receipt into that location, with each line received as the kind the line is.
"""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fasthtml.common import to_xml

import ui.api_client as api_client
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_receipt_accounting import _doc, _parcels


class _Routes:
    def __init__(self):
        self.routes: dict = {}

    def __getattr__(self, method):
        def register(path, *args, **kwargs):
            def deco(fn):
                self.routes[(method, path)] = fn
                return fn
            return deco
        return register


class _Request:
    cookies: dict = {}

    def __init__(self, form: list[tuple[str, str]]):
        self._form = form

    async def form(self):
        from starlette.datastructures import FormData
        return FormData(self._form)


class _ReceiveForm(HTMLParser):
    """The name/value pairs a browser submits from the Receive Goods form."""

    def __init__(self):
        super().__init__()
        self.fields: list[tuple[str, str]] = []
        self._inside = self._select = False
        self._picked = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._inside = a.get("id") == "li-bulk-fulfill-btn"
        elif self._inside and tag == "input" and a.get("name"):
            self.fields.append((a["name"], a.get("value") or ""))
        elif self._inside and tag == "select":
            self._select, self._picked = a.get("name"), False
        elif self._select and tag == "option" and not self._picked:
            self.fields.append((self._select, a.get("value") or ""))
            self._picked = True

    def handle_endtag(self, tag):
        if tag == "form":
            self._inside = False
        elif tag == "select":
            self._select = None


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
@pytest.mark.asyncio
async def test_the_receive_goods_form_records_a_receipt_into_the_chosen_location(client, session, auth, monkeypatch, doc_type):
    from ui.routes import documents

    r = await client.post("/companies/me/locations", headers=auth["headers"], json={"name": "Back room", "type": "warehouse"})
    assert r.status_code == 200, r.text
    locations = (await client.get("/companies/me/locations", headers=auth["headers"])).json()["items"]
    room = next(loc for loc in locations if loc["name"] == "Back room")
    locations = [room] + [loc for loc in locations if loc is not room]
    item_id = await _item(client, auth, 100.0, qty=0)
    lines = [{"item_id": item_id, "name": "Lot", "quantity": 3, "unit_price": 14.0}]
    if doc_type == "bill":
        lines.append({"name": "Delivery", "quantity": 1, "unit_price": 5.0})
    doc_id = await _doc(client, auth, doc_type, lines)
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()

    page = _ReceiveForm()
    page.feed(to_xml(documents._li_bulk_toolbar(doc_id, False, show_fulfill=True, is_inbound=True,
                                                inbound_line_items=doc["line_items"], locations=locations)))

    async def receive_po(_tok, entity_id, data):
        r = await client.post(f"/docs/{entity_id}/receive", headers=auth["headers"], json=data)
        return api_client._raise(r).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "receive_po", receive_po)
    routes = _Routes()
    documents.setup_routes(routes)
    handler = routes.routes[("post", "/docs/{entity_id}/receive")]

    resp = await handler(_Request(page.fields), doc_id)
    assert resp.status_code == 204, resp.body
    if doc_type == "purchase_order":
        assert (await _state(session, auth, item_id))["quantity"] == 3
    else:
        [parcel] = await _parcels(session, auth, doc_id)
        assert (parcel["quantity"], parcel["location_id"]) == (3, room["id"])
