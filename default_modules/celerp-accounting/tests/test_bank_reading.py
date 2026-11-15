# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Statements and receipts read by the AI reading job: page steps for long PDFs, a
statement and a receipt told apart in one drop, a page that cannot be read, Read again
for the failed steps only, and the model rung of Suggest charged once per key."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import uuid

import pytest
from sqlalchemy import select, text

import bank_statement_helpers as B

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from ai_fake_cloud import FakeCloud, answer, install  # noqa: E402

pytestmark = pytest.mark.asyncio


def _pdf(pages: int, marker: str = "STATEMENT") -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": marker})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _statement_reply(lines=None, *, opening="0.00", closing=None, start="2026-01-01", end="2026-01-31"):
    lines = lines or [{"line_date": "2026-01-05", "description": "FEE", "amount": "-5.00"}]
    closing = closing or f"{float(opening) + sum(float(l['amount']) for l in lines):.2f}"
    return answer(json.dumps({"kind": "statement", "header": {
        "period_start": start, "period_end": end, "opening_balance": opening, "closing_balance": closing,
        "currency": "USD"}, "lines": lines}))


def _receipt_reply():
    return answer(json.dumps({"kind": "receipt", "date": "2026-01-05", "total": "107.00", "tax": "7.00",
                              "tax_id": "0105551234567", "is_tax_invoice": True, "party": "Siam Supplies"}))


def _sent_text(body: dict) -> str:
    return json.dumps(body.get("messages", body))


async def _steps(session, job_id) -> list:
    from celerp.models.ai import AIJobStep
    session.expire_all()
    rows = (await session.execute(select(AIJobStep.key, AIJobStep.state, AIJobStep.result).where(
        AIJobStep.job_id == uuid.UUID(job_id)).order_by(AIJobStep.seq))).all()
    return [tuple(r) for r in rows]


async def test_long_pdf_reads_in_page_steps(client, session, monkeypatch):
    cloud = install(monkeypatch, FakeCloud(lambda identity, body: _statement_reply()))
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    preview = await B.read(client, co, [(_pdf(7), "march.pdf", "application/pdf")], bank_id=bk["id"])
    keys = [k for k, _s, _r in await _steps(session, preview["job_id"])]
    assert [k.rsplit(":", 1)[1] for k in keys] == ["1-3", "4-6", "7-7"]
    assert len(set(k.rsplit(":", 1)[0] for k in keys)) == 1
    assert sum(cloud.executions.values()) == 3


async def test_unreadable_statement_reports_reason(client, session, monkeypatch):
    install(monkeypatch, FakeCloud(lambda identity, body: answer("I cannot read this page.")))
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    preview = await B.read(client, co, [(_pdf(2), "blurry.pdf", "application/pdf")], bank_id=bk["id"])
    failed = preview["failed_steps"]
    assert len(failed) == 1 and failed[0]["filename"] == "blurry.pdf" and failed[0]["pages"] == "1-2"
    assert failed[0]["reason"] and failed[0]["retry"] is True


async def test_reading_job_classifies_statement_and_receipt(client, session, monkeypatch):
    install(monkeypatch, FakeCloud(lambda identity, body: _receipt_reply()
                                   if "slip.jpg" in _sent_text(body) else _statement_reply()))
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    preview = await B.read(client, co, [(_pdf(1), "jan.pdf", "application/pdf"), (jpeg, "slip.jpg", "image/jpeg")],
                           bank_id=bk["id"])
    assert len(preview["statements"]) == 1
    assert [r["filename"] for r in preview["receipts"]] == ["slip.jpg"]
    assert preview["receipts"][0]["total"] == "107.00"


