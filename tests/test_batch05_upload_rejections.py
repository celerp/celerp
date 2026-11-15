"""Invalid uploads leave neither stored bytes nor attachment events behind."""
import pytest
from test_entity_files import _headers, _SMALL_PDF, _ledger_by_type
from celerp.services import attachments
from celerp.config import settings
from ui.i18n import t

@pytest.mark.asyncio
@pytest.mark.parametrize('resource',['docs','lists'])
@pytest.mark.parametrize('reason',['empty','type','size','missing'])
async def test_rejected_upload_has_a_specific_error_and_leaves_no_partial_record(client, monkeypatch, tmp_path, resource, reason):
    monkeypatch.setattr(settings,'data_dir',tmp_path)
    monkeypatch.setattr(attachments,'_backend',attachments.LocalBackend())
    h=await _headers(client)
    eid=(await client.post('/'+resource,headers=h,json={('doc_type' if resource=='docs' else 'list_type'):'invoice' if resource=='docs' else 'quotation'})).json()['id']
    before=await _ledger_by_type(client,h,eid)
    body=b'' if reason=='empty' else b'x'*(attachments.MAX_FILE_BYTES+1) if reason=='size' else _SMALL_PDF
    mime='application/x-msdownload' if reason=='type' else 'application/pdf'
    path=f'/{resource}/{"missing-record" if reason=="missing" else eid}/files'
    r=await client.post(path,headers=h,files={'file':('bad.exe' if reason=='type' else 'scan.pdf',body,mime)})
    assert r.status_code==(404 if reason=='missing' else 413),r.text
    expected={'empty':t('auth.file_empty'),'type':t('error.file_type_unsupported',mime=mime),'size':t('error.file_too_large',mb=50)}
    if reason=='missing':assert "couldn't find" in r.text
    elif reason=='size':
        from ui.i18n import refusal_text
        assert refusal_text(r.json()['detail']) in {expected[reason], t('error.upload_too_large')}
    else:assert r.json()['detail']==expected[reason]
    assert (await client.get(f'/{resource}/{eid}',headers=h)).json().get('files',[])==[]
    assert (await _ledger_by_type(client,h,eid)).keys()==before.keys()
    assert not [p for p in tmp_path.rglob('*') if p.is_file()]
