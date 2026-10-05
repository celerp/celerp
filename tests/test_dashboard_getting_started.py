# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard's "Bring in your data" card and the "Finish setup" banner.

A new company's owner sees one card that points at the list pages where Import
lives, plus the restore and move options. It goes away once the company holds real
data, or when dismissed. A company left without a business type shows a banner to
the people who can set it. The API is stubbed at ui.api_client.
"""

from __future__ import annotations

import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from test_helpers import authed_cookies

pytestmark = pytest.mark.asyncio

_SOURCES = [{"key": "manager_io", "display_name": "Manager.io", "artifacts": []}]
_DEMO_ITEM = {"id": "item:demo-1", "sku": "DEMO-001", "name": "[DEMO] Sample Item"}
_SELF_CONTACT = {"id": "contact:self", "name": "Pat Owner", "is_self": True}
_EMPTY = {"items": [], "total": 0}


@pytest.fixture()
async def ui():
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://testserver", follow_redirects=False) as c:
        yield c


class _Dash:
    """The API as the dashboard sees it for one company."""

    def __init__(self, *, settings=None, vertical="retail", items=None, contacts=None, docs=None,
                 company_id="c1"):
        self.company = {"id": company_id, "name": "Acme Trading", "vertical": vertical,
                        "currency": "EUR", "settings": dict(settings or {})}
        if vertical:
            self.company["settings"].setdefault("vertical", vertical)
        self.list_items = AsyncMock(return_value=items or {"items": [_DEMO_ITEM], "total": 1})
        self.list_contacts = AsyncMock(return_value=contacts or {"items": [_SELF_CONTACT], "total": 1})
        self.list_docs = AsyncMock(return_value=docs or _EMPTY)
        self.patch_company = AsyncMock(return_value={})
        self._stack = ExitStack()

    def __enter__(self):
        for target, mock in (
            ("ui.api_client.get_company", AsyncMock(return_value=self.company)),
            ("ui.api_client.get_valuation", AsyncMock(return_value={})),
            ("ui.api_client.get_doc_summary", AsyncMock(return_value={})),
            ("ui.api_client.get_dashboard_kpis", AsyncMock(return_value={})),
            ("ui.api_client.my_companies", AsyncMock(return_value={"items": [self.company], "total": 1})),
            ("ui.api_client.get_ar_aging", AsyncMock(return_value={"buckets": {}})),
            ("ui.api_client.get_activity", AsyncMock(return_value=[])),
            ("ui.api_client.migration_sources", AsyncMock(return_value=_SOURCES)),
            ("ui.api_client.list_items", self.list_items),
            ("ui.api_client.list_contacts", self.list_contacts),
            ("ui.api_client.list_docs", self.list_docs),
            ("ui.api_client.patch_company", self.patch_company),
        ):
            self._stack.enter_context(patch(target, new=mock))
        return self

    def __exit__(self, *exc):
        self._stack.close()


async def _dashboard(ui, role="owner", **kw) -> str:
    with _Dash(**kw):
        r = await ui.get("/dashboard", cookies=authed_cookies(role=role))
    assert r.status_code == 200, r.status_code
    return r.text


def _card(html: str) -> str:
    """The card's markup, or "" when the dashboard has none."""
    m = re.search(r'<div[^>]*id="getting-started-card".*?<!-- /getting-started-card -->', html, re.S)
    return m.group(0) if m else ""


async def test_card_shows_for_new_company_with_permitted_links(ui):
    card = _card(await _dashboard(ui))
    assert card, "a new company's owner sees the card"
    for href in ("/inventory?hint=import", "/contacts/customers?hint=import", "/docs?hint=import"):
        assert f'href="{href}"' in card
    assert "Import is always at the top right of the page." in card
    assert ("Items marked [DEMO] are samples. Your first product import removes them unless you "
            "have changed them.") in card


async def test_card_links_follow_import_permissions(ui):
    # import_export_data taken from admin: no Products or Customers link, Documents stays
    # (its list page shows Import to anyone who can edit documents).
    no_import = {"role_grants": {"import_export_data": ["owner"]}}
    card = _card(await _dashboard(ui, role="admin", settings=no_import))
    assert card
    assert "/inventory?hint=import" not in card
    assert "/contacts/customers?hint=import" not in card
    assert 'href="/docs?hint=import"' in card
    # A read-only role gets no card at all.
    assert _card(await _dashboard(ui, role="viewer")) == ""


