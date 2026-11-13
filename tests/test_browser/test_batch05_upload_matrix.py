"""Real Files-card matrix, with the owning page URL recorded for every upload."""
import json
from pathlib import Path
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
    results=[]
    report=Path('/private/tmp/batch05-upload-matrix');report.mkdir(exist_ok=True)
    try:
        for name,mime,body in FILES:
            cell={'resource':resource,'type':kind,'state':'issued' if posted else 'draft','file':name,'bytes':len(body),'url':url,'result':'FAIL','error':''}
            results.append(cell)
            try:
                section.locator('input[type=file]').set_input_files({'name':name,'mimeType':mime,'buffer':body})
                expect(section.locator('a.file-link',has_text=name)).to_be_visible(timeout=15000)
                expect(section.locator('.files-notice')).to_be_empty()
                assert "Celerp couldn't find this document" not in page.locator('main').inner_text()
                stored=api.get(f'/{resource}/{eid}').json()['files']
                entry=next(f for f in stored if f['filename']==name)
                response=api.get(f'/{resource}/{eid}/files/{entry["id"]}')
                assert response.content==body
                cell['result']='PASS'
            except Exception as exc:
                cell['error']=str(exc);raise
    finally:
        (report/f'{resource}-{kind}-{posted}.json').write_text(json.dumps(results,indent=2))
