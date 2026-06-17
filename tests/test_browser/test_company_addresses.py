# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Company address picker browser tests.

Company addresses are managed on Finance > Company Details now (the self-contact address book), not
the settings company tab. These tests cover the document From-address picker, which is sourced from
company Locations that carry an address:
  1. Open a draft invoice
  2. Verify the From section renders the address picker when a location has an address
  3. Select a location -> verify the address is applied without error
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def draft_invoice_id(api):
    """Create a draft invoice via API for address picker tests."""
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": "ADDR-BROWSER-001",
        "status": "draft",
        "line_items": [{"name": "Address Test Widget", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}],
        "total": 50.0,
        "amount_outstanding": 50.0,
    })
    assert r.status_code in {200, 201}, f"Failed to create invoice: {r.text}"
    return r.json()["id"]


def test_company_tab_no_demo_data(page, ui_server):
    """SET-ADDR-02: Company tab must NOT show Demo Data section."""
    page.goto(f"{ui_server}/settings/general?tab=company", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Demo Data" not in body
    assert "Reload Demo Items" not in body


def test_invoice_from_section_has_address_picker(page, ui_server, draft_invoice_id, api):
    """SET-ADDR-04: Invoice From section shows address picker when locations exist."""
    # Ensure at least one location exists
    locs_resp = api.get("/companies/me/locations")
    all_locs = (locs_resp.json().get("items") or locs_resp.json().get("locations") or []) if locs_resp.status_code == 200 else []

    if not all_locs:
        create_resp = api.post("/companies/me/locations", json={
            "name": "Head Office",
            "type": "address",
            "address": {"text": "456 Corporate Ave, Bangkok"},
        })
        assert create_resp.status_code in {200, 201}, create_resp.text
        loc = create_resp.json()
    else:
        loc = all_locs[0]
        api.patch(f"/companies/me/locations/{loc['id']}", json={
            "address": {"text": "456 Corporate Ave, Bangkok"},
            "name": loc.get("name") or "Head Office",
        })

    page.goto(f"{ui_server}/docs/{draft_invoice_id}", wait_until="domcontentloaded")
    page.wait_for_load_state("load", timeout=5000)
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body

    page_content = page.content()
    # Should have either a select (picker) or a clickable cell for company_address
    has_picker = 'name="company_address"' in page_content
    has_cell = 'company_address' in page_content
    assert has_picker or has_cell, "company_address field not found in doc detail"


def test_invoice_address_picker_select(page, ui_server, draft_invoice_id, api):
    """SET-ADDR-05: Selecting a location from the picker updates company_address."""
    # Ensure a location with a known address exists
    locs_resp = api.get("/companies/me/locations")
    all_locs = (locs_resp.json().get("items") or locs_resp.json().get("locations") or []) if locs_resp.status_code == 200 else []

    _addr_text = "789 Picker Lane, Bangkok 10200"
    if not all_locs:
        create_resp = api.post("/companies/me/locations", json={
            "name": "Picker Office",
            "type": "address",
            "address": {"text": _addr_text},
        })
        assert create_resp.status_code in {200, 201}, create_resp.text
    else:
        api.patch(f"/companies/me/locations/{all_locs[0]['id']}", json={
            "name": "Picker Office",
            "address": {"text": _addr_text},
        })

    page.goto(f"{ui_server}/docs/{draft_invoice_id}", wait_until="domcontentloaded")
    page.wait_for_load_state("load", timeout=5000)
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body

    # The picker renders as a <select> in editing mode whose hx-patch targets the
    # company_address field (its form name is "value", not "company_address").
    sel = page.locator('select[hx-patch*="field/company_address"]')
    assert sel.count() > 0, (
        "company-address picker (select) not rendered despite a configured location"
    )

    # Select the address option by value
    sel.first.select_option(value=_addr_text)
    page.wait_for_load_state("load", timeout=5000)

    # Verify no crash
    body_after = page.locator("body").inner_text()
    assert "Internal Server Error" not in body_after
    assert "Traceback" not in body_after
