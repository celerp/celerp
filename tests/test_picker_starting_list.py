# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The pickers on a document show something to choose from before anything is typed.

The line-item picker opens on a starting list in the inventory list's default order, and the
contact picker says so when its contacts could not be loaded or when there are none of that
kind, instead of showing a blank list.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml

from test_form_submitted_twice import _Request, routes  # noqa: F401  (routes is a fixture)
from ui.api_client import APIError


@pytest.mark.asyncio
async def test_an_empty_picker_offers_the_newest_items_first_up_to_the_limit(client, auth, routes):
    from ui.routes.documents import _PICKER_START_LIMIT

    tag = uuid.uuid4().hex[:6]
    for i in range(_PICKER_START_LIMIT + 5):
        r = await client.post("/items", headers=auth["headers"], json={
            "status": "available", "sku": f"SL-{tag}-{i:02d}", "name": f"Start {tag} {i:02d}",
            "quantity": 1, "sell_by": "piece"})
        assert r.status_code == 200, r.text

    resp = await routes[("get", "/docs/catalog-search")](_Request(query={"q": ""}))

    assert resp.status_code == 200
    offered = json.loads(resp.body)
    assert len(offered) == _PICKER_START_LIMIT
    # The inventory list's default order: most recently updated first.
    mine = [o["sku"] for o in offered if o["sku"].startswith(f"SL-{tag}")]
    assert mine == [f"SL-{tag}-{i:02d}" for i in range(_PICKER_START_LIMIT + 4, 4, -1)]


@pytest.mark.asyncio
async def test_a_typed_picker_search_still_filters(client, auth, routes):
    tag = uuid.uuid4().hex[:6]
    for name in (f"Alpha {tag}", f"Beta {tag}"):
        r = await client.post("/items", headers=auth["headers"], json={
            "status": "available", "sku": name.replace(" ", "-"), "name": name, "quantity": 1, "sell_by": "piece"})
        assert r.status_code == 200, r.text

    resp = await routes[("get", "/docs/catalog-search")](_Request(query={"q": f"Alpha {tag}"}))

    assert [o["description"] for o in json.loads(resp.body)] == [f"Alpha {tag}"]


@pytest.mark.asyncio
async def test_the_line_fields_open_the_list_on_focus_and_click(client, auth):
    from ui.routes import documents

    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": []})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=auth["headers"])).json()

    page = to_xml(documents._doc_detail(doc))

    for field in ("sku", "description"):
        assert f"onfocus=\"celerpAcSearch(this,&#x27;{field}&#x27;)\"" in page or \
            f"onfocus=\"celerpAcSearch(this,'{field}')\"" in page
        assert f"onclick=\"celerpAcSearch(this,&#x27;{field}&#x27;)\"" in page or \
            f"onclick=\"celerpAcSearch(this,'{field}')\"" in page


# ── The contact picker ────────────────────────────────────────────────────

async def _picker(contacts=None, error=None, *, contact_type="customer", field="contact_id"):
    from ui.routes import documents

    lookup = AsyncMock(side_effect=error) if error else AsyncMock(return_value={"items": contacts or []})
    with patch("ui.api_client.list_contacts", new=lookup):
        picker = await documents._contact_picker(
            "token", resource="doc", entity_id="doc:INV-1", field=field, value="", contact_type=contact_type)
    return to_xml(picker), lookup


@pytest.mark.asyncio
async def test_a_failed_contact_load_is_an_error_not_an_empty_list():
    html, _ = await _picker(error=APIError(503, "unavailable"))

    assert "Contacts could not be loaded" in html
    assert "Try again" in html
    assert "/docs/doc:INV-1/field/contact_id/edit" in html  # retry re-opens the picker
    assert "Add new contact" not in html  # no options at all, so nothing to mistake for an empty company


@pytest.mark.asyncio
async def test_a_company_with_no_customers_is_told_so():
    html, _ = await _picker([])

    assert "No customers yet" in html
    assert "Add new contact" in html  # the way out is named, and still offered as an option


@pytest.mark.asyncio
async def test_a_company_with_no_vendors_is_told_so_on_the_company_name_picker():
    html, _ = await _picker([], contact_type="vendor", field="contact_company_name")

    assert "No vendors yet" in html


@pytest.mark.asyncio
async def test_the_picker_with_contacts_has_no_empty_note_and_keeps_the_preload_at_500():
    contacts = [{"entity_id": "contact:1", "name": "Alice", "company_name": "Co"}]

    html, lookup = await _picker(contacts)

    assert "Alice" in html
    assert "No customers yet" not in html and "Contacts could not be loaded" not in html
    assert lookup.await_args.args[1] == {"limit": 500, "contact_type": "customer"}
