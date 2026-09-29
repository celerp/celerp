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


# ── Selection: snapshot, precedence, repricing ─────────────────────────────

@pytest.mark.asyncio
async def test_list_contact_selection_snapshots_customer_and_reprices_at_patch_version(ui_client):
    # A read after the patch would see version 12 (another user's newer customer); the
    # reprice must pin 11, the version this patch produced, so a stale reprice is refused.
    pre, newer = _list(version=10), _list(version=12, contact_id="contact:bob")
    with (
        patch("ui.api_client.get_list", new=AsyncMock(side_effect=[pre, newer])) as mock_get,
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=_CONTACT)),
        patch("ui.api_client.patch_list", new=AsyncMock(return_value={"version": 11})) as mock_patch,
        patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        patch("ui.api_client.reprice_list", new=AsyncMock(return_value={"ok": True})) as mock_reprice,
    ):
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id", data={"value": "contact:alice"},
                                  cookies=_authed())
    assert r.status_code == 204
    # Whole page refresh so customer details and repriced lines update together.
    assert r.headers["HX-Redirect"] == "/lists/list:Q-1"
    sent = mock_patch.await_args.args[2]
    assert sent == {
        "contact_id": "contact:alice",
        "contact_name": "Alice",
        "contact_company_name": "Acme Corp",
        "contact_email": "alice@acme.example",
        "contact_phone": "555-0001",
        "contact_tax_id": "TX-1",
        "contact_billing_address": "Default billing",
        "contact_shipping_address": "Default shipping",
        "shipping_attn": "Dock B",
        "currency": "EUR",
    }
    # The price list is not a header write: the repricer owns header + lines.
    assert mock_reprice.await_args.args[1:] == ("list:Q-1", "Wholesale", 11)
    assert mock_get.await_count == 1


@pytest.mark.asyncio
async def test_list_contact_without_price_list_falls_back_to_company_default(ui_client):
    contact = {k: v for k, v in _CONTACT.items() if k != "price_list"}
    with (
        patch("ui.api_client.get_list", new=AsyncMock(return_value=_list())),
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=contact)),
        patch("ui.api_client.patch_list", new=AsyncMock(return_value={"version": 12})),
        patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        patch("ui.api_client.reprice_list", new=AsyncMock(return_value={"ok": True})) as mock_reprice,
    ):
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id", data={"value": "contact:alice"},
                                  cookies=_authed())
    assert r.status_code == 204
    assert mock_reprice.await_args.args[1:] == ("list:Q-1", "Retail", 12)


@pytest.mark.asyncio
@pytest.mark.parametrize("list_type", ["transfer", "audit", "writeoff", "shipping_doc"])
async def test_non_money_list_takes_customer_but_never_reprices(ui_client, list_type):
    with (
        patch("ui.api_client.get_list", new=AsyncMock(return_value=_list(list_type=list_type))),
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=_CONTACT)),
        patch("ui.api_client.patch_list", new=AsyncMock(return_value={"version": 11})) as mock_patch,
        patch("ui.api_client.get_default_price_list", new=AsyncMock(return_value="Retail")),
        patch("ui.api_client.reprice_list", new=AsyncMock()) as mock_reprice,
    ):
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id", data={"value": "contact:alice"},
                                  cookies=_authed())
    assert r.status_code == 204
    sent = mock_patch.await_args.args[2]
    assert sent["contact_name"] == "Alice"
    assert "currency" not in sent and "price_list" not in sent
    mock_reprice.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_reprice_error_is_reported(ui_client):
    """A rejected reprice (stale version, missing price permission) surfaces, never a silent redirect."""
    from ui.api_client import APIError
    with (
        patch("ui.api_client.get_list", new=AsyncMock(return_value=_list())),
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=_CONTACT)),
        patch("ui.api_client.patch_list", new=AsyncMock(return_value={"version": 11})),
        patch("ui.api_client.reprice_list", new=AsyncMock(side_effect=APIError(403, "Missing permission: set_sales_doc_prices"))),
    ):
        r = await ui_client.patch("/lists/list:Q-1/field/contact_id", data={"value": "contact:alice"},
                                  cookies=_authed())
    assert "HX-Redirect" not in r.headers
    assert "set_sales_doc_prices" in r.headers["HX-Trigger"]


# ── One canonical snapshot for documents and Lists ─────────────────────────

def test_contact_snapshot_address_precedence():
    from ui.routes.documents import _contact_snapshot

    snap = _contact_snapshot(_CONTACT)
    assert snap["contact_billing_address"] == "Default billing"
    assert snap["contact_shipping_address"] == "Default shipping"
    assert snap["shipping_attn"] == "Dock B"

    no_default = {**_CONTACT, "addresses": [a for a in _CONTACT["addresses"] if not a.get("is_default")]}
    snap = _contact_snapshot(no_default)
    assert snap["contact_billing_address"] == "First billing"
    assert snap["contact_shipping_address"] == "First shipping"
    assert snap["shipping_attn"] == "Dock A"

    legacy = {**_CONTACT, "addresses": []}
    snap = _contact_snapshot(legacy)
    assert snap["contact_billing_address"] == "Legacy billing"
    assert snap["contact_shipping_address"] == "Legacy shipping"
    assert snap["shipping_attn"] == ""


@pytest.mark.asyncio
async def test_document_contact_selection_uses_the_same_snapshot(ui_client):
    from ui.routes.documents import _contact_snapshot

    doc = {"entity_id": "doc:INV-1", "doc_type": "invoice", "status": "draft", "version": 5,
           "issue_date": "2026-01-01"}
    # After the patch (version 6) another user's contact change moved the document to 7.
    newer = {**doc, "version": 7, "contact_id": "contact:bob"}
    with (
        patch("ui.api_client.get_doc", new=AsyncMock(side_effect=[doc, newer, newer])),
        patch("ui.api_client.get_contact", new=AsyncMock(return_value=_CONTACT)),
        patch("ui.api_client.get_payment_terms", new=AsyncMock(return_value=[{"name": "Net 30", "days": 30}])),
        patch("ui.api_client.patch_doc", new=AsyncMock(return_value={"event_id": 6, "version": 6})) as mock_patch,
        patch("ui.api_client.reprice_doc", new=AsyncMock(return_value={"ok": True})) as mock_reprice,
    ):
        r = await ui_client.patch("/docs/doc:INV-1/field/contact_id", data={"value": "contact:alice"},
                                  cookies=_authed())
    assert r.status_code == 204
    sent = mock_patch.await_args.args[2]
    for key, value in _contact_snapshot(_CONTACT).items():
        assert sent[key] == value
    assert sent["payment_terms"] == "Net 30"
    assert sent["due_date"] == "2026-01-31"
    assert sent["currency"] == "EUR"
    # Repriced at the version the contact patch produced, never a later read.
    assert mock_reprice.await_args.args[1:] == ("doc:INV-1", "Wholesale", 6)
    assert mock_reprice.await_count == 1
