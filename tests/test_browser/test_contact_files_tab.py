# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A contact page offers its Documents tab only while the company can open Documents. With
Documents turned off the page opens on the contact's own Files tab, so its files stay
reachable, and turning Documents back on brings the Documents tab back."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


def _ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


def _company_modules(company_id: str, names: list[str] | None) -> None:
    """Give the company its own module list (*names*), or None to go back to the installation's.
    Written in place: the installation here lists Connectors, which is part of Celerp and
    always on, and the Modules page refuses to turn Documents off under it."""
    import psycopg2
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if names is None:
                cur.execute("UPDATE companies SET settings = (settings::jsonb - 'enabled_modules')::json "
                            "WHERE id = %s", (company_id,))
            else:
                cur.execute("UPDATE companies SET settings = (settings::jsonb || %s::jsonb)::json WHERE id = %s",
                            (json.dumps({"enabled_modules": names}), company_id))
    finally:
        conn.close()


def _without_documents() -> list[str]:
    """The installation's modules less Documents and every module that needs it."""
    from celerp.config import installation_modules
    from celerp.modules.registry import dependency_closure
    return [n for n in installation_modules() if "celerp-docs" not in dependency_closure([n])]


def test_a_contact_page_follows_whether_the_company_can_open_documents(page: Page, fresh_company, ui_server):
    contact = _ok(fresh_company.post("/crm/contacts", json={"name": "Files Customer", "contact_type": "customer"}))
    _ok(fresh_company.post(f"/crm/contacts/{contact['id']}/files",
                           files={"file": ("price-list.txt", b"prices", "text/plain")}))
    company_id = _ok(fresh_company.get("/companies/me"))["id"]
    _company_modules(company_id, _without_documents())
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
        _company_modules(company_id, None)

    page.goto(f"{ui_server}/contacts/{contact['id']}", wait_until="domcontentloaded")
    expect(page.locator("#tab-documents")).to_have_class("tab active")
    expect(page.locator("#tab-files")).to_have_count(0)
    expect(page.locator(".financial-card").first).to_be_visible()
    expect(page.locator("#tab-content")).to_contain_text("price-list.txt")
