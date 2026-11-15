"""Uploads through the shared document attachment controls."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser
PDF = b'%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n'

@pytest.mark.parametrize('resource,kind', [('docs', 'invoice'), ('lists', 'quotation')])
@pytest.mark.parametrize('posted', [False, True])
@pytest.mark.parametrize('filename', ['invoice.pdf', 'invoice.PDF', 'invoice copy.pdf'])
def test_pdf_upload_reaches_document_attachment_list(page, ui_server, api, posted, filename, resource, kind):
    response = api.post('/'+resource, json={('doc_type' if resource == 'docs' else 'list_type'): kind, 'line_items': [{'name': 'Service', 'quantity': 1, 'unit_price': 10}]})
    assert response.status_code == 200, response.text
    doc_id = response.json()['id']
    if posted:
        response = api.post(f'/{resource}/{doc_id}/finalize')
        assert response.status_code == 200, response.text
    page.goto(f'{ui_server}/{resource}/{doc_id}')
    section = page.locator('.doc-internal').filter(has=page.locator('[id^="files-section-"]'))
    section.locator('summary').click()
    page.locator('[id^="file-drop-input-"]').set_input_files({'name': filename, 'mimeType': 'application/pdf', 'buffer': PDF})
    expect(page.locator('a.file-link', has_text=filename)).to_be_visible()
    expect(page.locator('.files-notice')).to_be_empty()
    files = api.get(f'/{resource}/{doc_id}').json()['files']
    assert files[-1]['filename'] == filename
    assert api.get(f'/{resource}/{doc_id}/files/{files[-1]["id"]}').content == PDF


@pytest.mark.parametrize("camera", [False, True])
def test_file_picker_and_camera_input_accept_jpeg(page, ui_server, api, camera):
    import io
    from PIL import Image
    image=io.BytesIO(); Image.new("RGB", (16,16)).save(image, format="JPEG")
    eid=api.post('/docs',json={'doc_type':'invoice'}).json()['id']
    page.goto(f'{ui_server}/docs/{eid}')
    section=page.locator('.doc-internal').filter(has=page.locator('[id^="files-section-"]'))
    section.locator('summary').click()
    expect(section.locator('[id^="file-drop-input-"]')).to_have_count(1)
    camera_input=section.locator('[id^="file-camera-input-"]')
    expect(camera_input).to_have_attribute('capture','environment')
    expect(camera_input).to_have_attribute('accept','image/*')
    trigger=section.locator('.file-camera-btn' if camera else '.file-drop-text')
    with page.expect_file_chooser() as chooser:
        trigger.click()
    chooser.value.set_files({'name':'camera.jpg','mimeType':'image/jpeg','buffer':image.getvalue()})
    expect(section.locator('a.file-link',has_text='camera.jpg')).to_be_visible()
    assert api.get(f'/docs/{eid}').json()['files'][0]['mime']=='image/jpeg'
