# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The page sends Set as available as one request naming lines by their line id.

Selected lines are named by line id, every part-return quantity travels in the same
request, and the request carries the operation key the page minted for it, so a retry
replays instead of acting twice. Memo lines out with the customer render a quantity
field in the units they went out in, one per line, so several can be part-returned at
once. Draft rows carry no shipped quantity."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.api_client import APIError

_L0 = "11111111-1111-4111-8111-111111111111"
_L1 = "22222222-2222-4222-8222-222222222222"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _cookies():
    return {"celerp_token": make_test_token(role="owner")}


@pytest.mark.asyncio
@pytest.mark.parametrize("base,is_list", [("docs", False), ("lists", True)])
async def test_the_proxy_sends_one_request_by_line_id_with_its_key(ui_client, base, is_list):
    sent = AsyncMock(return_value={})
    with patch("ui.api_client.set_lines_available", new=sent):
        r = await ui_client.post(f"/{base}/x:1/set-available", cookies=_cookies(), data={
            "line_id": [_L0, _L1], f"qty[{_L1}]": "2", "idempotency_key": "k-1"})
    assert r.status_code == 204, r.text
    sent.assert_awaited_once()
    args, kwargs = sent.await_args
    assert args[1] == "x:1"
    assert kwargs == {"line_ids": [_L0, _L1], "line_entity_ids": [], "quantities": {_L1: 2.0},
                      "idempotency_key": "k-1:set-available", "is_list": is_list}


@pytest.mark.asyncio
async def test_the_proxy_shows_the_refusal(ui_client):
    refused = AsyncMock(side_effect=APIError(422, {"message_key": "lines.cannot_revert",
                                                   "message": "Cannot take back: S-1: nothing is out on this line",
                                                   "params": {"reasons": "S-1: nothing is out on this line"}}))
    with patch("ui.api_client.set_lines_available", new=refused):
        r = await ui_client.post("/docs/x:1/set-available", cookies=_cookies(),
                                 data={"line_id": [_L0], "idempotency_key": "k"})
    assert r.status_code != 204
    assert "nothing is out on this line" in (r.text + r.headers.get("HX-Trigger", ""))


@pytest.mark.asyncio
async def test_an_action_sent_without_the_pages_key_is_refused_as_out_of_date(ui_client):
    sent = AsyncMock(return_value={})
    with patch("ui.api_client.set_lines_available", new=sent):
        r = await ui_client.post("/docs/x:1/set-available", cookies=_cookies(), data={"line_id": [_L0]})
    sent.assert_not_awaited()
    assert r.status_code != 204


@pytest.mark.asyncio
async def test_the_toolbar_carries_the_pages_key():
    assert re.search(r'id="li-bulk-toolbar"[^>]*data-operation-key="[0-9a-f-]{36}"', _memo()) or \
        re.search(r'data-operation-key="[0-9a-f-]{36}"[^>]*id="li-bulk-toolbar"', _memo())


@pytest.mark.asyncio
async def test_reserve_and_fulfil_proxies_send_line_ids_and_the_key(ui_client):
    reserve = AsyncMock(return_value={})
    fulfil = AsyncMock(return_value={})
    with patch("ui.api_client.reserve_lines", new=reserve), patch("ui.api_client.fulfill_lines", new=fulfil):
        r = await ui_client.post("/docs/x:1/reserve-lines", cookies=_cookies(), data={
            "line_id": [_L0], "new_status": "reserved", "idempotency_key": "k-r"})
        assert r.status_code == 204, r.text
        r = await ui_client.post("/docs/x:1/fulfill-lines", cookies=_cookies(), data={
            "line_id": [_L0], "idempotency_key": "k-f"})
        assert r.status_code == 204, r.text
    assert reserve.await_args.kwargs == {"line_ids": [_L0], "line_entity_ids": [], "new_status": "reserved",
                                         "idempotency_key": "k-r:reserved", "is_list": False}
    assert fulfil.await_args.kwargs == {"line_ids": [_L0], "line_entity_ids": [], "idempotency_key": "k-f:fulfil"}


