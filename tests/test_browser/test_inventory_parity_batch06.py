"""Inventory uses the shared toolbar, record cards, file picker and print controls."""
import pytest
from playwright.sync_api import expect
pytestmark = pytest.mark.browser


@pytest.mark.parametrize('status', ['sold', 'archived', 'disposed'])
def test_direct_and_sidebar_status_navigation_update_toolbar(page, ui_server, status):
    page.goto(f'{ui_server}/inventory?status={status}', wait_until='domcontentloaded')
    toolbar = page.locator('#inventory-toolbar-actions')
    for selector in ('[hx-post="/inventory/create-blank"]', '[href="/inventory/import"]', '[href*="category-library"]'):
        expect(toolbar.locator(selector)).to_have_count(0)
    expect(toolbar.locator('a[href^="/inventory/export/csv"]')).to_have_count(1)
    page.goto(f'{ui_server}/inventory', wait_until='domcontentloaded')
    expect(page.locator('#inventory-toolbar-actions [hx-post="/inventory/create-blank"]')).to_have_count(1)
    page.locator(f'a[href="/inventory?status={status}"]').first.click()
    expect(page.locator('#inventory-toolbar-actions [hx-post="/inventory/create-blank"]')).to_have_count(0)
    expect(page.locator('#inventory-toolbar-actions a[href="/inventory/import"]')).to_have_count(0)
    expect(page.locator('#inventory-toolbar-actions a[href*="category-library"]')).to_have_count(0)
    expect(page.locator('#inventory-toolbar-actions a[href^="/inventory/export/csv"]')).to_have_count(1)


@pytest.mark.parametrize('tab', ['details', 'manufacturing'])
def test_item_tabs_share_one_working_file_picker_and_distinct_print_controls(page, ui_server, api, tab):
    response = api.post('/items', json={'sku': 'FILE-CARDS', 'name': 'Shared cards', 'sell_by': 'piece', 'quantity': 2})
    assert response.status_code == 200, response.text
    iid = response.json()['id']
    page.goto(f'{ui_server}/inventory/{iid}?tab={tab}', wait_until='domcontentloaded')
    card = page.locator('[id^="files-section-"]')
    expect(card).to_have_class('doc-section')
    expect(card.locator('.section-header .section-title')).to_be_visible()
    expect(card.locator('input[id^="file-drop-input-"]')).to_have_count(1)
    card.locator('input[id^="file-drop-input-"]').set_input_files({'name': 'inventory.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-1.4\n%%EOF'})
    expect(card.locator('a.file-link', has_text='inventory.pdf')).to_be_visible()
    assert len(api.get(f'/items/{iid}').json().get('files') or []) == 1
    worksheet = page.locator('a[href$="/worksheet/print"]')
    labels = page.locator('.print-label-wrapper > button')
    assert worksheet.get_attribute('title') != labels.get_attribute('title')
    assert worksheet.locator('svg').inner_html() != labels.locator('svg').inner_html()
    page.emulate_media(media='print')
    expect(card.locator('a.file-link', has_text='inventory.pdf')).to_be_visible()
