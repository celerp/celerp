"""
Visual Playwright test for file management UI - screenshot-only, no uploads.
"""
from __future__ import annotations
import os
import pytest
import httpx
from urllib.parse import quote
from playwright.sync_api import Page

pytestmark = pytest.mark.browser

SCREENSHOT_DIR = "/tmp/files_screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

_API_BASE = "http://127.0.0.1:18000"
_UI_BASE = "http://127.0.0.1:18080"


def ss(page: Page, name: str):
    path = f"{SCREENSHOT_DIR}/{name}.png"
    page.screenshot(path=path, full_page=True)
    print(f"\n  SCREENSHOT: {path}")


def test_contact_files_section(page: Page, seeded_user: dict):
    token = seeded_user["access_token"]
    hdrs = {"Authorization": f"Bearer {token}"}

    r = httpx.post(f"{_API_BASE}/crm/contacts", json={
        "name": "File Test Contact",
        "type": "customer",
        "email": "filestest@example.com"
    }, headers=hdrs)
    assert r.status_code in (200, 201), f"contact: {r.status_code} {r.text[:200]}"
    data = r.json()
    raw_id = data.get("entity_id") or data.get("id")
    print(f"\n  Contact raw_id: {raw_id}")

    page.goto(f"{_UI_BASE}/contacts/{quote(str(raw_id), safe='')}", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    ss(page, "01_contact_detail")

    # Scroll to files section
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(400)
    ss(page, "02_contact_files_section")

    # Check search placeholder renders correctly (not raw key)
    search = page.locator("input[type=search]")
    if search.count() > 0:
        ph = search.first.get_attribute("placeholder")
        print(f"  Search placeholder: '{ph}'")
        assert ph != "label.search", f"Raw i18n key showing: '{ph}'"

    # Check table headers rendered
    headers = [th.inner_text().strip() for th in page.locator("table th").all()]
    print(f"  Table headers: {headers}")


def test_doc_files_accordion(page: Page, seeded_user: dict):
    token = seeded_user["access_token"]
    hdrs = {"Authorization": f"Bearer {token}"}

    # Get or create contact
    r = httpx.get(f"{_API_BASE}/crm/contacts", headers=hdrs, params={"limit": 1})
    contacts_data = r.json()
    if isinstance(contacts_data, list) and contacts_data:
        contact_id = contacts_data[0].get("entity_id") or contacts_data[0].get("id")
    elif isinstance(contacts_data, dict) and contacts_data.get("items"):
        contact_id = contacts_data["items"][0].get("entity_id") or contacts_data["items"][0].get("id")
    else:
        r2 = httpx.post(f"{_API_BASE}/crm/contacts", json={"name": "Doc Test", "type": "customer"}, headers=hdrs)
        contact_id = r2.json().get("entity_id") or r2.json().get("id")

    # Create draft invoice
    r = httpx.post(f"{_API_BASE}/docs", json={
        "doc_type": "invoice",
        "entity_id": contact_id,
        "issue_date": "2026-05-01",
        "due_date": "2026-05-31",
        "line_items": []
    }, headers=hdrs)
    assert r.status_code in (200, 201), f"doc: {r.status_code} {r.text[:200]}"
    doc_id = r.json().get("entity_id") or r.json().get("id")
    print(f"\n  Doc ID: {doc_id}")

    page.goto(f"{_UI_BASE}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    # Click the Files details/summary to expand
    files_summary = page.locator("details summary").filter(has_text="Files")
    print(f"  Files summary elements: {files_summary.count()}")
    if files_summary.count() > 0:
        files_summary.first.click()
        page.wait_for_timeout(600)

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(400)
    ss(page, "10_doc_files_expanded")

    # Check upload zone visible
    upload = page.locator("input[type=file]")
    print(f"  Upload inputs visible: {upload.count()}")

    # Check search placeholder
    search = page.locator("input[type=search]")
    if search.count() > 0:
        ph = search.first.get_attribute("placeholder")
        print(f"  Search placeholder: '{ph}'")
        assert ph != "label.search", f"Raw i18n key showing: '{ph}'"

    ss(page, "11_doc_files_full")
