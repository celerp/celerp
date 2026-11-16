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
from ui.i18n import refusal_text, t

from celerp.ai import tools as ai_tools
from celerp.ai.files import XLSX_CONTENT_TYPE, upload_dir
from celerp.models.projections import Projection

from test_helpers import real_agent_app
import bank_statement_helpers as B
from test_reconciliation_v2 import _auth, _create_bank, _create_recon

pytestmark = pytest.mark.usefixtures("tools_open_when_called")

# Routes the assistant drives without a key of its own.
_AGENT_RECON_ROUTES = {
    ("GET", "/accounting/bank-accounts"),
    ("POST", "/accounting/reconciliation/start"),
    ("GET", "/accounting/reconciliation/{session_id}/workbench"),
    ("POST", "/accounting/reconciliation/{session_id}/write-off"),
    ("GET", "/accounting/reconciliation/{session_id}"),
    ("GET", "/accounting/reconciliation/{session_id}/statement-lines"),
    ("POST", "/accounting/reconciliation/{session_id}/match"),
    ("POST", "/accounting/reconciliation/{session_id}/unmatch"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/attach"),
    ("DELETE", "/accounting/reconciliation/{session_id}/lines/{line_id}/attach/{att_id}"),
}
# The bank statement writes: each replays under its idempotency key, and those that act
# on lines name the request field that selects them.
_AGENT_BANK_ROUTES = {
    ("POST", "/accounting/reconciliation/read-statement"): None,
    ("POST", "/accounting/reconciliation/import-statement"): "statements",
    ("POST", "/accounting/reconciliation/bank/{bank_id}/resolve"): "decisions[].line_ids accept.line_ids",
    ("POST", "/accounting/reconciliation/bank/{bank_id}/suggest"): None,
    ("POST", "/accounting/reconciliation/bank/{bank_id}/unmatch"): "line_ids",
    ("POST", "/accounting/reconciliation/bank/{bank_id}/complete"): None,
    ("DELETE", "/accounting/reconciliation/{session_id}"): None,
}
_REMOVED = {
    ("POST", "/accounting/reconciliation/{session_id}/import-file"),
    ("POST", "/accounting/reconciliation/{session_id}/import-csv"),
    ("POST", "/accounting/reconciliation/{session_id}/auto-match"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/match"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/unmatch"),
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/create"),
    ("PATCH", "/accounting/reconciliation/{session_id}/lines/{line_id}"),
    ("POST", "/accounting/reconciliation/{session_id}/bulk-confirm"),
    ("POST", "/accounting/reconciliation/{session_id}/complete"),
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


async def _read(client: AsyncClient, headers: dict, bank_id: str, file_id: str, column_map: dict | None = None):
    """read-statement over one kept file; the preview once the job is done (the response when refused)."""
    from celerp.ai import jobs
    from test_helpers import until
    r = await client.post(f"{B.API}/read-statement", headers=headers,
                          json={"file_ids": [file_id], "bank_id": bank_id, "idempotency_key": B.key()})
    if r.status_code != 200:
        return r
    job_id = r.json()["job_id"]
    await until(lambda: not jobs.running_tasks())
    if column_map:
        p = await client.post(f"{B.API}/statement-preview/{job_id}", headers=headers, json={
            "statements": [{"header": {}}], "column_map": column_map})
    else:
        p = await client.get(f"{B.API}/statement-preview/{job_id}", headers=headers)
    assert p.status_code == 200, p.text
    return {**p.json(), "job_id": job_id}


def _rows(preview: dict) -> list[tuple]:
    return [(l["line_date"], l["description"], str(l["amount"])) for st in preview["statements"] for l in st["lines"]]


async def _lines(client: AsyncClient, headers: dict, sid: str) -> list[dict]:
    r = await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_reconciliation_capabilities_compile():
    compiled = ai_tools.compile_agent_capabilities(real_agent_app(), {})
    caps = {(cap["method"], cap["path"]): cap for cap in compiled.values()}
    assert _AGENT_RECON_ROUTES <= set(caps)
    for route in _AGENT_RECON_ROUTES:
        assert caps[route]["key_field"] is None
        body = caps[route]["tool"]["function"]["parameters"]["properties"].get("body", {})
        assert "idempotency_key" not in body.get("properties", {})
    for route, selection in _AGENT_BANK_ROUTES.items():
        assert route in caps, route
        assert (caps[route]["key_field"], caps[route]["selection"]) == ("idempotency_key", selection), route
    assert caps[("POST", "/accounting/reconciliation/read-statement")]["credits"] is True
    assert not _REMOVED & set(caps)


@pytest.mark.asyncio
async def test_read_statement_csv_and_xlsx(client):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)

    csv_id = _seed_upload(company_id, _user_id(h), _CSV, "march.csv", "text/csv")
    from_csv = _rows(await _read(client, h, bank["id"], csv_id))
    assert from_csv == [("2026-03-01", "Bank Fee", "-500.00"), ("2026-03-02", "Client wire", "12000.00")]

    xlsx_id = _seed_upload(
        company_id, _user_id(h),
        _xlsx([["Date", "Description", "Amount"], ["2026-03-01", "Bank Fee", -500], ["2026-03-02", "Client wire", 12000]]),
        "march.xlsx", XLSX_CONTENT_TYPE,
    )
    assert [(d, desc, float(a)) for d, desc, a in _rows(await _read(client, h, bank["id"], xlsx_id))] == \
        [(d, desc, float(a)) for d, desc, a in from_csv]

    odd_id = _seed_upload(company_id, _user_id(h), b"When,What,HowMuch\n2026-03-01,Fee,-5\n", "odd.csv", "text/csv")
    odd = await _read(client, h, bank["id"], odd_id)
    assert (odd["needs_mapping"], odd["headers"]) == (True, ["When", "What", "HowMuch"])
    mapped = await _read(client, h, bank["id"], odd_id, {"date": "When", "description": "What", "amount": "HowMuch"})
    assert _rows(mapped) == [("2026-03-01", "Fee", "-5.00")]

    r = await _read(client, h, bank["id"], "ai_up_missing")
    assert r.status_code == 404
    assert r.json()["detail"]["message_key"] == "ai.err_file_not_found"

    other = await _second_company(client, h)
    other_company = await _company_id(client, other)
    foreign_id = _seed_upload(other_company, _user_id(other), _CSV, "theirs.csv", "text/csv")
    r = await _read(client, h, bank["id"], foreign_id)
    assert r.status_code == 404
    assert r.json()["detail"]["message_key"] == "ai.err_file_not_found"

    broken_id = _seed_upload(company_id, _user_id(h), b"not a workbook", "broken.xlsx", XLSX_CONTENT_TYPE)
    broken = await _read(client, h, bank["id"], broken_id)
    assert broken["statements"] == [] and len(broken["failed_steps"]) == 1, broken


@pytest.mark.asyncio
async def test_statement_import_exact_replay_keeps_line_ids_and_progress(client):
    h = await _auth(client)
    co = await B.co_of(client, h)
    bank = await _create_bank(client, h)
    preview = await B.read(client, co, [(_CSV, "march.csv", "text/csv")], bank_id=bank["id"])
    header = {"opening_balance": "100000.00", "closing_balance": "111500.00",
              "period_start": "2026-03-01", "period_end": "2026-03-31"}
    idem = B.key()
    first = await B.import_statement(client, co, preview, bank_id=bank["id"], header=header, idem=idem, tick_all=True)
    sid = first["sessions"][0]["id"]
    before = await _lines(client, h, sid)
    line = before[0]
    assert (await B.decide(client, h, sid, line["id"], "no_entry")).json()["results"][0]["ok"]

    replay = await B.import_statement(client, co, preview, bank_id=bank["id"], header=header, idem=idem, tick_all=True)
    assert replay == first
    after = await _lines(client, h, sid)
    assert [row["id"] for row in after] == [row["id"] for row in before]
    assert next(row for row in after if row["id"] == line["id"])["status"] == "skipped"


@pytest.mark.asyncio
async def test_statement_replacement_refused_after_progress(client):
    """A statement with a settled line is neither removed nor imported again for its date."""
    h = await _auth(client)
    co = await B.co_of(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    data = await B.import_into(client, h, recon["id"], _CSV, "march.csv")
    sid = data["session_id"]
    assert (await B.decide(client, h, sid, data["lines"][0]["id"], "no_entry")).json()["results"][0]["ok"]

    r = await client.request("DELETE", f"{B.API}/{sid}", json={"idempotency_key": B.key()}, headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "acct.err_recon_delete_settled"

    changed = await B.read(client, co, [(b"Date,Description,Amount\n2026-03-01,Different,-6\n", "changed.csv",
                                         "text/csv")], bank_id=bank["id"])
    await B.import_statement(client, co, changed, bank_id=bank["id"], tick_all=True, expect=409, header={
        "opening_balance": "105006.00", "closing_balance": "105000.00",
        "period_start": "2026-03-01", "period_end": "2026-03-31"})
    assert [row["description"] for row in await _lines(client, h, sid)] == ["Bank Fee", "Client wire"]


@pytest.mark.asyncio
async def test_no_decision_sets_a_resolved_state_directly(client):
    """A line is matched or posted only through what it matches or posts: a decision that
    names a state instead of an answer is refused and the line stays open."""
    h = await _auth(client)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    data = await B.import_into(client, h, recon["id"], _CSV, "march.csv")
    sid, line_id = data["session_id"], data["lines"][0]["id"]
    for status in ("matched", "created", "posted"):
        r = await B.decide(client, h, sid, line_id, status)
        assert r.status_code == 422, r.text
    r = await B.decide(client, h, sid, line_id, "match", je_ids=[])
    assert r.status_code == 200 and not r.json()["results"][0]["ok"], r.text
    assert (await _lines(client, h, sid))[0]["status"] == "unmatched"


@pytest.mark.asyncio
async def test_workbench_caps_and_scopes(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    rows = "".join(f"2026-03-01,Line {i},-{i + 1}\n" for i in range(501))
    data = await B.import_into(client, h, recon["id"], b"Date,Description,Amount\n" + rows.encode(), "big.csv")
    sid = data["session_id"]
    assert len(data["lines"]) == 501

    r = await client.get(f"/accounting/reconciliation/{sid}/workbench", headers=h)
    assert r.status_code == 200, r.text
    wb = r.json()
    assert len(wb["statement_lines"]) == 500
    assert wb["statement_lines_truncated"] is True
    assert wb["book_entries_truncated"] is False
    assert wb["session"]["id"] == sid
    assert wb["bank_account"]["id"] == bank["id"]
    assert wb["difference"] == pytest.approx(5000.0)

    other = await _second_company(client, h)
    r = await client.get(f"/accounting/reconciliation/{sid}/workbench", headers=other)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_line_post_rejects_unknown_account_and_repeats_cleanly(client, session):
    h = await _auth(client)
    company_id = await _company_id(client, h)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    data = await B.import_into(client, h, recon["id"], _CSV, "march.csv")
    sid, line_id = data["session_id"], data["lines"][0]["id"]

    def post(memo: str, account: str = "6100", idem: str | None = None):
        return client.post(f"{B.API}/bank/{bank['id']}/resolve", headers=h, json={
            "idempotency_key": idem or B.key(),
            "decisions": [{"line_ids": [line_id], "kind": "post", "account_code": account, "memo": memo}]})

    r = await post("Fee", "9999")
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"] is False
    assert "9999" in r.json()["results"][0]["reason"]

    idem = B.key()
    r = await post("Fee", idem=idem)
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    (je_id,) = r.json()["results"][0]["je_ids"]

    replay = await post("Fee", idem=idem)
    assert replay.json() == r.json()
    again = await post("Fee")
    assert again.json()["results"][0]["ok"] is False
    recon_state = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert recon_state["reconciled_je_ids"].count(je_id) == 1

    # Undoing a posted line reverses its entry in the books, not merely hides it from the
    # reconciliation. A later correction posts a new immutable entry.
    unmatch = await B.undo(client, h, sid, [line_id])
    assert unmatch.status_code == 200, unmatch.text
    assert (await _lines(client, h, sid))[0]["status"] == "unmatched"
    old_je = await session.get(
        Projection, {"company_id": _uuid.UUID(company_id), "entity_id": je_id},
        populate_existing=True,
    )
    assert old_je is not None
    assert old_je.state["status"] == "void"

    corrected = await post("Corrected fee")
    assert corrected.json()["results"][0]["ok"], corrected.text
    assert corrected.json()["results"][0]["je_ids"] != [je_id]


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
    assert r.json()["detail"]["message"] == "Account 9999 is not in the chart of accounts."
