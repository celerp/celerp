"""Real Files-card matrix, with the owning page URL recorded for every upload."""
import pytest
from playwright.sync_api import expect
from upload_matrix_helpers import FILES, OWNERS

pytestmark=pytest.mark.browser

@pytest.mark.parametrize('resource,kind,posted', OWNERS)
def test_attachment_files_card_matrix(page, ui_server, api, resource, kind, posted):
    payload={('doc_type' if resource=='docs' else 'list_type'):kind,
             'line_items':[{'name':'Service','quantity':1,'unit_price':10}]}
    r=api.post('/'+resource,json=payload)
    assert r.status_code==200,r.text
    eid=r.json()['id']
    if posted:
        r=api.post(f'/{resource}/{eid}/finalize')
        assert r.status_code==200,r.text
    url=f'{ui_server}/{resource}/{eid}'
    page.goto(url,wait_until='domcontentloaded')
    section=page.locator('.doc-internal').filter(has=page.locator('[id^="files-section-"]'))
    section.locator('summary').click()
    for name,mime,body in FILES:
        try:
            section.locator('[id^="file-drop-input-"]').set_input_files({'name':name,'mimeType':mime,'buffer':body})
            expect(section.locator('a.file-link',has_text=name)).to_be_visible(timeout=15000)
            expect(section.locator('.files-notice')).to_be_empty()
            assert "Celerp couldn't find this document" not in page.locator('main').inner_text()
            stored=api.get(f'/{resource}/{eid}').json()['files']
            entry=next(f for f in stored if f['filename']==name)
            response=api.get(f'/{resource}/{eid}/files/{entry["id"]}')
            assert response.content==body
        except Exception as exc:
            raise AssertionError(f'{name} ({len(body)} bytes) on {url}: {exc}') from exc
