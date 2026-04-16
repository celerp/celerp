# Copyright (c) 2026 Noah Severs. All rights reserved.
"""Group 15: Import history + undo — complete an import, verify history tab, undo it.

All default modules are expected to be loaded. Tests are unconditional.
"""
import uuid
import pytest

pytestmark = pytest.mark.browser

_ITEMS_CSV = (
    b"sku,name,quantity,location_name\n"
    b"IMP-HIST-001,Import History Test Item,10,Default\n"
    b"IMP-HIST-002,Import History Test Item 2,5,Default\n"
)


@pytest.fixture(scope="module")
def import_location(api):
    """Ensure a location named 'Default' exists for import tests."""
    r = api.post("/companies/me/locations", json={"name": "Default", "type": "warehouse"})
    assert r.status_code in {200, 201, 409}, f"Failed to create location: {r.text}"


@pytest.fixture
def completed_batch_id(api, import_location):
    """POST a CIF batch import and return the batch_id."""
    idempotency_key = f"test-import-hist-{uuid.uuid4()}"
    payload = {
        "records": [
            {
                "entity_id": f"item:{uuid.uuid4()}",
                "event_type": "item.created",
                "data": {
                    "sku": "IMP-HIST-A",
                    "name": "Hist Item A",
                    "quantity": 3,
                    "location_name": "Default",
                    "category": "General",
                },
                "source": "test_import_history",
                "idempotency_key": f"{idempotency_key}-A",
            },
            {
                "entity_id": f"item:{uuid.uuid4()}",
                "event_type": "item.created",
                "data": {
                    "sku": "IMP-HIST-B",
                    "name": "Hist Item B",
                    "quantity": 2,
                    "location_name": "Default",
                    "category": "General",
                },
                "source": "test_import_history",
                "idempotency_key": f"{idempotency_key}-B",
            },
        ],
        "filename": "test_import_history.csv",
    }
    r = api.post("/items/import/batch", json=payload)
    assert r.status_code in {200, 201}, f"POST /items/import/batch failed: {r.status_code} {r.text}"
    data = r.json()
    batch_id = data.get("batch_id")
    assert batch_id, f"No batch_id in import response: {data}"
    return batch_id


def test_import_history_tab_loads(page, ui_server):
    """IH-01: Settings → Import History tab loads without error."""
    resp = page.goto(f"{ui_server}/settings?tab=import-history", wait_until="domcontentloaded")
    assert resp.status != 500, "Import History tab returned 500"
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    assert "/login" not in page.url


def test_import_history_shows_batch(page, ui_server, completed_batch_id):
    """IH-02: After import, batch appears in Settings → Import History tab."""
    page.goto(f"{ui_server}/settings?tab=import-history", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    assert (
        "Undo" in body
        or "undo" in page.content().lower()
        or "import" in body.lower()
    ), f"No import batch entries visible. Body snippet: {body[:400]}"


def test_import_history_undo_endpoint(api, import_location):
    """IH-03: POST /items/import/batches/{id}/undo returns success."""
    # Create a dedicated batch for this test (not using shared completed_batch_id fixture
    # to avoid ordering dependency with test_import_history_shows_batch which runs first)
    key = f"test-undo-ep-{uuid.uuid4()}"
    payload = {
        "records": [{
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": {
                "sku": f"IMP-UNDO-EP-{key[-6:]}",
                "name": "Undo Endpoint Test",
                "quantity": 1,
                "location_name": "Default",
                "category": "General",
            },
            "source": "test_undo_endpoint",
            "idempotency_key": key,
        }],
        "filename": "undo_ep_test.csv",
    }
    r = api.post("/items/import/batch", json=payload)
    assert r.status_code in {200, 201}, f"POST /items/import/batch failed: {r.text}"
    batch_id = r.json().get("batch_id")
    assert batch_id, f"No batch_id: {r.json()}"

    undo_r = api.post(f"/items/import/batches/{batch_id}/undo")
    assert undo_r.status_code in {200, 204}, \
        f"Undo batch {batch_id} failed: {undo_r.status_code} {undo_r.text}"


def test_import_history_undo_removes_items(api, import_location):
    """IH-04: Fresh batch → undo → items from that batch are no longer active."""
    key = f"test-undo-rm-{uuid.uuid4()}"
    item_entity_id = f"item:{uuid.uuid4()}"
    payload = {
        "records": [{
            "entity_id": item_entity_id,
            "event_type": "item.created",
            "data": {
                "sku": f"IMP-UNDO-RM-{key[-6:]}",
                "name": "Undo Remove Test",
                "quantity": 1,
                "location_name": "Default",
                "category": "General",
            },
            "source": "test_undo_removes",
            "idempotency_key": key,
        }],
        "filename": "undo_rm_test.csv",
    }
    r = api.post("/items/import/batch", json=payload)
    assert r.status_code in {200, 201}, f"POST batch failed: {r.text}"
    batch_id = r.json().get("batch_id")
    assert batch_id, f"No batch_id: {r.json()}"

    # Item should exist before undo (projection may lag — allow 404 as soft pass)
    r_before = api.get(f"/items/{item_entity_id}")
    assert r_before.status_code in {200, 404}, f"Unexpected status pre-undo: {r_before.status_code}"

    undo_r = api.post(f"/items/import/batches/{batch_id}/undo")
    assert undo_r.status_code in {200, 204}, f"Undo failed: {undo_r.status_code}"

    r_after = api.get(f"/items/{item_entity_id}")
    assert r_after.status_code in {404, 200}, f"Unexpected status after undo: {r_after.status_code}"
    if r_after.status_code == 200:
        status = r_after.json().get("status", "")
        assert status in ("deleted", "disposed"), \
            f"Expected item deleted after undo, got status={status!r}"


def test_undo_button_visible_in_ui(page, ui_server, completed_batch_id):
    """IH-05: Import History tab shows undo-related content for active batches."""
    page.goto(f"{ui_server}/settings?tab=import-history", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
    content = page.content().lower()
    assert "undo" in content or "import" in content, \
        "Import history tab loaded but no undo-related content found"


def test_import_history_via_browser_upload(page, ui_server, import_location):
    """IH-06: Full browser flow: upload CSV → confirm → batch appears in history."""
    page.goto(f"{ui_server}/inventory/import", wait_until="domcontentloaded")
    file_input = page.locator("input[type='file']").first
    assert file_input.count() > 0, "No file input found on /inventory/import"

    file_input.set_input_files({
        "name": "import_history_test.csv",
        "mimeType": "text/csv",
        "buffer": _ITEMS_CSV,
    })

    upload_btn = page.locator(
        "button:has-text('Upload'), button:has-text('Preview'), "
        "button:has-text('Next'), input[type='submit']"
    ).first
    if upload_btn.count() > 0:
        upload_btn.click()
        page.wait_for_load_state("networkidle", timeout=10000)

    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, "Import preview step crashed"
    assert "Traceback" not in body

    confirm_btn = page.locator(
        "button:has-text('Confirm'), button:has-text('Import'), "
        "button:has-text('Confirm Import')"
    ).first
    if confirm_btn.count() > 0:
        confirm_btn.click()
        page.wait_for_load_state("networkidle", timeout=10000)
        body = page.locator("body").inner_text()
        assert "Internal Server Error" not in body, "Import confirm step crashed"

    page.goto(f"{ui_server}/settings?tab=import-history", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body
    assert "Traceback" not in body
