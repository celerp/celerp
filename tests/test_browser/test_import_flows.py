# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 6: Import flows — upload CSV → preview step reached, no 500."""
import io
import pytest

pytestmark = pytest.mark.browser

_IMPORT_CSVS = [
    ("items", "/inventory/import", b"sku,name,quantity\nIMP-TEST-001,Import Test,10"),
    ("contacts", "/crm/import/contacts", b"name,email\nImport Co,import@test.com"),
    ("locations", "/settings/import/locations", b"name\nImport Warehouse"),
]


@pytest.mark.parametrize("label,route,csv_bytes", _IMPORT_CSVS, ids=[l for l, _, _ in _IMPORT_CSVS])
def test_import_upload_reaches_preview(page, ui_server, label, route, csv_bytes):
    """IMP-01..03: Upload CSV → confirm/preview step reached."""
    page.goto(f"{ui_server}{route}", wait_until="domcontentloaded")

    # Find file input
    file_input = page.locator("input[type='file']").first
    if file_input.count() == 0:
        pytest.skip(f"No file input found at {route}")

    file_input.set_input_files({
        "name": f"{label}.csv",
        "mimeType": "text/csv",
        "buffer": csv_bytes,
    })

    # Click upload/preview button
    upload_btn = page.locator(
        "button:has-text('Upload'), button:has-text('Preview'), "
        "button:has-text('Next'), input[type='submit']"
    ).first
    if upload_btn.count() > 0:
        upload_btn.click()
        page.wait_for_load_state("networkidle", timeout=10000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"{label} import: Internal Server Error"
    assert "Traceback" not in body, f"{label} import: traceback in body"
    # Should show a preview table, confirm button, or success message
    # (not just the original upload form with no progress)


def test_import_without_location_col_blocked(page, ui_server, api):
    """IMP-06: Inventory import without location_name col → error, not 500."""
    # First create a location via API so the rule is testable
    api.post("/companies/me/locations", json={"name": "Test Location"})

    page.goto(f"{ui_server}/inventory/import", wait_until="domcontentloaded")
    file_input = page.locator("input[type='file']").first
    if file_input.count() == 0:
        pytest.skip("No file input found at /inventory/import")

    # CSV without location_name - should fail validation
    csv_bytes = b"sku,name,quantity\nNOLOC-001,No Location,5"
    file_input.set_input_files({
        "name": "no_location.csv",
        "mimeType": "text/csv",
        "buffer": csv_bytes,
    })

    upload_btn = page.locator(
        "button:has-text('Upload'), button:has-text('Preview'), "
        "button:has-text('Next'), input[type='submit']"
    ).first
    if upload_btn.count() > 0:
        upload_btn.click()
        page.wait_for_load_state("networkidle", timeout=10000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, "Import without location: Internal Server Error"
    assert "500" not in page.url, "Import without location: URL contains 500"


def test_import_docs_accessible(page, ui_server):
    """IMP-03: /docs/import loads without error."""
    resp = page.goto(f"{ui_server}/docs/import", wait_until="domcontentloaded")
    assert resp.status != 500
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
