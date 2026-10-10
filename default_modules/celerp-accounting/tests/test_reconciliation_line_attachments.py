# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A statement line's attachments live in the company's attachment store.

They are stored, served and removed through celerp/services/attachments.py, exactly as
contact, document and item files are: under the company's own folder, readable only by
that company, and deleted when the line lets go of them. Nothing is written relative to
the process directory.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.services.attachments import company_attachment_dir

_PDF = b"%PDF-1.4 receipt"


@pytest.fixture(autouse=True)
def _process_dir(tmp_path, monkeypatch):
    """Run from an empty directory and prove every test leaves it empty: no attachment is
    ever written relative to the process directory."""
    monkeypatch.chdir(tmp_path)
    yield
    assert list(tmp_path.iterdir()) == [], "a file was written relative to the process directory"


async def _company(client) -> tuple[dict, str]:
    r = await client.post("/auth/register", json={
        "email": f"rla_{uuid.uuid4().hex[:8]}@example.com", "password": "pass1234",
        "name": "Recon", "company_name": f"Recon {uuid.uuid4().hex[:6]}",
    })
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return h, (await client.get("/companies/me", headers=h)).json()["id"]


async def _other_company(client, h) -> dict:
    """A second company, created from the first user's session."""
    r = await client.post("/companies", json={"name": f"Other {uuid.uuid4().hex[:6]}"}, headers=h)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _line(client, h) -> tuple[str, list[str]]:
    bank = (await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Main", "account_number": "1", "bank_type": "checking", "currency": "THB",
    })).json()
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 500.0,
    })
    sid = r.json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/import-csv", headers=h,
                          files={"file": ("s.csv", b"Date,Description,Amount\n2026-03-05,Deposit,500\n"
                                                   b"2026-03-06,Fee,-5\n", "text/csv")})
    assert r.status_code == 200, r.text
    items = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()["items"]
    return sid, [l["id"] for l in items]


async def _attach(client, h, sid, line_id, content=_PDF, name="receipt.pdf", mime="application/pdf"):
    return await client.post(f"/accounting/reconciliation/{sid}/lines/{line_id}/attach", headers=h,
                             files={"file": (name, content, mime)})


async def _attachments(client, h, sid, line_id) -> list[dict]:
    items = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()["items"]
    return next(l for l in items if l["id"] == line_id)["attachments"]


@pytest.mark.asyncio
async def test_an_attachment_is_stored_for_the_company_and_served_to_it(client):
    h, cid = await _company(client)
    sid, (line, _) = await _line(client, h)

    r = await _attach(client, h, sid, line)
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["url"] == f"/static/attachments/{cid}/{att['attachment_id']}"
    assert att["filename"] == "receipt.pdf"
    assert (company_attachment_dir(cid) / att["attachment_id"]).read_bytes() == _PDF
    assert await _attachments(client, h, sid, line) == [{"id": att["attachment_id"], "url": att["url"]}]
    served = await client.get(att["url"], headers=h)
    assert (served.status_code, served.content) == (200, _PDF)


@pytest.mark.asyncio
async def test_another_company_cannot_read_the_attachment(client):
    h, _cid = await _company(client)
    sid, (line, _) = await _line(client, h)
    url = (await _attach(client, h, sid, line)).json()["url"]

    other = await _other_company(client, h)
    assert (await client.get(url, headers=other)).status_code == 404


@pytest.mark.asyncio
async def test_removing_an_attachment_deletes_its_file(client):
    h, cid = await _company(client)
    sid, (line, _) = await _line(client, h)
    att = (await _attach(client, h, sid, line)).json()
    stored = company_attachment_dir(cid) / att["attachment_id"]
    assert stored.exists()

    r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line}/attach/{att['attachment_id']}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"removed": att["attachment_id"]}
    assert not stored.exists()
    assert await _attachments(client, h, sid, line) == []
    assert (await client.get(att["url"], headers=h)).status_code == 404


# Neighbour guards.

