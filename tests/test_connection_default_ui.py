# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The standing fund per connection on the connection's page in Settings > Connectors, and the
tag a document filled from it carries: shown with who set it, read-only without the fund
permission, "Inactive" once its setter lost it, saved in place with Try again on a refusal."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from test_ui import _company_stub, _mock_get_company  # noqa: F401  (autouse fixture: the company settings)

pytestmark = pytest.mark.asyncio

_FUNDS = {"items": [{"id": "fund:r", "name": "Building fund", "archived": False}]}
_USERS = {"items": [{"id": "u-1", "name": "Dana Treasurer", "email": "d@example.com"}]}
_PEER = "0b5c3a52-0000-4000-8000-000000000001"


def _items(*, choice: str | None = "fund:r", active: bool = True) -> dict:
    return {"items": [
        {"origin": "connected_company", "source_key": _PEER, "label": "Paper Supplies Ltd",
         "fund_choice": choice, "set_by": "u-1" if choice else None,
         "set_at": "2026-11-10T09:00:00+00:00" if choice else None, "version": 2, "active": active},
        {"origin": "accounting_system", "source_key": "quickbooks", "label": "QuickBooks",
         "fund_choice": None, "set_by": None, "set_at": None, "version": 0, "active": False},
    ]}


def _company(*, funds: bool = True, role: str = "owner", grants: dict | None = None) -> dict:
    settings = {"funds_enabled": True} if funds else {}
    if grants:
        settings["role_grants"] = grants
    return {"id": "00000000-0000-0000-0000-000000000002", "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01",
            "settings": settings, "current_role": role}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _page_patches(company: dict, items: dict):
    from ui.routes import settings_connectors as routes
    return (patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))),
            patch("ui.api_client.list_connection_defaults", new=AsyncMock(return_value=items)),
            patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)),
            patch("ui.api_client.get_users", new=AsyncMock(return_value=_USERS)),
            patch.object(routes, "_fetch_catalog", AsyncMock(return_value=([], "", False))),
            patch.object(routes, "_get_connector_config", AsyncMock(return_value=None)),
            patch.object(routes, "_entity_runs", AsyncMock(return_value={})),
            patch.object(routes, "_attention", AsyncMock(return_value=[])),
            patch.object(routes, "_first_sync_summary", AsyncMock(return_value="")),
            patch.object(routes, "_from_accountant_section", AsyncMock(return_value="")))


async def _page(ui_client, platform: str, *, company: dict | None = None, items: dict | None = None) -> str:
    patches = _page_patches(company or _company(), items or _items())
    for p in patches:
        p.start()
    try:
        r = await ui_client.get(f"/settings/connectors/{platform}")
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200, r.text
    return r.text


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


async def test_connection_page_shows_default_fund_with_setter(ui_client):
    html = await _page(ui_client, "celerp")
    text = _text(html)
    assert "Default fund for documents from Paper Supplies Ltd" in text
    assert "Building fund" in text and "Set by Dana Treasurer on 2026-11-10" in text
    assert "QuickBooks" not in text, "a connection shows only its own sources"
    # Click to edit, and Clear while a default is set.
    assert 'hx-get="/settings/connectors/celerp/fund-default?' in html
    assert ">Clear<" in html
    # QuickBooks shows its own default, at -- while none is set, with no Clear.
    qb = await _page(ui_client, "quickbooks")
    qb_text = _text(qb)
    assert "Default fund for documents from QuickBooks" in qb_text and "--" in qb_text
    assert "Paper Supplies Ltd" not in qb_text and ">Clear<" not in qb


async def test_connection_default_hidden_without_funds(ui_client):
    text = _text(await _page(ui_client, "celerp", company=_company(funds=False)))
    assert "Default fund for documents from" not in text


async def test_connection_default_read_only_and_inactive(ui_client):
    # A role without the fund permission reads the default with the reason it can't change it.
    viewer = _company(role="admin", grants={"manage_accounting": ["owner"]})
    html = await _page(ui_client, "celerp", company=viewer)
    assert "Only users who can manage funds can change this" in _text(html)
    assert "fund-default?" not in html and ">Clear<" not in html
    # A default whose setter lost the permission says so.
    inactive = _text(await _page(ui_client, "celerp", items=_items(active=False)))
    assert "Inactive: Dana Treasurer can no longer choose funds" in inactive


async def test_connection_default_saves_in_place_and_keeps_a_refused_choice(ui_client):
    from ui.api_client import APIError

    form = {"origin": "connected_company", "source_key": _PEER, "value": "fund:r",
            "expected_version": "2", "request_key": "rk-1"}
    saved = AsyncMock(return_value={"fund_choice": "fund:r", "set_by": "u-1", "version": 3})
    with patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(_company()))), \
         patch("ui.api_client.list_connection_defaults", new=AsyncMock(return_value=_items())), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_users", new=AsyncMock(return_value=_USERS)), \
         patch("ui.api_client.set_connection_default", new=saved):
        r = await ui_client.post("/settings/connectors/celerp/fund-default", data=form)
        assert r.status_code == 200, r.text
        saved.assert_awaited_once()
        assert saved.await_args.args[1] == {"origin": "connected_company", "source_key": _PEER, "value": "fund:r",
                                            "expected_version": 2, "request_key": "rk-1"}
        assert "HX-Trigger" in r.headers and "Building fund" in _text(r.text)

        # Clear sends no value.
        saved.reset_mock()
        await ui_client.post("/settings/connectors/celerp/fund-default", data={**form, "value": ""})
        assert saved.await_args.args[1]["value"] is None

        # A stale copy is refused in place: the choice is kept, with the reason and Try again.
        stale = AsyncMock(side_effect=APIError(409, {"message": "Someone changed this default fund after you "
                                                                "opened it. Try again to see the current one."}))
        with patch("ui.api_client.set_connection_default", new=stale):
            r = await ui_client.post("/settings/connectors/celerp/fund-default", data=form)
    assert r.status_code == 200
    text = _text(r.text)
    assert "Someone changed this default fund" in text and "Try again" in text
    assert re.search(r'<input type="hidden" name="value"[^>]*value="fund:r"', r.text), "the refused choice is kept"


async def test_document_filled_from_default_is_tagged(ui_client):
    doc = {"entity_id": "doc:b1", "doc_type": "bill", "status": "draft", "currency": "USD", "version": 7,
           "origin": "connected_company", "fund_choice": "fund:r",
           "fund_choice_source": f"default:connected_company:{_PEER}",
           "line_items": [{"line_id": "L1", "description": "Paper", "quantity": 1, "unit_price": 40.0,
                           "line_total": 40.0, "origin": "connected_company"}], "total": 40.0}
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(_company()))):
        r = await ui_client.get("/docs/doc:b1")
    assert r.status_code == 200, r.text
    header = re.search(r'<div[^>]*id="doc-fund"[^>]*>(.*?)</div>', r.text, re.S)
    assert header and "Fund from your default for a connected company" in _text(header.group(1)), header
    # A choice a user made reads From <source> as before.
    doc["fund_choice_source"] = "user"
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(_company()))):
        r = await ui_client.get("/docs/doc:b1")
    assert "Fund from your default" not in r.text
