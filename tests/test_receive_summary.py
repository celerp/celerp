# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A receipt says what it did with each line it received, and only that receipt's lines.

Stock lines add stock; expense and asset lines add none, and the summary says so without
claiming anything was booked. A retried receipt answers exactly as the first one did.
"""
from __future__ import annotations

import json
import uuid

import pytest

import ui.api_client as api_client
from test_receipt_accounting import _doc, _finalize
from test_receive_goods_form import _Request, _Routes
from test_receive_selected_lines import _post, _stamp_line_ids

pytestmark = pytest.mark.asyncio


async def _mixed_bill(client, session, auth) -> tuple[str, list[str]]:
    tag = uuid.uuid4().hex[:6]
    bill = await _doc(client, auth, "bill", [
        {"sku": f"SUM-{tag}-0", "name": "Goods A", "quantity": 2, "unit_price": 5.0},
        {"sku": f"SUM-{tag}-1", "name": "Goods B", "quantity": 2, "unit_price": 5.0},
        {"name": "Courier fee", "quantity": 1, "unit_price": 25.0, "receive_as": "expense"},
        {"name": "Shelving", "quantity": 1, "unit_price": 300.0, "receive_as": "asset"},
    ])
    await _finalize(client, auth, bill)
    return bill, await _stamp_line_ids(session, auth, bill)


async def test_receive_summary_this_operation(client, session, auth):
    bill, ids = await _mixed_bill(client, session, auth)

    r = await _post(client, auth, bill,
                    {"source_line_id": ids[0], "quantity_received": 2},
                    {"source_line_id": ids[2], "quantity_received": 1})
    assert r.status_code == 200, r.text
    assert r.json()["line_counts"] == {"stock": 1, "expense": 1, "asset": 0}

    # The second receipt counts its own lines, never the ones received before it.
    key = str(uuid.uuid4())
    r = await _post(client, auth, bill,
                    {"source_line_id": ids[1], "quantity_received": 2},
                    {"source_line_id": ids[3], "quantity_received": 1}, idempotency_key=key)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["line_counts"] == {"stock": 1, "expense": 0, "asset": 1}

    # A retry answers exactly as the receipt did.
    again = await _post(client, auth, bill,
                        {"source_line_id": ids[1], "quantity_received": 2},
                        {"source_line_id": ids[3], "quantity_received": 1}, idempotency_key=key)
    assert again.status_code == 200, again.text
    assert again.json() == first


async def test_receive_summary_toast(client, session, auth, monkeypatch):
    from ui.routes import documents

    bill, ids = await _mixed_bill(client, session, auth)

    async def receive_po(_tok, entity_id, data):
        return api_client._raise(await client.post(
            f"/docs/{entity_id}/receive", headers=auth["headers"], json=data)).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "receive_po", receive_po)
    routes = _Routes()
    documents.setup_routes(routes)
    handler = routes.routes[("post", "/docs/{entity_id}/receive")]

    resp = await handler(_Request([("qty_0", "2"), ("line_id_0", ids[0]), ("qty_1", "2"), ("line_id_1", ids[1]),
                                   ("qty_2", "1"), ("line_id_2", ids[2]), ("location_id", "")]), bill)
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == f"/docs/{bill}"
    toast = json.loads(resp.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == "2 lines added stock. 1 expense line added no stock."

    resp = await handler(_Request([("qty_3", "1"), ("line_id_3", ids[3]), ("location_id", "")]), bill)
    toast = json.loads(resp.headers["HX-Trigger"])["celerpToast"]
    # Nothing in it claims an entry was posted or booked.
    assert toast["message"] == "1 asset line added no stock."


def test_receive_summary_wording():
    from ui.routes.documents import _receive_summary

    assert _receive_summary({"stock": 1, "expense": 0, "asset": 0}) == "1 line added stock."
    assert _receive_summary({"stock": 0, "expense": 2, "asset": 3}) == (
        "2 expense lines and 3 asset lines added no stock.")
    assert _receive_summary({"stock": 3, "expense": 1, "asset": 1}) == (
        "3 lines added stock. 1 expense line and 1 asset line added no stock.")
    # A receipt that reported no counts says nothing rather than guessing.
    assert _receive_summary(None) == ""


def test_toast_survives_redirect():
    from ui.components.shell import _CLIENT_JS

    # A toast sent with a redirect is kept for the page the redirect opens.
    assert "HX-Redirect" in _CLIENT_JS and "celerp_pending_toast" in _CLIENT_JS


@pytest.mark.parametrize("key", [
    "documents.receive_summary_stock_one", "documents.receive_summary_stock_many",
    "documents.receive_summary_expense_one", "documents.receive_summary_expense_many",
    "documents.receive_summary_asset_one", "documents.receive_summary_asset_many",
    "documents.receive_summary_no_stock", "documents.receive_summary_no_stock_two",
])
def test_receive_summary_copy_in_every_locale(key):
    from ui import i18n
    for code in i18n.available_langs():
        assert i18n.t(key, code) != key, (key, code)
