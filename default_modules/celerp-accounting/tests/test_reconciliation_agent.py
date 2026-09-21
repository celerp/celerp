# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Reconciliation routes the AI assistant drives: capability compilation, statement
file import from the upload store, the capped workbench, and the validation that
keeps a retried or mistaken proposal from landing twice or on a missing account."""

from __future__ import annotations

import io
import json
import uuid as _uuid

import pytest
from httpx import AsyncClient

from celerp.ai import tools as ai_tools
from celerp.ai.files import XLSX_CONTENT_TYPE, upload_dir
from celerp.models.projections import Projection

from test_helpers import real_agent_app
from test_reconciliation_v2 import _auth, _create_bank, _create_recon

_AGENT_RECON_ROUTES = {
    ("GET", "/accounting/bank-accounts"),
    ("POST", "/accounting/reconciliation/start"),
    ("GET", "/accounting/reconciliation/{session_id}/workbench"),
    ("POST", "/accounting/reconciliation/{session_id}/import-file"),
    ("POST", "/accounting/reconciliation/{session_id}/auto-match"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/match"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/unmatch"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/create"),
    ("PATCH", "/accounting/reconciliation/{session_id}/lines/{line_id}"),
    ("POST", "/accounting/reconciliation/{session_id}/bulk-confirm"),
    ("POST", "/accounting/reconciliation/{session_id}/complete"),
    ("POST", "/accounting/reconciliation/{session_id}/write-off"),
}

_CSV = b"Date,Description,Amount\n2026-03-01,Bank Fee,-500\n2026-03-02,Client wire,12000\n"


def _seed_upload(company_id: str, user_id: str, content: bytes, filename: str, content_type: str) -> str:
    file_id = f"ai_up_{_uuid.uuid4().hex}"
    d = upload_dir()
    (d / f"{file_id}.bin").write_bytes(content)
    (d / f"{file_id}.meta").write_text(json.dumps({
        "filename": filename, "content_type": content_type, "size": len(content),
        "company_id": company_id, "user_id": user_id,
    }))
    return file_id


def _xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _company_id(client: AsyncClient, headers: dict) -> str:
    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _user_id(headers: dict) -> str:
    from celerp.services.auth import decode_access_token
    return str(decode_access_token(headers["Authorization"].split(" ", 1)[1])["sub"])


async def _second_company(client: AsyncClient, headers: dict) -> dict:
    """Registration is locked after bootstrap, so a second tenant is created
    through the companies API from the first user's session."""
    r = await client.post("/companies", json={"name": f"Other Co {_uuid.uuid4().hex[:6]}"}, headers=headers)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _import_file(client: AsyncClient, headers: dict, sid: str, file_id: str, column_map: dict | None = None):
    body = {"file_id": file_id}
    if column_map:
        body["column_map"] = column_map
    return await client.post(f"/accounting/reconciliation/{sid}/import-file", json=body, headers=headers)


async def _lines(client: AsyncClient, headers: dict, sid: str) -> list[dict]:
    r = await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_reconciliation_capabilities_compile():
    compiled = ai_tools.compile_agent_capabilities(real_agent_app(), {})
    routes = {(cap["method"], cap["path"]) for cap in compiled.values()}
    assert _AGENT_RECON_ROUTES <= routes
    for cap in compiled.values():
        if (cap["method"], cap["path"]) not in _AGENT_RECON_ROUTES:
            continue
        assert cap["inject_idempotency"] is False
        body = cap["tool"]["function"]["parameters"]["properties"].get("body", {})
        assert "idempotency_key" not in body.get("properties", {})
    assert ("GET", "/accounting/reconciliation/{session_id}/statement-lines") not in routes
    assert ("POST", "/accounting/reconciliation/{session_id}/import-csv") not in routes


