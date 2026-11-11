# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A create door says why it could not create, and an ended session at any door opens the
sign-in page with the session cookies cleared. A read a door depends on never turns into an
invented empty value: a failed location read is not "no locations", a failed duplicate check
does not make a second placeholder, a failed settings read is not "no permission"."""
from __future__ import annotations

import html
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import authed_cookies
from ui.api_client import APIError
from ui.components.shell import no_access_text
from ui.i18n import t

pytestmark = pytest.mark.asyncio

_COMPANY = {"id": "co:door", "name": "Door Co", "currency": "USD", "settings": {}}
_LOCATIONS = {"items": [{"id": "loc:1", "name": "Main store"}], "total": 1}


@pytest.fixture(autouse=True)
def _reads():
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])):
        yield


async def _ui(method: str, path: str, role: str = "owner", **kw):
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as ui:
        ui.cookies.update(authed_cookies(role))
        return await getattr(ui, method)(path, **kw)


def _signed_out(r) -> bool:
    """The app's 401 answer: the sign-in page, with the session cookie deleted."""
    target = r.headers.get("HX-Redirect") or r.headers.get("location") or ""
    cleared = any(c.startswith("celerp_token=") and ("Max-Age=0" in c or "expires=" in c.lower())
                  for c in r.headers.get_list("set-cookie"))
    return target.startswith("/login") and cleared


# ── New audit (J3) ─────────────────────────────────────────────────────────


async def test_audit_create_answers_hx_redirect():
    with patch("ui.api_client.get_locations", new=AsyncMock(return_value=_LOCATIONS)):
        page = await _ui("get", "/lists/new-audit")
    assert page.status_code == 200
    assert 'hx-post="/lists/new-audit"' in page.text
    with patch("ui.api_client.create_audit", new=AsyncMock(return_value={"id": "list:A1"})):
        r = await _ui("post", "/lists/new-audit", data={"location_id": "loc:1"}, headers={"HX-Request": "true"})
    assert r.status_code == 204, r.text
    assert r.headers["HX-Redirect"] == "/lists/list:A1"


async def test_audit_create_refusal_is_shown():
    refusal = "You need the Edit documents permission to do this."
    with patch("ui.api_client.create_audit", new=AsyncMock(side_effect=APIError(403, refusal))):
        r = await _ui("post", "/lists/new-audit", data={"location_id": "loc:1"}, headers={"HX-Request": "true"})
    assert r.status_code == 403
    assert r.text == refusal


async def test_audit_create_without_location_is_told():
    create = AsyncMock(return_value={"id": "list:A1"})
    with patch("ui.api_client.create_audit", new=create):
        r = await _ui("post", "/lists/new-audit", data={"location_id": ""}, headers={"HX-Request": "true"})
    assert r.status_code == 400
    assert r.text == t("flash.no_location_selected")
    create.assert_not_called()


async def test_audit_page_sends_ended_session_to_sign_in():
    with patch("ui.api_client.get_locations", new=AsyncMock(side_effect=APIError(401, "Session expired"))):
        r = await _ui("get", "/lists/new-audit")
    assert _signed_out(r), (r.status_code, dict(r.headers))


async def test_audit_page_shows_a_failed_location_load():
    with patch("ui.api_client.get_locations", new=AsyncMock(side_effect=APIError(500, "The location store is unavailable."))):
        r = await _ui("get", "/lists/new-audit")
    body = html.unescape(r.text)
    assert "The location store is unavailable." in body
    assert 'class="flash flash--error"' in r.text
    assert t("documents.audit_no_locations") not in body


# ── Contact create duplicate check (X4a) ───────────────────────────────────


async def test_contact_create_shows_a_failed_duplicate_check():
    create = AsyncMock(return_value={"entity_id": "contact:new"})
    with patch("ui.api_client.list_contacts", new=AsyncMock(side_effect=APIError(500, "Contacts could not be read."))), \
         patch("ui.api_client.create_contact", new=create):
        r = await _ui("post", "/contacts/create?type=customer", headers={"HX-Request": "true"})
    assert r.status_code == 500
    assert r.text == "Contacts could not be read."
    create.assert_not_called()
    with patch("ui.api_client.list_contacts", new=AsyncMock(side_effect=APIError(401, "Session expired"))), \
         patch("ui.api_client.create_contact", new=create):
        r = await _ui("post", "/contacts/create?type=customer", headers={"HX-Request": "true"})
    assert _signed_out(r), (r.status_code, dict(r.headers))
    create.assert_not_called()


