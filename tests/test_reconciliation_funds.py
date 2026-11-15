# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""With funds in use, a journal entry posted from a bank statement line names its fund:
the bank page's answer form has one Fund choice for the entry, the split form one per part, each
Unrestricted until a fund is picked. The bank side of each fund's part moves with it, so
every fund balances on its own; Unrestricted sends no fund."""
from __future__ import annotations

import html
import re
import uuid

import pytest
from sqlalchemy import select

import bank_statement_helpers as B
from celerp.models.projections import Projection
from ui.i18n import t

pytestmark = pytest.mark.asyncio

ROWS = [("2026-03-05", "Library gift", "300"), ("2026-03-14", "Supplies", "-120")]


async def _fund(api, name: str, restricted: bool) -> str:
    r = await api.post("/accounting/funds", json={"request_key": uuid.uuid4().hex, "name": name,
                                                  "restricted": restricted, "purpose": "Library" if restricted else None})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _statement(api) -> tuple[dict, dict]:
    """A USD bank opened at 1000 with one imported statement of ROWS: the bank and its
    lines by description."""
    co = await B.co_of(api, {"Authorization": api.headers["Authorization"]})
    bank = await B.bank(api, co["h"], name="Harbor Bank", opening=1000.0, opening_date="2026-02-28")
    _sid, lines = await B.lines_on(api, co, bank["id"], ROWS, opening="1000.00", closing="1180.00",
                                   start="2026-03-01", end="2026-03-31")
    return bank, {line["description"]: line for line in lines}


async def _entries(session, je_id: str) -> list[tuple[str, float, float, str | None]]:
    session.expire_all()
    row = (await session.execute(select(Projection).where(Projection.entity_id == je_id))).scalar_one()
    return sorted((e["account"], e["debit"], e["credit"], e.get("fund_id")) for e in row.state["entries"])


async def _posted(api, bank_id: str, line_id: str, **decision) -> dict:
    """One resolve decision on one line: its result."""
    r = await B.decide_on(api, {}, bank_id, line_id, "post", **decision)
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def _template(page: str, name: str) -> str:
    return page.split(f'id="bank-tpl-{name}"', 1)[1].split("</template>", 1)[0]


async def _je_of(api, bank_id: str, line_id: str) -> str:
    co = await B.co_of(api, {"Authorization": api.headers["Authorization"]})
    line = next(x for x in B.all_lines(await B.groups(api, co, bank_id, view="all")) if x["id"] == line_id)
    assert len(line["matched_je_ids"]) == 1, line
    return line["matched_je_ids"][0]


async def test_with_funds_on_a_statement_line_entry_names_its_fund(owner_ui, session):
    api = owner_ui.api
    assert (await api.put("/accounting/funds/setting", json={"enabled": True})).status_code == 200
    library = await _fund(api, "Library", True)
    reserve = await _fund(api, "Board reserve", False)
    bank, lines = await _statement(api)
    code, page_url = bank["chart_account_code"], f"/accounting/reconcile/bank/{bank['id']}"
    gift, supplies = lines["Library gift"]["id"], lines["Supplies"]["id"]

    page = (await owner_ui.get(page_url)).text
    form, split = _template(page, "answer"), _template(page, "split")
    assert re.findall(r'<input[^>]*name="fund"[^>]*value="([^"]*)"', form) == ["unrestricted"], form
    assert "Library" in form and "Board reserve" in form
    assert re.findall(r'<input[^>]*name="fund"[^>]*value="([^"]*)"', split) == ["unrestricted"] * 3, split
    # Left at Unrestricted, money moved off a balance the funds share is split among them (rule B): both forms say so.
    hint = html.escape(t("recon.fund_hint"), quote=False)
    assert hint in form and hint in split

    r = await owner_ui.post(f"{page_url}/resolve-line", data={
        "line_id": gift, "form": "answer", "answer": "post:4100", "fund": library, "idempotency_key": B.key()})
    assert r.status_code == 200 and "form-error" not in r.text, r.text
    je = await _je_of(api, bank["id"], gift)
    assert await _entries(session, je) == sorted([(code, 300.0, 0.0, library), ("4100", 0.0, 300.0, library)])

    r = await owner_ui.post(f"{page_url}/resolve-line", data={
        "line_id": supplies, "form": "split", "account_code": ["6200", "6200", ""], "amount": ["70", "50", ""],
        "fund": [reserve, "unrestricted", "unrestricted"], "idempotency_key": B.key()})
    assert r.status_code == 200 and "form-error" not in r.text, r.text
    je = await _je_of(api, bank["id"], supplies)
    assert await _entries(session, je) == sorted([
        (code, 0.0, 70.0, reserve), ("6200", 70.0, 0.0, reserve),
        (code, 0.0, 50.0, None), ("6200", 50.0, 0.0, None)])


async def test_without_funds_the_statement_line_forms_have_no_fund_choice(owner_ui):
    api = owner_ui.api
    bank, lines = await _statement(api)
    page = (await owner_ui.get(f"/accounting/reconcile/bank/{bank['id']}")).text
    form, split = _template(page, "answer"), _template(page, "split")
    assert 'name="fund"' not in form and 'name="fund"' not in split
    assert html.escape(t("recon.fund_hint"), quote=False) not in split
    result = await _posted(api, bank["id"], lines["Library gift"]["id"], account_code="4100",
                           fund_id=str(uuid.uuid4()))
    assert not result["ok"] and "fund" in result["reason"].lower(), result


async def _je(api, ts: str, *lines: tuple[str, float, float, str | None]) -> None:
    r = await api.post("/accounting/journal-entries", json={
        "ts": ts, "memo": "Setup", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": a, "debit": d, "credit": c, **({"fund_id": f} if f else {})} for a, d, c, f in lines]})
    assert r.status_code == 200, r.text


async def test_a_statement_line_moving_a_pooled_balance_takes_each_funds_share(owner_ui, session):
    """Rule M/B: an entry line with no fund that moves value off an asset or liability splits pro rata to
    what each fund holds there, the bank side moving with each share; a picked fund replaces the split and
    never takes more than it holds there; an expense line with no fund stays unrestricted."""
    api = owner_ui.api
    assert (await api.put("/accounting/funds/setting", json={"enabled": True})).status_code == 200
    library = await _fund(api, "Library", True)
    # The processor clearing account (1140) holds 60 for Library and 40 unrestricted; accrued
    # expenses (2140) owe 30 for Library.
    await _je(api, "2026-03-01", ("1140", 60, 0, library), ("4100", 0, 60, library),
              ("1140", 40, 0, None), ("4100", 0, 40, None))
    await _je(api, "2026-03-01", ("6200", 30, 0, library), ("2140", 0, 30, library))
    bank, lines = await _statement(api)
    code = bank["chart_account_code"]
    payout, accrual = lines["Library gift"]["id"], lines["Supplies"]["id"]

    result = await _posted(api, bank["id"], payout, account_code="1140", fund_id=library)
    assert not result["ok"] and "Library" in result["reason"], result
    result = await _posted(api, bank["id"], payout, account_code="1140")
    assert result["ok"], result
    # 300 leaves clearing: Library's 60, the unrestricted 40 and the 200 beyond what the funds hold.
    assert await _entries(session, result["je_ids"][0]) == sorted([
        (code, 60.0, 0.0, library), ("1140", 0.0, 60.0, library),
        (code, 240.0, 0.0, None), ("1140", 0.0, 240.0, None)])

    result = await _posted(api, bank["id"], accrual, splits=[
        {"account_code": "2140", "amount": 100}, {"account_code": "6200", "amount": 20}])
    assert result["ok"], result
    assert await _entries(session, result["je_ids"][0]) == sorted([
        (code, 0.0, 30.0, library), ("2140", 30.0, 0.0, library),
        (code, 0.0, 90.0, None), ("2140", 70.0, 0.0, None), ("6200", 20.0, 0.0, None)])