@pytest.mark.asyncio
async def test_import_file_csv_and_xlsx(client):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])

    csv_id = _seed_upload(company_id, _user_id(h), _CSV, "march.csv", "text/csv")
    r = await _import_file(client, h, recon["id"], csv_id)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "needs_mapping": False, "session_id": recon["id"], "rows_imported": 2, "csv_filename": "march.csv",
    }
    from_csv = [(l["line_date"], l["description"], l["amount"]) for l in await _lines(client, h, recon["id"])]

    xlsx_id = _seed_upload(
        company_id, _user_id(h),
        _xlsx([["Date", "Description", "Amount"], ["2026-03-01", "Bank Fee", -500], ["2026-03-02", "Client wire", 12000]]),
        "march.xlsx", XLSX_CONTENT_TYPE,
    )
    r = await _import_file(client, h, recon["id"], xlsx_id)
    assert r.status_code == 200, r.text
    assert r.json()["rows_imported"] == 2
    from_xlsx = [(l["line_date"], l["description"], l["amount"]) for l in await _lines(client, h, recon["id"])]
    assert from_xlsx == from_csv

    odd_id = _seed_upload(company_id, _user_id(h), b"When,What,HowMuch\n2026-03-01,Fee,-5\n", "odd.csv", "text/csv")
    r = await _import_file(client, h, recon["id"], odd_id)
    assert r.status_code == 200, r.text
    assert r.json()["needs_mapping"] is True
    assert r.json()["headers"] == ["When", "What", "HowMuch"]
    r = await _import_file(client, h, recon["id"], odd_id, {"date": "When", "description": "What", "amount": "HowMuch"})
    assert r.status_code == 200, r.text
    assert r.json()["rows_imported"] == 1

    r = await _import_file(client, h, recon["id"], "ai_up_missing")
    assert r.status_code == 422
    assert "ai_up_missing" in r.json()["detail"]

    other = await _second_company(client, h)
    other_company = await _company_id(client, other)
    foreign_id = _seed_upload(other_company, _user_id(other), _CSV, "theirs.csv", "text/csv")
    r = await _import_file(client, h, recon["id"], foreign_id)
    assert r.status_code == 422
    assert foreign_id in r.json()["detail"]

    broken_id = _seed_upload(company_id, _user_id(h), b"not a workbook", "broken.xlsx", XLSX_CONTENT_TYPE)
    r = await _import_file(client, h, recon["id"], broken_id)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_statement_import_exact_replay_keeps_line_ids_and_progress(client):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    file_id = _seed_upload(company_id, _user_id(h), _CSV, "march.csv", "text/csv")
    first = await _import_file(client, h, recon["id"], file_id)
    assert first.status_code == 200, first.text
    before = await _lines(client, h, recon["id"])

    replay = await _import_file(client, h, recon["id"], file_id)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    after = await _lines(client, h, recon["id"])
    assert [row["id"] for row in after] == [row["id"] for row in before]


@pytest.mark.asyncio
async def test_statement_replacement_refused_after_progress(client):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    first_id = _seed_upload(company_id, _user_id(h), _CSV, "march.csv", "text/csv")
    assert (await _import_file(client, h, recon["id"], first_id)).status_code == 200
    line = (await _lines(client, h, recon["id"]))[0]
    skipped = await client.patch(
        f"/accounting/reconciliation/{recon['id']}/lines/{line['id']}",
        json={"status": "skipped"}, headers=h,
    )
    assert skipped.status_code == 200

    changed = _seed_upload(
        company_id, _user_id(h),
        b"Date,Description,Amount\n2026-03-01,Different,-6\n",
        "changed.csv", "text/csv",
    )
    r = await _import_file(client, h, recon["id"], changed)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "statement_has_progress"