# ── Journal form settings read (X4b) ───────────────────────────────────────


def _journal_reads(company):
    return (patch("ui.api_client.get_company", new=company),
            patch("ui.api_client.get_chart", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})))


async def test_journal_form_sends_ended_session_to_sign_in():
    a, b, c = _journal_reads(AsyncMock(side_effect=APIError(401, "Session expired")))
    with a, b, c:
        r = await _ui("get", "/accounting/journal/new?ts=2026-11-01")
    assert _signed_out(r), (r.status_code, dict(r.headers))


async def test_journal_form_says_when_settings_cannot_be_read():
    reason = "Company settings could not be read."
    for role in ("owner", "viewer"):
        # Only the form's settings read fails; later reads (currency, page chrome) answer.
        a, b, c = _journal_reads(AsyncMock(side_effect=[APIError(500, reason)] + [_COMPANY] * 5))
        with a, b, c:
            r = await _ui("get", "/accounting/journal/new?ts=2026-11-01", role=role)
        body = html.unescape(r.text)
        assert (role, reason in body) == (role, True)
        # A permission that was never checked is never claimed missing.
        missing = html.unescape(no_access_text(permission="manage_accounting"))
        assert (role, missing in body) == (role, False)


# ── Bulk duplicate (J4) ────────────────────────────────────────────────────


async def test_bulk_duplicate_names_each_refusal():
    items = {"item:a": {"id": "item:a", "sku": "SKU-A", "name": "Alpha"},
             "item:b": {"id": "item:b", "sku": "SKU-B", "name": "Bravo"}}

    async def _create(token, data):
        if data.get("name") == "Bravo":
            raise APIError(409, "Bravo is archived and cannot be copied.")
        return {"id": "item:copy", "event_id": "e1"}

    with patch("ui.api_client.get_item", new=AsyncMock(side_effect=lambda token, eid: items[eid])), \
         patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": [], "total": 0})), \
         patch("ui.api_client.create_item", new=_create):
        r = await _ui("post", "/api/items/bulk/duplicate",
                      content=b"selected=item%3Aa&selected=item%3Ab",
                      headers={"content-type": "application/x-www-form-urlencoded"})
    body = html.unescape(r.text + json.dumps(dict(r.headers)))
    assert t("inventory.bulk_duplicated_partial", ok=1, failed=1) in body
    assert t("inventory.bulk_duplicate_refusals", items="SKU-B: Bravo is archived and cannot be copied.") in body


# ── Work center (J5) ───────────────────────────────────────────────────────


async def test_work_center_refusal_is_shown():
    refusal = "A work center called New work center already exists."
    with patch("ui.api_client.create_work_center", new=AsyncMock(side_effect=APIError(409, refusal))), \
         patch("ui.api_client.list_work_centers", new=AsyncMock(return_value={"items": []})), \
         patch("ui.api_client.get_locations", new=AsyncMock(return_value=_LOCATIONS)):
        r = await _ui("post", "/manufacturing/work-centers/new", headers={"HX-Request": "true"})
    assert refusal in json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]
    assert 'id="wc-table"' in r.text
    # A failed table read says why where the table was, never an empty table.
    with patch("ui.api_client.create_work_center", new=AsyncMock(return_value={"id": "wc:1"})), \
         patch("ui.api_client.list_work_centers", new=AsyncMock(side_effect=APIError(500, "Work centers could not be read."))), \
         patch("ui.api_client.get_locations", new=AsyncMock(return_value=_LOCATIONS)):
        r = await _ui("post", "/manufacturing/work-centers/new", headers={"HX-Request": "true"})
    assert 'id="wc-table"' in r.text and "Work centers could not be read." in html.unescape(r.text)
    assert 'class="flash flash--error"' in r.text
    for status in (401,):
        with patch("ui.api_client.create_work_center", new=AsyncMock(side_effect=APIError(status, "Session expired"))):
            r = await _ui("post", "/manufacturing/work-centers/new", headers={"HX-Request": "true"})
        assert _signed_out(r), (r.status_code, dict(r.headers))
