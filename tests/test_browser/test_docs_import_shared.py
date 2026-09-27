# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Importing a shared document from the documents list: the Import button, a
share link that is not valid, and a downloaded .celerp file end to end."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

OUT = Path("/tmp/playwright_shots/docs_import_shared")


def _no_crash(page):
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body


def test_documents_import_button_opens_shared_import(page, ui_server):
    page.goto(f"{ui_server}/docs?type=invoice", wait_until="domcontentloaded")
    page.get_by_role("link", name="Import", exact=True).click()
    page.wait_for_url("**/docs/import")
    expect(page.locator("#shared-import")).to_be_visible()
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / "import_page.png"), full_page=True)


def test_invalid_share_link_shows_error_and_keeps_input(page, ui_server):
    page.goto(f"{ui_server}/docs/import", wait_until="domcontentloaded")
    link = "https://example.com/not-a-share"
    page.fill("#share-link", link)
    page.locator('form[action="/docs/import/shared"] button[type=submit]').click()
    expect(page.locator("#shared-import .flash--error")).to_contain_text("not a Celerp share link")
    expect(page.locator("#share-link")).to_have_value(link)
    _no_crash(page)


def test_uploaded_celerp_file_becomes_received_document(page, ui_server, api, tmp_path):
    r = api.post("/docs", json={
        "doc_type": "invoice",
        "ref_id": "DOC-SHARED-IMPORT-001",
        "status": "draft",
        "line_items": [{"name": "Shared widget", "quantity": 2, "unit_price": 50.0, "line_total": 100.0}],
        "total": 100.0,
        "amount_outstanding": 100.0,
    })
    assert r.status_code in {200, 201}, r.text
    share = api.post(f"/docs/{r.json()['id']}/share")
    assert share.status_code == 200, share.text
    bundle = api.get(f"/share/{share.json()['token']}/bundle")
    assert bundle.status_code == 200, bundle.text
    path = tmp_path / "shared.celerp"
    path.write_bytes(bundle.content)

    page.goto(f"{ui_server}/docs/import", wait_until="domcontentloaded")
    page.set_input_files("#celerp-file", str(path))
    page.locator('form[action="/docs/import/shared-file"] button[type=submit]').click()
    page.wait_for_url(re.compile(r"/docs/doc:rcv:"))
    _no_crash(page)
    expect(page.locator("body")).to_contain_text("Shared widget")
