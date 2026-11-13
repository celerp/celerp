# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""With funds in use, a journal entry posted from a bank statement line names its fund:
the create form has one Fund choice for the entry, the split form one per split, each
Unrestricted until a fund is picked. The bank side of each fund's part moves with it, so
every fund balances on its own; Unrestricted sends no fund."""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

CSV = b"Date,Description,Amount\n2026-03-05,Library gift,300\n2026-03-14,Supplies,-120\n"


async def _fund(api, name: str, restricted: bool) -> str:
    r = await api.post("/accounting/funds", json={"request_key": uuid.uuid4().hex, "name": name,
                                                  "restricted": restricted, "purpose": "Library" if restricted else None})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _statement(api) -> tuple[str, dict, dict]:
    bank = (await api.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": 1000.0})).json()
    r = await api.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 1180.0})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await api.post(f"/accounting/reconciliation/{sid}/import-csv", files={"file": ("s.csv", CSV, "text/csv")})
    assert r.status_code == 200, r.text
    lines = {l["description"]: l for l in
             (await api.get(f"/accounting/reconciliation/{sid}/statement-lines")).json()["items"]}
    return sid, bank, lines


async def _entries(session, je_id: str) -> list[tuple[str, float, float, str | None]]:
    session.expire_all()
    row = (await session.execute(select(Projection).where(Projection.entity_id == je_id))).scalar_one()
    return sorted((e["account"], e["debit"], e["credit"], e.get("fund_id")) for e in row.state["entries"])


async def _line(api, sid: str, line_id: str) -> dict:
    items = (await api.get(f"/accounting/reconciliation/{sid}/statement-lines")).json()["items"]
    return next(l for l in items if l["id"] == line_id)


async def test_with_funds_on_a_statement_line_entry_names_its_fund(owner_ui, session):
    api = owner_ui.api
    assert (await api.put("/accounting/funds/setting", json={"enabled": True})).status_code == 200
    library = await _fund(api, "Library", True)
    reserve = await _fund(api, "Board reserve", False)
    sid, bank, lines = await _statement(api)
    code = bank["chart_account_code"]
    gift, supplies = lines["Library gift"]["id"], lines["Supplies"]["id"]

    form = (await owner_ui.get(f"/accounting/reconcile/{sid}/lines/{gift}/create-form")).text
    assert re.findall(r'<input[^>]*name="fund"[^>]*value="([^"]*)"', form) == ["unrestricted"], form
    assert "Library" in form and "Board reserve" in form
    split = (await owner_ui.get(f"/accounting/reconcile/{sid}/lines/{supplies}/split-form")).text
    assert re.findall(r'<input[^>]*name="fund_0"[^>]*value="([^"]*)"', split) == ["unrestricted"], split

    r = await owner_ui.post(f"/accounting/reconcile/{sid}/lines/{gift}/create-confirm",
                            data={"account_code": "4100", "memo": "Gift", "amount": "300", "fund": library})
    assert r.status_code == 200, r.text
    je = (await _line(api, sid, gift))["matched_je_id"]
    assert await _entries(session, je) == sorted([(code, 300.0, 0.0, library), ("4100", 0.0, 300.0, library)])

    r = await owner_ui.post(f"/accounting/reconcile/{sid}/lines/{supplies}/split-confirm", data={
        "account_code_0": "6200", "amount_0": "70", "fund_0": reserve,
        "account_code_1": "6200", "amount_1": "50", "fund_1": "unrestricted"})
    assert r.status_code == 200, r.text
    je = (await _line(api, sid, supplies))["matched_je_id"]
    assert je, r.text
    assert await _entries(session, je) == sorted([
        (code, 0.0, 70.0, reserve), ("6200", 70.0, 0.0, reserve),
        (code, 0.0, 50.0, None), ("6200", 50.0, 0.0, None)])


async def test_without_funds_the_statement_line_forms_have_no_fund_choice(owner_ui):
    api = owner_ui.api
    sid, _, lines = await _statement(api)
    gift, supplies = lines["Library gift"]["id"], lines["Supplies"]["id"]
    assert 'name="fund"' not in (await owner_ui.get(f"/accounting/reconcile/{sid}/lines/{gift}/create-form")).text
    assert 'name="fund_0"' not in (await owner_ui.get(f"/accounting/reconcile/{sid}/lines/{supplies}/split-form")).text
    r = await api.post(f"/accounting/reconciliation/{sid}/lines/{gift}/create",
                       json={"account_code": "4100", "fund_id": str(uuid.uuid4())})
    assert r.status_code == 422 and "fund" in r.text.lower(), r.text
