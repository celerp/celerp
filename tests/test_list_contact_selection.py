# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Choosing a List customer runs the same contact workflow as a document."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _authed(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(role=role)}


_CONTACTS = [{"entity_id": f"contact:{i}", "name": f"Customer {i}", "company_name": f"Co {i}"}
             for i in range(12)]

_CONTACT = {
    "entity_id": "contact:alice",
    "name": "Alice",
    "company_name": "Acme Corp",
    "email": "alice@acme.example",
    "phone": "555-0001",
    "tax_id": "TX-1",
    "currency": "EUR",
    "price_list": "Wholesale",
    "payment_terms": "Net 30",
    "billing_address": "Legacy billing",
    "shipping_address": "Legacy shipping",
    "addresses": [
        {"address_type": "billing", "full_address": "First billing", "attn": "Billing first"},
        {"address_type": "billing", "full_address": "Default billing", "is_default": True},
        {"address_type": "shipping", "full_address": "First shipping", "attn": "Dock A"},
        {"address_type": "shipping", "full_address": "Default shipping", "attn": "Dock B", "is_default": True},
    ],
}


def _list(list_type: str = "quotation", version: int = 10, **extra) -> dict:
    return {"entity_id": "list:Q-1", "list_type": list_type, "status": "draft",
            "version": version, "line_items": [], "currency": "USD", **extra}


def _company():
    return AsyncMock(return_value={"settings": {}})


# ── Editor: the same searchable customer selector as documents ─────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["contact_id", "contact_company_name"])
async def test_list_contact_edit_renders_document_customer_selector(ui_client, field):
    lists_contacts = AsyncMock(return_value={"items": _CONTACTS, "total": len(_CONTACTS)})
    with (
        patch("ui.api_client.get_company", new=_company()),
        patch("ui.api_client.get_list", new=AsyncMock(return_value=_list(contact_id="contact:3"))),
        patch("ui.api_client.list_contacts", new=lists_contacts),
    ):
        r = await ui_client.get(f"/lists/list:Q-1/field/{field}/edit", cookies=_authed())
    assert r.status_code == 200
    html = r.text
    assert "combobox-wrap" in html
    assert 'type="text" name="value"' not in html
    # Customer-filtered options and server-side search, exactly like a sales document.
    assert lists_contacts.await_args.args[1]["contact_type"] == "customer"
    assert f"/contacts/search-options?contact_type=customer&amp;field={field}" in html
    # Saving goes through the List mutation queue, persisting pending line edits first.
    assert """onchange='_celerpPatchListField(this, "/lists/list:Q-1/field/contact_id", true)'""" in html
    assert "/docs/list:Q-1" not in html


@pytest.mark.asyncio
async def test_list_and_document_editors_render_the_same_selector(ui_client):
    """One shared renderer: only the save wiring differs between a doc and a List."""
    doc = {"entity_id": "doc:INV-1", "doc_type": "invoice", "status": "draft", "contact_id": "contact:3"}
    with (
        patch("ui.api_client.get_company", new=_company()),
        patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
        patch("ui.api_client.get_list", new=AsyncMock(return_value=_list(contact_id="contact:3"))),
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS})),
    ):
        doc_html = (await ui_client.get("/docs/doc:INV-1/field/contact_id/edit", cookies=_authed())).text
        list_html = (await ui_client.get("/lists/list:Q-1/field/contact_id/edit", cookies=_authed())).text
    for html in (doc_html, list_html):
        assert "Customer 11" in html
        assert 'data-value="__new__"' in html
        assert 'value="Customer 3"' in html


@pytest.mark.asyncio
async def test_list_contact_add_new_goes_to_customers(ui_client):
    with patch("ui.api_client.patch_list", new=AsyncMock()) as mock_patch:
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id", data={"value": "__new__"},
                                  cookies=_authed())
    assert r.status_code == 204
    assert r.headers["HX-Redirect"] == "/contacts/customers"
    mock_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_contact_display_shows_name_not_id(ui_client):
    with patch("ui.api_client.get_list", new=AsyncMock(
            return_value=_list(contact_id="contact:alice", contact_name="Alice"))):
        r = await ui_client.get("/lists/list:Q-1/field/contact_id/display", cookies=_authed())
    assert "Alice" in r.text
    assert "contact:alice" not in r.text.replace("/lists/list:Q-1", "")


# ── Selection: the page sends only the contact and the version it shows ──

