# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A contact page offers its Documents tab only while the company can open Documents. With
Documents turned off the page opens on the contact's own Files tab, so its files stay
reachable, and turning Documents back on brings the Documents tab back."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


def _ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


def _turn_off_documents(api) -> list[str]:
    """Turn Documents off for the company through the Modules API, after each module that
    needs it. Returns the modules turned off, in that order. The new company has no list of
    its own, so it uses the installation's, the same one the API server reads."""
    from celerp.modules.registry import dependents

    needed_by = dependents(None)
    turned_off: list[str] = []

    def off(name: str) -> None:
        for user in needed_by.get(name, []):
            off(user)
        if name not in turned_off:
            _ok(api.post(f"/companies/me/modules/{name}/disable"))
            turned_off.append(name)

    off("celerp-docs")
    return turned_off


def test_a_contact_page_follows_whether_the_company_can_open_documents(page: Page, fresh_company, ui_server):
    contact = _ok(fresh_company.post("/crm/contacts", json={"name": "Files Customer", "contact_type": "customer"}))
    _ok(fresh_company.post(f"/crm/contacts/{contact['id']}/files",
                           files={"file": ("price-list.txt", b"prices", "text/plain")}))
    turned_off = _turn_off_documents(fresh_company)
    try:
        page.goto(f"{ui_server}/contacts/{contact['id']}", wait_until="domcontentloaded")
        expect(page.locator("#tab-files")).to_have_class("tab active")
        expect(page.locator("#tab-documents")).to_have_count(0)
        expect(page.locator(".financial-card")).to_have_count(0)
        content = page.locator("#tab-content")
        expect(content.locator(".file-drop-zone")).to_be_visible()
        expect(content).to_contain_text("price-list.txt")
        expect(page.locator("body")).not_to_contain_text("could not be loaded")

        # Away to Notes and back: the Files tab loads the same files again.
        page.locator("#tab-notes").click()
        expect(content.locator(".file-drop-zone")).to_have_count(0)
        page.locator("#tab-files").click()
        expect(content).to_contain_text("price-list.txt")
    finally:
        for name in reversed(turned_off):
            _ok(fresh_company.post(f"/companies/me/modules/{name}/enable"))

    page.goto(f"{ui_server}/contacts/{contact['id']}", wait_until="domcontentloaded")
    expect(page.locator("#tab-documents")).to_have_class("tab active")
    expect(page.locator("#tab-files")).to_have_count(0)
    expect(page.locator(".financial-card").first).to_be_visible()
    expect(page.locator("#tab-content")).to_contain_text("price-list.txt")
