# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Files for the bank page: a statement file and a client's receipt are kept through the
accounting file route, with no AI access and no assistant permission, so a CSV, Excel or
OFX statement imports on an install that is not connected."""

from __future__ import annotations

import pytest

import bank_statement_helpers as B

pytestmark = pytest.mark.asyncio

FILES = f"{B.API}/files"


@pytest.fixture(autouse=True)
def _no_ai_access(monkeypatch):
    async def _never() -> bool:
        return False
    monkeypatch.setattr("celerp.session_gate.ai_access_available", _never)


async def _send(client, h, *files):
    return await client.post(FILES, files=[("files", f) for f in files], headers=h)


async def test_statement_file_imports_without_ai_access(client):
    from celerp.ai import jobs
    from test_helpers import until
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    csv = B.csv_bytes([("2026-01-05", "NETFLIX.COM", "-15.99"), ("2026-01-09", "WITHDRAWAL", "-200.00")])
    r = await _send(client, co["h"], ("jan.csv", csv, "text/csv"))
    assert r.status_code == 201, r.text
    r = await client.post(f"{B.API}/read-statement", json={"file_ids": r.json()["file_ids"], "bank_id": bk["id"],
                                                           "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    await until(lambda: not jobs.running_tasks())
    p = (await client.get(f"{B.API}/statement-preview/{r.json()['job_id']}", headers=co["h"])).json()
    assert p["failed_steps"] == []
    assert [l["description"] for l in p["statements"][0]["lines"]] == ["NETFLIX.COM", "WITHDRAWAL"]


async def test_client_keeps_receipt_with_answer_permission_only(client, session):
    from test_helpers import grant_permission
    co = await B.company(client)
    await grant_permission(client, co["h"], "answer_bank_questions", "viewer")
    client_h = await B.member(client, session, co, "viewer")
    r = await _send(client, client_h, ("slip.png", b"\x89PNG\r\n\x1a\n", "image/png"))
    assert r.status_code == 201, r.text
    assert len(r.json()["file_ids"]) == 1


async def test_files_refused_without_bank_permission(client, session):
    co = await B.company(client)
    viewer_h = await B.member(client, session, co, "viewer")
    r = await _send(client, viewer_h, ("jan.csv", b"Date,Description,Amount\n", "text/csv"))
    assert r.status_code == 403, r.text


async def test_a_0_byte_statement_file_is_refused(client):
    co = await B.company(client)
    r = await _send(client, co["h"], ("jan.csv", b"", "text/csv"))
    assert r.status_code == 400 and "File is empty" in r.text


async def test_files_refused_by_type_size_and_count(client, monkeypatch):
    co = await B.company(client)
    r = await _send(client, co["h"], ("run.exe", b"MZ", "application/octet-stream"))
    assert r.status_code == 400 and "run.exe" in r.text
    monkeypatch.setattr("celerp.ai.files.MAX_UPLOAD_BYTES", 8)
    r = await _send(client, co["h"], ("big.csv", b"Date,Amount\n", "text/csv"))
    assert r.status_code == 400 and "big.csv" in r.text
    monkeypatch.undo()
    r = await _send(client, co["h"], *[(f"f{i}.csv", b"a\n", "text/csv") for i in range(21)])
    assert r.status_code == 400