@pytest.mark.asyncio
async def test_list_contact_selection_sends_only_contact_and_version(ui_client):
    with (
        patch("ui.api_client.patch_list", new=AsyncMock(return_value={"version": 11})) as mock_patch,
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=_CONTACT)) as mock_contact,
        patch("ui.api_client.reprice_list", new=AsyncMock()) as mock_reprice,
    ):
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id",
                                  data={"value": "contact:alice", "expected_version": "10"},
                                  cookies=_authed())
    assert r.status_code == 204
    # Whole page refresh so customer details and repriced lines update together.
    assert r.headers["HX-Redirect"] == "/lists/list:Q-1"
    assert mock_patch.await_args.args[1:] == ("list:Q-1", {"contact_id": "contact:alice"})
    assert mock_patch.await_args.kwargs == {"expected_version": 10}
    # Snapshot, currency and prices are the backend's job, in the same save.
    mock_contact.assert_not_awaited()
    mock_reprice.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_selection_error_is_reported(ui_client):
    """A refused selection (stale version, missing price permission) surfaces, never a silent redirect."""
    from ui.api_client import APIError
    with patch("ui.api_client.patch_list",
               new=AsyncMock(side_effect=APIError(403, "Requires the set_sales_doc_prices permission"))):
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id",
                                  data={"value": "contact:alice", "expected_version": "10"},
                                  cookies=_authed())
    assert "HX-Redirect" not in r.headers
    assert "set_sales_doc_prices" in r.headers["HX-Trigger"]


@pytest.mark.asyncio
async def test_document_contact_selection_sends_only_contact_and_version(ui_client):
    doc = {"entity_id": "doc:INV-1", "doc_type": "invoice", "status": "draft", "version": 6}
    with (
        patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=_CONTACT)) as mock_contact,
        patch("ui.api_client.patch_doc", new=AsyncMock(return_value={"event_id": 6, "version": 6})) as mock_patch,
        patch("ui.api_client.reprice_doc", new=AsyncMock()) as mock_reprice,
    ):
        r = await ui_client.patch("/docs/doc:INV-1/field/contact_id",
                                  data={"value": "contact:alice", "expected_version": "5"},
                                  cookies=_authed())
    assert r.status_code == 204
    assert r.headers["HX-Redirect"] == "/docs/doc:INV-1"
    assert mock_patch.await_args.args[1:] == ("doc:INV-1", {"contact_id": "contact:alice"})
    assert mock_patch.await_args.kwargs == {"expected_version": 5}
    mock_contact.assert_not_awaited()
    mock_reprice.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_pickers_post_the_page_version(ui_client):
    doc = {"entity_id": "doc:INV-1", "doc_type": "invoice", "status": "draft", "contact_id": "contact:3"}
    with (
        patch("ui.api_client.get_company", new=_company()),
        patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)),
        patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": _CONTACTS})),
    ):
        html = (await ui_client.get("/docs/doc:INV-1/field/contact_id/edit", cookies=_authed())).text
    assert "js:{expected_version: window._celerpEntityVersion}" in html


# ── One canonical snapshot for documents and Lists ─────────────────────────

def test_contact_snapshot_address_precedence():
    from celerp_contacts.references import contact_snapshot

    snap = contact_snapshot(_CONTACT)
    assert snap["contact_name"] == "Alice"
    assert snap["contact_billing_address"] == "Default billing"
    assert snap["contact_shipping_address"] == "Default shipping"
    assert snap["shipping_attn"] == "Dock B"

    no_default = {**_CONTACT, "addresses": [a for a in _CONTACT["addresses"] if not a.get("is_default")]}
    snap = contact_snapshot(no_default)
    assert snap["contact_billing_address"] == "First billing"
    assert snap["contact_shipping_address"] == "First shipping"
    assert snap["shipping_attn"] == "Dock A"

    legacy = {**_CONTACT, "addresses": []}
    snap = contact_snapshot(legacy)
    assert snap["contact_billing_address"] == "Legacy billing"
    assert snap["contact_shipping_address"] == "Legacy shipping"
    assert snap["shipping_attn"] == ""

    structured = {**_CONTACT, "addresses": [
        {"address_type": "billing", "line1": "1 Main St", "city": "Springfield", "is_default": True},
    ]}
    assert "1 Main St" in contact_snapshot(structured)["contact_billing_address"]
