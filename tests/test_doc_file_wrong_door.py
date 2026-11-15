"""A document's files are reached only through /docs/{id}/files and a list's only through
/lists/{id}/files: every file operation through the other route answers 404 and leaves the
record, its events and the stored bytes as they were."""
import pytest

from celerp.config import settings
from celerp.services import attachments
from test_entity_files import _SMALL_PDF, _headers, _ledger_by_type

_OPS = {
    "upload": ("post", "", {"files": {"file": ("scan.pdf", _SMALL_PDF, "application/pdf")}}),
    "download": ("get", "/{fid}", {}),
    "tag": ("patch", "/{fid}/tag", {"data": {"document_tag": "contract"}}),
    "description": ("patch", "/{fid}/description", {"data": {"description": "Signed"}}),
    "delete": ("delete", "/{fid}", {}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("own,other", [("docs", "lists"), ("lists", "docs")])
@pytest.mark.parametrize("op", sorted(_OPS))
async def test_every_file_operation_through_the_other_route_is_refused(client, monkeypatch, tmp_path, own, other, op):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())
    h = await _headers(client)
    body = {"doc_type": "invoice"} if own == "docs" else {"list_type": "quotation"}
    eid = (await client.post(f"/{own}", headers=h, json=body)).json()["id"]
    up = await client.post(f"/{own}/{eid}/files", headers=h,
                           files={"file": ("kept.pdf", _SMALL_PDF, "application/pdf")})
    assert up.status_code == 200, up.text
    fid = up.json()["id"]
    record = (await client.get(f"/{own}/{eid}", headers=h)).json()
    events = await _ledger_by_type(client, h, eid)
    stored = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    method, suffix, kw = _OPS[op]
    r = await client.request(method.upper(), f"/{other}/{eid}/files{suffix.format(fid=fid)}", headers=h, **kw)

    assert r.status_code == 404, r.text
    assert (await client.get(f"/{own}/{eid}", headers=h)).json().get("files") == record.get("files")
    assert await _ledger_by_type(client, h, eid) == events
    assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == stored
    # The record's own route still serves the same operation.
    if op != "upload":
        ok = await client.request(method.upper(), f"/{own}/{eid}/files{suffix.format(fid=fid)}", headers=h, **kw)
        assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_a_connected_company_file_through_lists_is_refused_before_it_is_read():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from celerp.services.peer_import import receive_file

    file = SimpleNamespace(read=None)  # never read: the refusal comes first
    with pytest.raises(HTTPException) as refused:
        await receive_file(SimpleNamespace(peer=object()), None, "doc:SRC-1", file, "list",
                           source_file_id="f1", sha256_hex="0" * 64, document_tag=None, description=None)
    assert refused.value.status_code == 404