async def test_read_statement_retry_reads_failed_steps_only(client, session, monkeypatch):
    import httpx
    fail = {"on": True}

    def script(identity, body):
        if fail["on"] and identity in fail.get("ids", set()):
            cloud.state.pop(identity, None)  # the service never ran it
            return httpx.ConnectError("relay down")
        return _statement_reply()
    cloud = install(monkeypatch, FakeCloud(script))
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    from celerp.ai import jobs
    # The second page step fails: pick its identity from the job once it exists.
    real_job = jobs.file_operation_id

    def remember(job_id, key, generation):
        op = real_job(job_id, key, generation)
        if key.endswith(":4-6") and generation == 0:
            fail.setdefault("ids", set()).add(str(op))
        return op
    monkeypatch.setattr(jobs, "file_operation_id", remember)
    idem = B.key()
    file_id = B.upload(co["company_id"], co["user_id"], _pdf(7), "march.pdf", "application/pdf")
    body = {"file_ids": [file_id], "bank_id": bk["id"], "idempotency_key": idem}
    r = await client.post(f"{B.API}/read-statement", json=body, headers=co["h"])
    job_id = r.json()["job_id"]
    await B.until(lambda: not jobs.running_tasks())
    preview = (await client.get(f"{B.API}/statement-preview/{job_id}", headers=co["h"])).json()
    assert [f["pages"] for f in preview["failed_steps"]] == ["4-6"]
    assert "4" in preview["failed_steps"][0]["text"] and "march.pdf" in preview["failed_steps"][0]["text"]
    replay = await client.post(f"{B.API}/read-statement", json=body, headers=co["h"])
    assert replay.json()["job_id"] == job_id
    sent_before = sum(cloud.executions.values())
    fail["on"] = False
    again = await client.post(f"{B.API}/read-statement", json={**body, "job_id": job_id, "idempotency_key": B.key()},
                              headers=co["h"])
    assert again.status_code == 200, again.text
    await B.until(lambda: not jobs.running_tasks())
    assert sum(cloud.executions.values()) == sent_before + 1
    preview = (await client.get(f"{B.API}/statement-preview/{again.json()['job_id']}", headers=co["h"])).json()
    assert preview["failed_steps"] == []


async def test_model_rung_replay_charges_once(client, session, monkeypatch):
    def script(identity, body):
        groups = [m for m in body.get("messages", []) if m.get("role") == "user"]
        return answer(json.dumps({"SOMETHING NEW CO": {"account_code": "5100", "reason": "Supplies"}})) if groups \
            else answer("{}")
    cloud = install(monkeypatch, FakeCloud(script))
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], [("2026-01-05", "SOMETHING NEW CO", "-40.00")], opening="0.00",
                      closing="-40.00", start="2026-01-01", end="2026-01-31")
    before = sum(cloud.executions.values())
    idem = B.key()
    first = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": idem}, headers=co["h"])
    again = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": idem}, headers=co["h"])
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert sum(cloud.executions.values()) == before + 1
    used = (await session.execute(text("SELECT count(*) FROM ai_usage WHERE company_id = :c"),
                                  {"c": co["company_id"]})).scalar_one()
    assert used == 1