@pytest.mark.asyncio
async def test_the_api_client_posts_the_selection_quantities_and_key():
    import ui.api_client as api
    posted = []

    class _C:
        async def post(self, url, json=None):
            posted.append((url, json))
            import httpx
            return httpx.Response(200, json={}, request=httpx.Request("POST", "http://api" + url))

    @asynccontextmanager
    async def _fake(token, timeout=10.0):
        yield _C()

    with patch.object(api, "_api_client", _fake):
        await api.set_lines_available("t", "doc:1", line_ids=[_L0], quantities={_L0: 1.5}, idempotency_key="k")
        await api.set_lines_available("t", "list:1", line_ids=[_L0], idempotency_key="k2", is_list=True)
    assert posted == [
        ("/docs/doc:1/set-available", {"line_ids": [_L0], "line_entity_ids": [], "quantities": {_L0: 1.5},
                                        "idempotency_key": "k"}),
        ("/lists/list:1/set-available", {"line_ids": [_L0], "line_entity_ids": [], "quantities": None,
                                          "idempotency_key": "k2"}),
    ]


def _memo(status="sent", item_status="memo_out"):
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:memo-1", "doc_type": "memo", "status": status, "ref_id": "M-1",
           "line_items": [
               {"line_id": _L0, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1",
                "quantity": 3, "sell_by": "carat", "unit_price": 1},
               {"line_id": _L1, "sku": "S-2", "item_id": "item:2", "entity_id": "item:2",
                "quantity": 1, "sell_by": "piece", "unit_price": 1}]}
    return to_xml(_doc_detail(doc, item_status_map={"item:1": item_status, "item:2": item_status}))


def test_finalized_rows_name_their_line_and_offer_a_return_quantity_per_memo_line():
    html = _memo()
    assert f'data-line-id="{_L0}"' in html and f'data-line-id="{_L1}"' in html
    field = re.search(rf'<input[^>]*name="qty\[{_L0}\]"[^>]*>', html)
    assert field, "a memo line out with the customer offers a return quantity"
    assert 'max="3"' in field.group(0) and 'value="3"' in field.group(0)
    assert "carat" in html[field.end():field.end() + 200]
    # Every memo line out with the customer gets its own field, so several part-return at once.
    assert re.search(rf'<input[^>]*name="qty\[{_L1}\]"[^>]*max="1"', html)


def test_rows_not_out_on_memo_offer_no_return_quantity():
    assert 'name="qty[' not in _memo(item_status="reserved")


def test_the_page_sends_one_request_and_never_promise_all():
    html = _memo()
    assert "Promise.all" not in html
    assert "/set-available" in html
    assert "/revert-lines" not in html


def test_draft_rows_carry_no_shipped_quantity():
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:inv-1", "doc_type": "invoice", "status": "draft", "ref_id": "I-1",
           "line_items": [{"line_id": _L0, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1",
                           "quantity": 3, "unit_price": 1}]}
    html = to_xml(_doc_detail(doc))
    assert "data-out-qty" not in html


def test_the_line_quantity_field_is_shared():
    from ui.routes.documents import _line_qty_input
    html = to_xml(_line_qty_input("qty[a]", 2.5, "kg", 1))
    tag = re.search(r'<input[^>]*>', html).group(0)
    assert 'name="qty[a]"' in tag and 'max="2.5"' in tag and 'value="1"' in tag and 'type="number"' in tag
    assert "kg" in html


def _draft_toolbar(doc_type: str, entity_id: str):
    from ui.routes.documents import _doc_detail
    d = {"entity_id": entity_id, "doc_type": doc_type, "status": "draft", "ref_id": "D-1",
         "line_items": [{"line_id": _L0, "sku": "S-1", "item_id": "item:1", "entity_id": "item:1",
                         "quantity": 3, "unit_price": 1}]}
    return to_xml(_doc_detail(d, item_status_map={"item:1": "reserved"},
                              item_status_doc_map={"item:1": (entity_id, "D-1")}))


def test_a_draft_document_releases_its_holds_but_never_reserves():
    html = _draft_toolbar("invoice", "doc:inv-1")
    assert 'value="li-revert"' in html
    assert 'value="li-reserve"' not in html


def test_a_draft_list_reserves_and_releases():
    html = _draft_toolbar("list", "list:q-1")
    assert 'value="li-revert"' in html and 'value="li-reserve"' in html
