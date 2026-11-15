"""The attachment file picker, its own camera input, and interoperable camera MIME types."""
import io
import pytest
from PIL import Image
from starlette.datastructures import Headers, UploadFile
from fasthtml.common import to_xml
from ui.components.files import files_section
from celerp.services.attachments import upload_mime

@pytest.mark.parametrize('owner', ['doc', 'list', 'contact', 'item'])
def test_shared_files_section_renders_file_picker_and_camera_input(owner):
    from bs4 import BeautifulSoup
    section=BeautifulSoup(to_xml(files_section(owner, 'test-1', [])), 'html.parser')
    picker=section.select_one('input[id^=file-drop-input-]')
    camera=section.select_one('input[id^=file-camera-input-]')
    assert len(section.select('input[type=file]'))==2
    assert 'image/*' in picker['accept'] and '.pdf' in picker['accept']
    assert not picker.has_attr('capture')
    assert camera['accept']=='image/*' and camera['capture']=='environment'
    assert section.select_one('.file-camera-btn')

@pytest.mark.parametrize('sent,expected', [('image/x-png','image/png'),('IMAGE/PNG; charset=binary','image/png'),('image/jpg','image/jpeg'),('image/pjpeg','image/jpeg'),('application/octet-stream','image/png')])
def test_camera_mime_aliases_are_normalized(sent, expected):
    assert upload_mime(UploadFile(io.BytesIO(), filename='photo.PNG', headers=Headers({'content-type':sent})))==expected

@pytest.mark.asyncio
@pytest.mark.parametrize('name,mime,mode', [('alpha.png','image/x-png','RGBA'), ('camera.jpg','image/jpeg','RGB'), ('photo.png','image/png','RGBA')])
async def test_image_with_camera_mime_is_stored_and_listed(client, name, mime, mode):
    from test_entity_files import _headers
    h=await _headers(client)
    doc=(await client.post('/lists',headers=h,json={'list_type':'quotation'})).json()['id']
    image=io.BytesIO();Image.new(mode,(128,128)).save(image,format='PNG' if name.endswith('.png') else 'JPEG')
    r=await client.post(f'/lists/{doc}/files',headers=h,files={'file':(name,image.getvalue(),mime)})
    assert r.status_code==200,r.text
    assert r.json()['mime']==('image/png' if name.endswith('.png') else 'image/jpeg')
    files=(await client.get(f'/lists/{doc}',headers=h)).json()['files']
    assert files[0]['filename']==name

@pytest.mark.asyncio
async def test_unsupported_executable_has_a_clear_type_error(client):
    from test_entity_files import _headers
    h=await _headers(client)
    eid=(await client.post('/docs',headers=h,json={'doc_type':'invoice'})).json()['id']
    r=await client.post(f'/docs/{eid}/files',headers=h,files={'file':('bad.exe',b'MZ','application/x-msdownload')})
    assert r.status_code in (413,415),r.text
    from ui.i18n import t
    assert r.json()['detail'] == t('error.file_type_unsupported', mime='application/x-msdownload')
    assert (await client.get(f'/docs/{eid}',headers=h)).json()['files']==[]