@pytest.mark.asyncio
async def test_attaching_the_same_file_twice_keeps_one_copy(client):
    h, cid = await _company(client)
    sid, (line, _) = await _line(client, h)
    first = (await _attach(client, h, sid, line)).json()
    second = (await _attach(client, h, sid, line)).json()
    assert first == second
    assert len(await _attachments(client, h, sid, line)) == 1
    assert sorted(p.name for p in company_attachment_dir(cid).iterdir() if "_thumb" not in p.name) \
        == [first["attachment_id"]]


@pytest.mark.asyncio
async def test_the_same_file_on_two_lines_and_two_companies_is_kept_apart(client):
    h, _cid = await _company(client)
    sid, (line_a, line_b) = await _line(client, h)
    a = (await _attach(client, h, sid, line_a)).json()
    b = (await _attach(client, h, sid, line_b)).json()
    other = await _other_company(client, h)
    osid, (oline, _) = await _line(client, other)
    o = (await _attach(client, other, osid, oline)).json()
    assert len({a["url"], b["url"], o["url"]}) == 3

    r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line_a}/attach/{a['attachment_id']}", headers=h)
    assert r.status_code == 200, r.text
    assert (await client.get(b["url"], headers=h)).status_code == 200
    assert (await client.get(o["url"], headers=other)).status_code == 200


@pytest.mark.asyncio
async def test_removing_an_attachment_the_line_does_not_hold_is_refused(client):
    h, cid = await _company(client)
    sid, (line_a, line_b) = await _line(client, h)
    b = (await _attach(client, h, sid, line_b)).json()

    for att_id in (b["attachment_id"], "missing.pdf"):
        r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line_a}/attach/{att_id}", headers=h)
        assert r.status_code == 404, (att_id, r.text)
        assert r.json()["detail"]["message_key"] == "accounting.statement_attachment_not_found"
    # A path in place of an id never reaches a file.
    for att_id in ("..%2Fsecret.pdf", f"..%2F{b['attachment_id']}", "x%2F..%2Fy.pdf"):
        r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line_b}/attach/{att_id}", headers=h)
        assert r.status_code == 404, (att_id, r.text)
    assert (company_attachment_dir(cid) / b["attachment_id"]).exists()
    assert len(await _attachments(client, h, sid, line_b)) == 1

    r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line_b}/attach/{b['attachment_id']}", headers=h)
    assert r.status_code == 200, r.text
    r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line_b}/attach/{b['attachment_id']}", headers=h)
    assert r.status_code == 404, "a repeated remove is refused"


@pytest.mark.asyncio
async def test_a_file_type_the_store_does_not_take_is_refused_and_nothing_is_kept(client):
    h, cid = await _company(client)
    sid, (line, _) = await _line(client, h)

    r = await _attach(client, h, sid, line, content=b"MZ\x90", name="tool.exe", mime="application/x-msdownload")
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["message_key"] == "accounting.statement_attachment_refused"
    assert await _attachments(client, h, sid, line) == []
    d = company_attachment_dir(cid)
    assert not d.exists() or list(d.iterdir()) == []


@pytest.mark.asyncio
async def test_a_file_sent_as_generic_binary_is_stored_by_the_type_its_name_gives(client):
    """The app's own client sends every file as generic binary; a PDF named so is a PDF."""
    h, _ = await _company(client)
    sid, (line, _) = await _line(client, h)

    r = await _attach(client, h, sid, line, mime="application/octet-stream")
    assert r.status_code == 200, r.text
    assert len(await _attachments(client, h, sid, line)) == 1


@pytest.mark.asyncio
async def test_generic_binary_with_no_known_type_is_still_refused(client):
    h, _ = await _company(client)
    sid, (line, _) = await _line(client, h)

    r = await _attach(client, h, sid, line, content=b"\x00\x01", name="blob.bin",
                      mime="application/octet-stream")
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["message_key"] == "accounting.statement_attachment_refused"
    assert await _attachments(client, h, sid, line) == []