async def test_bank_statements_year_end_to_end(client, session, monkeypatch):
    """A blank company: three monthly PDF statements and five slips in one drop, a new bank
    from the first statement's opening, one group table, Post ticked, a month-1 rule
    suggesting in month 3, slips matched, a free-text answer, a client answer, Suspense
    reclassified, all three complete and the bank balance equal to the last closing."""
    from test_helpers import grant_permission
    months = {
        "jan.pdf": ("2026-01-01", "2026-01-31", "1000.00", [
            {"line_date": "2026-01-05", "description": "NETFLIX.COM", "amount": "-15.99"},
            {"line_date": "2026-01-09", "description": "POS STATIONERY SHOP", "amount": "-107.00"},
            {"line_date": "2026-01-15", "description": "WITHDRAWAL", "amount": "-200.00"}]),
        "feb.pdf": ("2026-02-01", "2026-02-28", "677.01", [
            {"line_date": "2026-02-05", "description": "NETFLIX.COM", "amount": "-15.99"},
            {"line_date": "2026-02-10", "description": "TRANSFER", "amount": "-50.00"}]),
        "mar.pdf": ("2026-03-01", "2026-03-31", "611.02", [
            {"line_date": "2026-03-05", "description": "NETFLIX.COM", "amount": "-15.99"},
            {"line_date": "2026-03-12", "description": "POS STATIONERY SHOP", "amount": "-53.50"}]),
    }
    slips = {f"slip{i}.jpg": ("2026-01-09", "107.00") if i == 0 else (f"2026-03-1{i}", "53.50" if i == 2 else "9.99")
             for i in range(5)}

    def script(identity, body):
        sent = _sent_text(body)
        for name, (start, end, opening, lines) in months.items():
            if name in sent:
                return _statement_reply(lines, opening=opening, start=start, end=end)
        for name, (d, total) in slips.items():
            if name in sent:
                return answer(json.dumps({"kind": "receipt", "date": d, "total": total, "tax": None,
                                          "tax_id": None, "is_tax_invoice": False, "party": None}))
        return answer("{}")
    install(monkeypatch, FakeCloud(script))
    co = await B.company(client)
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    files = [(_pdf(1), n, "application/pdf") for n in months] + [(jpeg, n, "image/jpeg") for n in slips]
    preview = await B.read(client, co, files)
    assert len(preview["statements"]) == 3 and len(preview["receipts"]) == 5
    assert all(st["tie"]["ok"] for st in preview["statements"])
    out = await B.import_statement(client, co, preview, new_bank={**preview["new_bank"], "bank_name": "Main"})
    bank_id = out["bank_id"]
    assert len(out["sessions"]) == 3
    bank = await B.bank_row(client, co["h"], bank_id)
    assert bank["opening_balance"] == 1000.0
    lines = B.all_lines(await B.groups(client, co, bank_id))
    jan_netflix = next(l for l in lines if l["description"] == "NETFLIX.COM" and l["line_date"].startswith("2026-01"))
    r = await B.resolve(client, co, bank_id, decisions=[{"line_ids": [jan_netflix["id"]], "kind": "post",
                                                         "account_code": "5100", "remember": True}])
    assert r.json()["results"][0]["ok"]
    await client.post(f"{B.API}/bank/{bank_id}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    table = await B.groups(client, co, bank_id)
    mar_netflix = next(l for l in B.all_lines(table) if l["description"] == "NETFLIX.COM" and l["line_date"].startswith("2026-03"))
    assert mar_netflix["suggestion"]["source"] == "rule"
    slip_lines = [l for l in B.all_lines(table) if l["description"] == "POS STATIONERY SHOP"]
    assert all(l["suggestion"]["source"] == "slip" for l in slip_lines)
    ticked = [g["key"] for g in table["groups"] if g["ticked"]]
    r = await B.resolve(client, co, bank_id, accept={"group_keys": ticked})
    assert r.status_code == 200 and r.json()["refused"] == 0
    # A free-text answer on TRANSFER, the client answers WITHDRAWAL, then WITHDRAWAL goes to Suspense first.
    table = await B.groups(client, co, bank_id)
    transfer = B.line_by_text(table, "TRANSFER")
    withdrawal = B.line_by_text(table, "WITHDRAWAL")
    r = await B.resolve(client, co, bank_id, decisions=[{"line_ids": [transfer["id"]], "kind": "post", "account_code": "5200"}])
    assert r.json()["results"][0]["ok"]
    await grant_permission(client, co["h"], "answer_bank_questions", "viewer")
    client_h = await B.member(client, session, co, "viewer")
    monkeypatch.setattr("celerp.services.email.send_email", _ok_email)
    r = await client.post(f"{B.API}/bank/{bank_id}/ask", json={"line_ids": [withdrawal["id"]], "note": "What was this?",
                                                                "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/bank-questions/{withdrawal['id']}/answer", json={
        "answer": "office rent", "idempotency_key": B.key()}, headers=client_h)
    assert r.status_code == 200, r.text
    r = await B.resolve(client, co, bank_id, decisions=[{"line_ids": [withdrawal["id"]], "kind": "suspense"}])
    assert r.json()["results"][0]["ok"]
    r = await client.post(f"{B.API}/lines/{withdrawal['id']}/reclassify", json={"account_code": "6100",
                                                                               "idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    r = await client.post(f"{B.API}/bank/{bank_id}/complete", json={"idempotency_key": B.key()}, headers=co["h"])
    assert len(r.json()["completed"]) == 3, r.text
    tb = (await client.get("/accounting/trial-balance", params={"date_to": "2026-03-31"}, headers=co["h"])).json()
    bank_code = (await B.bank_row(client, co["h"], bank_id))["chart_account_code"]
    assert {line["code"]: round(float(line["net"]), 2) for line in tb["lines"]}[bank_code] == 541.53


async def _ok_email(to, subject, body_html, body_text="", **kw):
    from celerp.services.email import EmailResult
    return EmailResult(True, "sent")