@pytest.mark.asyncio
async def test_generic_line_patch_cannot_manufacture_resolved_state(client):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    file_id = _seed_upload(company_id, _user_id(h), _CSV, "march.csv", "text/csv")
    assert (await _import_file(client, h, recon["id"], file_id)).status_code == 200
    line_id = (await _lines(client, h, recon["id"]))[0]["id"]
    for status in ("matched", "created"):
        r = await client.patch(
            f"/accounting/reconciliation/{recon['id']}/lines/{line_id}",
            json={"status": status}, headers=h,
        )
        assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_workbench_caps_and_scopes(client):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    rows = "".join(f"2026-03-01,Line {i},-{i + 1}\n" for i in range(501))
    file_id = _seed_upload(company_id, _user_id(h), b"Date,Description,Amount\n" + rows.encode(), "big.csv", "text/csv")
    r = await _import_file(client, h, recon["id"], file_id)
    assert r.status_code == 200, r.text
    assert r.json()["rows_imported"] == 501

    r = await client.get(f"/accounting/reconciliation/{recon['id']}/workbench", headers=h)
    assert r.status_code == 200, r.text
    wb = r.json()
    assert len(wb["statement_lines"]) == 500
    assert wb["statement_lines_truncated"] is True
    assert wb["book_entries_truncated"] is False
    assert wb["session"]["id"] == recon["id"]
    assert wb["bank_account"]["id"] == bank["id"]
    assert wb["difference"] == pytest.approx(5000.0)

    other = await _second_company(client, h)
    r = await client.get(f"/accounting/reconciliation/{recon['id']}/workbench", headers=other)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_line_create_rejects_unknown_account_and_repeats_cleanly(client, session):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    file_id = _seed_upload(company_id, _user_id(h), _CSV, "march.csv", "text/csv")
    assert (await _import_file(client, h, recon["id"], file_id)).status_code == 200
    line_id = (await _lines(client, h, recon["id"]))[0]["id"]
    url = f"/accounting/reconciliation/{recon['id']}/lines/{line_id}/create"

    r = await client.post(url, json={"account_code": "9999", "memo": "Fee"}, headers=h)
    assert r.status_code == 422
    assert "9999" in r.json()["detail"]

    partial = await client.post(url, json={"account_code": "6100", "memo": "Fee", "amount": 1}, headers=h)
    assert partial.status_code == 422, partial.text
    assert "Partial" in partial.json()["detail"]

    r = await client.post(url, json={"account_code": "6100", "memo": "Fee"}, headers=h)
    assert r.status_code == 200, r.text
    je_id = r.json()["matched_je_id"]
    assert r.json()["status"] == "created"

    r = await client.post(url, json={"account_code": "6100", "memo": "Fee"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["matched_je_id"] == je_id
    recon_state = (await client.get(f"/accounting/reconciliation/{recon['id']}", headers=h)).json()
    assert recon_state["created_count"] == 1
    assert recon_state["reconciled_je_ids"].count(je_id) == 1

    # Unmatching a generated entry must reverse it in the books, not merely hide
    # it from reconciliation state. A later correction creates a new immutable JE.
    unmatch = await client.post(
        f"/accounting/reconciliation/{recon['id']}/lines/{line_id}/unmatch", headers=h,
    )
    assert unmatch.status_code == 200, unmatch.text
    assert unmatch.json()["status"] == "unmatched"
    old_je = await session.get(
        Projection, {"company_id": _uuid.UUID(company_id), "entity_id": je_id},
        populate_existing=True,
    )
    assert old_je is not None
    assert old_je.state["status"] == "void"

    corrected = await client.post(url, json={"account_code": "6100", "memo": "Corrected fee"}, headers=h)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["status"] == "created"
    assert corrected.json()["matched_je_id"] != je_id


@pytest.mark.asyncio
async def test_start_is_idempotent_for_same_statement(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    first = await _create_recon(client, h, bank["id"])
    again = await _create_recon(client, h, bank["id"])
    assert again["id"] == first["id"]

    mismatch = await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank["id"], "statement_date": first["statement_date"],
        "statement_balance": float(first["statement_balance"]) + 1,
    }, headers=h)
    assert mismatch.status_code == 409, mismatch.text

    r = await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank["id"], "statement_date": "2026-04-30", "statement_balance": 1.0,
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] != first["id"]


@pytest.mark.asyncio
async def test_write_off_rejects_missing_account(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    r = await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 100000.5,
    }, headers=h)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", json={"account_code": "9999"}, headers=h)
    assert r.status_code == 422
    assert "9999" in r.json()["detail"]