@pytest.mark.parametrize("kind", ["product", "contact", "document"])
async def test_card_hidden_when_company_has_real_data(ui, kind):
    real = {
        "product": {"items": {"items": [_DEMO_ITEM, {"id": "item:1", "sku": "SKU-1", "name": "Teak chair"}],
                              "total": 2}},
        "contact": {"contacts": {"items": [_SELF_CONTACT, {"id": "contact:2", "name": "Buyer Ltd"}],
                                 "total": 2}},
        "document": {"docs": {"items": [{"id": "doc:1"}], "total": 1}},
    }[kind]
    assert _card(await _dashboard(ui, **real)) == ""


async def test_demo_records_do_not_count_as_data(ui):
    demo_set = {"items": [dict(_DEMO_ITEM, id=f"item:d{i}", sku=f"DEMO-AGR-00{i}") for i in range(1, 8)],
                "total": 7}
    assert _card(await _dashboard(ui, items=demo_set))


async def test_card_dismiss_persists_per_company(ui):
    with _Dash() as dash:
        r = await ui.post("/dashboard/getting-started/dismiss", cookies=authed_cookies(role="owner"))
    assert r.status_code == 200
    dash.patch_company.assert_awaited_once()
    assert dash.patch_company.await_args.args[1] == {"getting_started_dismissed": True}
    assert 'id="getting-started-card"' not in r.text
    # The dismissal is a company setting: the company that dismissed shows no card,
    # another company still does.
    assert _card(await _dashboard(ui, settings={"getting_started_dismissed": True})) == ""
    assert _card(await _dashboard(ui, company_id="c2"))


async def test_dismiss_key_is_sent_as_a_company_setting():
    """patch_company only forwards allowlisted settings keys; the dismissal must be one."""
    import ui.api_client as api

    sent = []

    def _resp(method, url, body):
        return httpx.Response(200, json=body, request=httpx.Request(method, f"http://api{url}"))

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def patch(self, url, json):
            sent.append((url, json))
            return _resp("PATCH", url, {})

        async def get(self, url):
            return _resp("GET", url, {"settings": {}})

    with patch.object(api, "_api_client", lambda *a, **k: _Client()):
        await api.patch_company("tok", {"getting_started_dismissed": True})
    assert sent == [("/companies/me", {"settings": {"getting_started_dismissed": True}})]


async def test_card_restore_and_move_options(ui):
    card = _card(await _dashboard(ui))
    assert 'href="/settings/restore-backup"' in card
    assert 'href="/setup/new-company/migrate"' in card
    assert "Currently supported: Manager.io" in card
    # Restore and move both start owner-only wizards, so an admin's card leaves them out.
    admin_card = _card(await _dashboard(ui, role="admin"))
    assert admin_card
    assert "/settings/restore-backup" not in admin_card
    assert "/setup/new-company/migrate" not in admin_card


@pytest.mark.parametrize("path, needle", [
    ("/settings/restore-backup", 'accept=".celerp-company"'),
    ("/setup/new-company/migrate", 'name="files"'),
])
async def test_card_restore_and_move_links_open_working_wizards(ui, path, needle):
    with _Dash():
        r = await ui.get(path, cookies=authed_cookies(role="owner"))
    assert r.status_code == 200
    assert needle in r.text


async def test_finish_setup_banner_when_business_type_missing(ui):
    html = await _dashboard(ui, vertical="")
    assert "Finish setup: choose your business type" in html
    assert 'href="/setup/company"' in html
    # Only the owner can set the business type; an admin sees no banner.
    assert "Finish setup: choose your business type" not in await _dashboard(ui, role="admin", vertical="")


async def test_no_banner_when_business_type_set(ui):
    html = await _dashboard(ui)
    assert "Finish setup: choose your business type" not in html


async def test_upgraded_company_sees_nothing_new(ui):
    """A 2.5.3 company: real data, a business type, no onboarding keys."""
    with _Dash(items={"items": [{"id": "item:1", "sku": "R-1", "name": "Ring"}], "total": 240},
               contacts={"items": [{"id": "contact:1", "name": "Buyer"}], "total": 31},
               docs={"items": [{"id": "doc:1"}], "total": 412}):
        r = await ui.get("/dashboard", cookies=authed_cookies(role="owner"))
    assert r.status_code == 200
    assert _card(r.text) == ""
    assert "Finish setup" not in r.text
