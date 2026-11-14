"""A file uploaded to a document or a list keeps the tag and description sent with it.

The upload takes two kinds of form fields: a connected company's file identity
(source_file_id, sha256_hex), sent only with that company's integration key, and the file's
ordinary details (document_tag, description), which anyone allowed to upload may send. The
ordinary details are stored on the file-attached event; the identity fields from a signed-in
user are refused before anything is stored."""
import asyncio

import pytest
from sqlalchemy import delete

from celerp.config import settings
from celerp.models.projections import Projection
from celerp.services import attachments
from test_entity_files import _SMALL_PDF, _headers, _ledger_by_type

_DOORS = [("docs", {"doc_type": "invoice"}), ("lists", {"list_type": "quotation"})]


@pytest.fixture
def stored_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())
    return lambda: sorted(p for p in tmp_path.rglob("*") if p.is_file())


async def _record(client, door, body):
    h = await _headers(client)
    eid = (await client.post(f"/{door}", headers=h, json=body)).json()["id"]
    return h, eid


def _upload(name="scan.pdf"):
    return {"file": (name, _SMALL_PDF, "application/pdf")}


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
async def test_upload_keeps_its_tag_and_description(client, stored_files, door, body):
    h, eid = await _record(client, door, body)
    r = await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(),
                          data={"document_tag": "contracts", "description": "Signed original"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    files = (await client.get(f"/{door}/{eid}", headers=h)).json()["files"]
    assert [(f["id"], f["document_tag"], f["description"]) for f in files] == [
        (fid, "contracts", "Signed original")]
    (attached,) = (await _ledger_by_type(client, h, eid))[f"{door[:-1]}.file_attached"]
    assert (attached["data"]["document_tag"], attached["data"]["description"]) == (
        "contracts", "Signed original")


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
async def test_upload_without_details_stores_empty_details(client, stored_files, door, body):
    h, eid = await _record(client, door, body)
    r = await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(),
                          data={"document_tag": "", "description": ""})
    assert r.status_code == 200, r.text
    (f,) = (await client.get(f"/{door}/{eid}", headers=h)).json()["files"]
    assert (f["document_tag"], f["description"]) == ("", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
@pytest.mark.parametrize("field,value", [("source_file_id", "file-1"), ("sha256_hex", "a" * 64)])
async def test_connected_company_fields_from_a_signed_in_user_are_refused(
        client, stored_files, door, body, field, value):
    h, eid = await _record(client, door, body)
    events = await _ledger_by_type(client, h, eid)
    r = await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(),
                          data={field: value, "document_tag": "contracts"})
    assert r.status_code == 422, r.text
    assert (await client.get(f"/{door}/{eid}", headers=h)).json().get("files", []) == []
    assert await _ledger_by_type(client, h, eid) == events
    assert stored_files() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
@pytest.mark.parametrize("field,size", [("document_tag", 129), ("description", 4097)])
async def test_overlong_details_are_refused_before_storing(client, stored_files, door, body, field, size):
    h, eid = await _record(client, door, body)
    r = await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(), data={field: "x" * size})
    assert r.status_code == 422, r.text
    assert stored_files() == []
    assert (await client.get(f"/{door}/{eid}", headers=h)).json().get("files", []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
async def test_a_failed_upload_leaves_no_file_and_no_event(client, stored_files, monkeypatch, door, body):
    h, eid = await _record(client, door, body)
    events = await _ledger_by_type(client, h, eid)

    async def too_large(*_a, **_k):
        raise ValueError("File exceeds the size limit")
    monkeypatch.setattr(attachments, "store_upload", too_large)
    r = await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(),
                          data={"document_tag": "contracts", "description": "Signed original"})
    assert r.status_code == 413, r.text
    assert (await client.get(f"/{door}/{eid}", headers=h)).json().get("files", []) == []
    assert await _ledger_by_type(client, h, eid) == events
    assert stored_files() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
async def test_sending_the_same_upload_again_adds_a_second_file(client, stored_files, door, body):
    """A signed-in user's upload carries no identity of its own, so sending it again is a
    second file: both are kept, each with its own id and the details it was sent with."""
    h, eid = await _record(client, door, body)
    sent = [await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(),
                              data={"document_tag": "contracts", "description": "Signed original"})
            for _ in range(2)]
    assert [r.status_code for r in sent] == [200, 200]
    files = (await client.get(f"/{door}/{eid}", headers=h)).json()["files"]
    assert sorted(f["id"] for f in files) == sorted(r.json()["id"] for r in sent)
    assert len({f["id"] for f in files}) == 2
    assert {(f["document_tag"], f["description"]) for f in files} == {("contracts", "Signed original")}


@pytest.mark.asyncio
@pytest.mark.parametrize("door,body", _DOORS)
async def test_a_record_deleted_during_the_upload_keeps_no_file(
        client, session, stored_files, monkeypatch, door, body):
    h, eid = await _record(client, door, body)
    store = attachments.store_upload

    async def store_then_delete_record(company_id, file, attachment_type=None):
        meta = await store(company_id, file, attachment_type)
        # Another request deletes the record meanwhile (its own task, as in the app).
        await asyncio.create_task(session.execute(delete(Projection).where(Projection.entity_id == eid)))
        return meta
    monkeypatch.setattr(attachments, "store_upload", store_then_delete_record)
    r = await client.post(f"/{door}/{eid}/files", headers=h, files=_upload(),
                          data={"document_tag": "contracts", "description": "Signed original"})
    assert r.status_code == 404, r.text
    assert stored_files() == []
