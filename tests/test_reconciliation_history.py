# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each bank account lists its earlier reconciliations, newest statement first, and a
completed one opens read-only. One reconciliation per bank account and statement date:
starting a statement again resumes it or opens the completed one. A statement date is
a real date, a balance is compared at the account currency's own precision, and the
currency of an account that already has history cannot change. History that no longer
holds is flagged for review, never corrected."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from celerp.main import app
from company_backup_support import token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor, _waiting_on_a_lock
from test_helpers import invite_user, register_admin
from test_reconciliation_reopen import _ui_as

pytestmark = pytest.mark.asyncio

OPENING = 1000.0


async def admin(client) -> tuple[str, dict]:
    tok = await register_admin(client)
    return tok, {"Authorization": f"Bearer {tok}"}


async def bank(client, h, *, currency="USD", opening=OPENING, name="Harbor Bank", number="0001", rate=None) -> dict:
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": name, "account_number": number, "bank_type": "checking",
        "currency": currency, "opening_balance": opening, "rate": rate}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def start(client, h, bank_id, day, balance=OPENING):
    return await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank_id, "statement_date": day, "statement_balance": balance}, headers=h)


async def post_je(client, h, day, lines: list[tuple[str, float, float]], memo="Entry") -> str:
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": day, "memo": memo, "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": a, "debit": d, "credit": c} for a, d, c in lines]})
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("je_id") or body["id"]


async def deposit(client, h, b: dict, day, amount, memo="Deposit") -> str:
    """A bank posting: money in from revenue."""
    return await post_je(client, h, day, [(b["chart_account_code"], amount, 0.0), ("4100", 0.0, amount)], memo)


async def legacy_deposit(client, h, session, b: dict, day, amount) -> str:
    """A books-currency-only posting on a foreign-currency account, as written before
    bank lines carried the account's own amount (the manual journal now refuses it)."""
    async def label(currency):
        await session.execute(text("UPDATE bank_accounts SET currency = :c WHERE id = :b"),
                              {"c": currency, "b": b["id"]})
        await session.commit()
    await label("USD")
    try:
        return await deposit(client, h, b, day, amount)
    finally:
        await label(b["currency"])


async def match(client, h, sid, je_ids):
    return await client.post(f"/accounting/reconciliation/{sid}/match", json={"je_ids": je_ids}, headers=h)


async def complete(client, h, sid):
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    return r.json()


async def completed(client, h, b: dict, day, balance=OPENING, je_ids=()) -> str:
    r = await start(client, h, b["id"], day, balance)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    if je_ids:
        assert (await match(client, h, sid, list(je_ids))).status_code == 200
    await complete(client, h, sid)
    return sid


async def history(client, h, bank_id):
    return await client.get(f"/accounting/bank-accounts/{bank_id}/reconciliations", headers=h)


async def _session_count(session, bank_id) -> int:
    return (await session.execute(text(
        "SELECT count(*) FROM reconciliation_sessions WHERE bank_account_id = :b"), {"b": bank_id})).scalar()


async def legacy_session(session, b: dict, day: str, *, status="completed", je_ids=(), balance=OPENING) -> str:
    """A reconciliation row as one written before statement dates were checked or
    statements kept to one session per date."""
    from celerp_accounting.models import ReconciliationSession
    company_id = (await session.execute(text(
        "SELECT company_id FROM bank_accounts WHERE id = :b"), {"b": b["id"]})).scalar()
    sid = uuid.uuid4()
    session.add(ReconciliationSession(
        id=sid, company_id=company_id, bank_account_id=uuid.UUID(b["id"]), statement_date=day,
        statement_balance=balance, status=status, reconciled_je_ids=list(je_ids),
        completed_at=datetime.now(timezone.utc) if status == "completed" else None))
    await session.commit()
    return str(sid)


# --- History -------------------------------------------------------------------------

async def test_history_lists_every_session_of_the_account_newest_first(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    other = await bank(client, h, name="Other Bank", number="0002")
    first = await completed(client, h, b, "2026-07-31")
    second = await completed(client, h, b, "2026-08-31")
    third = (await start(client, h, b["id"], "2026-09-30")).json()["id"]
    await completed(client, h, other, "2026-08-31")
    r = await history(client, h, b["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["id"] for i in body["items"]] == [third, second, first]
    assert body["total"] == 3
    top = body["items"][0]
    assert top["statement_date"] == "2026-09-30" and top["status"] == "open" and top["completed_at"] is None
    assert body["items"][1]["status"] == "completed" and body["items"][1]["completed_at"]
    assert all(i["needs_review"] is False for i in body["items"])
    assert all(i["bank_account_id"] == b["id"] for i in body["items"])


async def test_history_of_another_companys_account_is_not_found(real_engine, real_client):
    boss, a, other_co = await _harbor(real_engine)
    ha, hb = auth(await token(real_engine, boss, a)), auth(await token(real_engine, boss, other_co))
    b = await bank(real_client, ha)
    await completed(real_client, ha, b, "2026-08-31")
    r = await history(real_client, hb, b["id"])
    assert r.status_code == 404, r.text
    assert r.json()["detail"]
    r = await history(real_client, ha, "not-a-uuid")
    assert r.status_code == 422, r.text


async def test_history_needs_manage_accounting(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    viewer = await invite_user(client, session, h, "viewer@example.test", "viewer")
    r = await history(client, {"Authorization": f"Bearer {viewer}"}, b["id"])
    assert r.status_code == 403, r.text
    async with _ui_as(viewer) as ui:
        r = await ui.get(f"/accounting/reconcile/history/{b['id']}")
    assert r.status_code in (302, 303), r.text
    assert "/settings/accounting" in r.headers["location"] and "msg=" in r.headers["location"]


async def test_history_of_an_account_with_no_reconciliations_says_so(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    r = await history(client, h, b["id"])
    assert r.status_code == 200 and r.json() == {"items": [], "total": 0}, r.text
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/history/{b['id']}")).text
    assert "This account has no reconciliations yet. Start one with Reconcile." in page
    assert f"/accounting/reconcile/start?bank_id={b['id']}" in page


async def test_history_status_filter_reads_the_url(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    done = await completed(client, h, b, "2026-08-31")
    open_ = (await start(client, h, b["id"], "2026-09-30")).json()["id"]
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/history/{b['id']}")).text
        assert "Reconciliation history" in page and "Harbor Bank" in page
        assert f"/accounting/reconcile/{done}" in page and f"/accounting/reconcile/{open_}" in page
        assert "In progress" in page and "Completed" in page
        page = (await ui.get(f"/accounting/reconcile/history/{b['id']}?status=completed")).text
        assert f"/accounting/reconcile/{done}" in page and f"/accounting/reconcile/{open_}" not in page
        page = (await ui.get(f"/accounting/reconcile/history/{b['id']}?status=open")).text
        assert f"/accounting/reconcile/{open_}" in page and f"/accounting/reconcile/{done}" not in page
        page = (await ui.get(f"/accounting/reconcile/history/{b['id']}?status=bogus")).text
        assert f"/accounting/reconcile/{open_}" in page and f"/accounting/reconcile/{done}" in page


async def test_history_lists_an_archived_account(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = await completed(client, h, b, "2026-08-31")
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"is_active": False}, headers=h)
    assert r.status_code == 200, r.text
    r = await history(client, h, b["id"])
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [sid], r.text


async def test_history_page_unknown_account_returns_to_bank_accounts_with_message(client, session):
    tok, _ = await admin(client)
    async with _ui_as(tok) as ui:
        r = await ui.get(f"/accounting/reconcile/history/{uuid.uuid4()}")
    assert r.status_code in (302, 303), r.text
    loc = r.headers["location"]
    assert loc.startswith("/settings/accounting") and "tab=bank-accounts" in loc and "msg=" in loc


async def test_bank_account_card_links_its_history(client, session):
    tok, h = await admin(client)
    active = await bank(client, h)
    archived = await bank(client, h, name="Old Bank", number="0009")
    await client.patch(f"/accounting/bank-accounts/{archived['id']}", json={"is_active": False}, headers=h)
    async with _ui_as(tok) as ui:
        page = (await ui.get("/settings/accounting?tab=bank-accounts")).text
    assert f'href="/accounting/reconcile/history/{active["id"]}"' in page
    assert f'href="/accounting/reconcile/history/{archived["id"]}"' in page


async def test_workspace_links_its_history(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    sid = (await start(client, h, b["id"], "2026-09-30")).json()["id"]
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
    assert f'href="/accounting/reconcile/history/{b["id"]}"' in page


async def test_completed_workspace_is_read_only(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    sid = await completed(client, h, b, "2026-09-30", OPENING + 50.0, [je])
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
    assert f'hx-post="/accounting/reconcile/{sid}/reopen"' in page
    for control in ("/auto-match", "/bulk-confirm", "/import", "/write-off", "/unmatch", "/match-confirm",
                    "/split-form", "/create-form", "/skip"):
        assert f"/accounting/reconcile/{sid}{control}" not in page and control + '"' not in page, control


async def test_reopen_asks_for_confirmation(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    sid = await completed(client, h, b, "2026-09-30")
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
    assert "hx-confirm=\"Reopen this reconciliation? Its matches stay as they are and your books don&#x27;t change." \
        in page or "hx-confirm=\"Reopen this reconciliation? Its matches stay as they are and your books don't change." \
        in page


async def test_reopen_needs_manage_accounting(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = await completed(client, h, b, "2026-09-30")
    for role in ("viewer", "operator"):
        tok = await invite_user(client, session, h, f"{role}@example.test", role)
        r = await client.post(f"/accounting/reconciliation/{sid}/reopen", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, (role, r.text)
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["status"] == "completed"


# Every reconciliation endpoint and its permission, as the running route table holds them.
RECONCILIATION_ROUTES = {
    ("GET", "/accounting/bank-accounts"): "manage_accounting",
    ("POST", "/accounting/bank-accounts"): "manage_accounting",
    ("GET", "/accounting/bank-accounts/{bank_id}"): "manage_accounting",
    ("PATCH", "/accounting/bank-accounts/{bank_id}"): "manage_accounting",
    ("GET", "/accounting/bank-accounts/{bank_id}/reconciliations"): "manage_accounting",
    ("POST", "/accounting/reconciliation/start"): "manage_accounting",
    ("GET", "/accounting/reconciliation/{session_id}"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/auto-match"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/bulk-confirm"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/complete"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/import-csv"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/import-file"): "manage_accounting",
    ("PATCH", "/accounting/reconciliation/{session_id}/lines/{line_id}"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/attach"): "manage_accounting",
    ("DELETE", "/accounting/reconciliation/{session_id}/lines/{line_id}/attach/{att_id}"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/create"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/match"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/split"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/lines/{line_id}/unmatch"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/match"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/reopen"): "manage_accounting",
    ("GET", "/accounting/reconciliation/{session_id}/statement-lines"): "manage_accounting",
    ("GET", "/accounting/reconciliation/{session_id}/workbench"): "manage_accounting",
    ("POST", "/accounting/reconciliation/{session_id}/write-off"): "manage_accounting",
    ("DELETE", "/docs/{entity_id}/payments/{payment_index}"): "record_payments",
}


def _route_table() -> dict[tuple[str, str], tuple[str, ...]]:
    from celerp.ai.tools import _api_routes, _route_permissions
    return {(m, path): _route_permissions(route)
            for path, methods, route in _api_routes(app) for m in methods}


async def test_route_table_contracts_hold():
    table = _route_table()
    for (method, path), perm in RECONCILIATION_ROUTES.items():
        assert (method, path) in table, (method, path)
        assert table[(method, path)] == (perm,), (method, path, table[(method, path)])


async def test_reconciliation_writers_need_manage_accounting(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = (await start(client, h, b["id"], "2026-09-30")).json()["id"]
    line = str(uuid.uuid4())
    op = {"Authorization": f"Bearer {await invite_user(client, session, h, 'operator@example.test', 'operator')}"}
    for (method, path), perm in RECONCILIATION_ROUTES.items():
        if perm != "manage_accounting":
            continue
        url = path.format(bank_id=b["id"], session_id=sid, line_id=line, att_id="x", entity_id="x")
        r = await client.request(method, url, headers=op, json={})
        assert r.status_code == 403, (method, path, r.status_code, r.text)


async def test_session_of_another_company_is_not_found(real_engine, real_client):
    boss, a, other_co = await _harbor(real_engine)
    ha, hb = auth(await token(real_engine, boss, a)), auth(await token(real_engine, boss, other_co))
    b = await bank(real_client, ha)
    sid = (await start(real_client, ha, b["id"], "2026-09-30")).json()["id"]
    assert (await real_client.get(f"/accounting/reconciliation/{sid}", headers=hb)).status_code == 404
    assert (await match(real_client, hb, sid, [])).status_code == 404
    assert (await real_client.post(f"/accounting/reconciliation/{sid}/complete", headers=hb)).status_code == 404
    await complete(real_client, ha, sid)
    assert (await real_client.post(f"/accounting/reconciliation/{sid}/reopen", headers=hb)).status_code == 404
    assert (await real_client.get(f"/accounting/reconciliation/{sid}", headers=ha)).json()["status"] == "completed"


async def test_line_of_another_session_is_not_found(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    s1 = (await start(client, h, b["id"], "2026-08-31")).json()["id"]
    s2 = (await start(client, h, b["id"], "2026-09-30")).json()["id"]
    r = await client.post(f"/accounting/reconciliation/{s1}/import-csv", headers=h, files={
        "file": ("s.csv", b"Date,Description,Amount\n2026-08-05,Coffee,-4.50\n", "text/csv")})
    assert r.status_code == 200, r.text
    line = (await client.get(f"/accounting/reconciliation/{s1}/statement-lines", headers=h)).json()
    line_id = (line.get("items") if isinstance(line, dict) else line)[0]["id"]
    for action in ("match", "unmatch", "split", "create"):
        r = await client.post(f"/accounting/reconciliation/{s2}/lines/{line_id}/{action}", headers=h, json={})
        assert r.status_code in (404, 422), (action, r.text)
    r = await client.patch(f"/accounting/reconciliation/{s2}/lines/{line_id}", headers=h, json={"status": "skipped"})
    assert r.status_code == 404, r.text


# --- Flagging for review ------------------------------------------------------------

async def test_history_flags_a_reference_outside_the_account_for_review(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    other = await bank(client, h, name="Other Bank", number="0002")
    foreign = await deposit(client, h, other, "2026-09-05", 25.0)
    sid = await legacy_session(session, b, "2026-09-30", je_ids=[foreign])
    item = (await history(client, h, b["id"])).json()["items"][0]
    assert item["id"] == sid and item["needs_review"] is True
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["needs_review"] is True
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
        assert "Needs review" in page
        page = (await ui.get(f"/accounting/reconcile/history/{b['id']}")).text
        assert "Needs review" in page
    # The note has a full-width row of its own; inside the date cell (no wrapping in
    # data tables) it would stretch the table past the page edge.
    note = re.search(r'<td[^>]*colspan="(\d+)"[^>]*>\s*<p[^>]*>Needs review', page)
    assert note and int(note.group(1)) == page.split("<thead>")[1].split("</thead>")[0].count("<th")


async def test_history_flags_an_unbalanced_completed_session_for_review(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = await legacy_session(session, b, "2026-09-30", balance=OPENING + 7.0)
    fine = await completed(client, h, b, "2026-10-31")
    items = {i["id"]: i for i in (await history(client, h, b["id"])).json()["items"]}
    assert items[sid]["needs_review"] is True and items[fine]["needs_review"] is False


async def test_history_flagging_changes_nothing(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    other = await bank(client, h, name="Other Bank", number="0002")
    foreign = await deposit(client, h, other, "2026-09-05", 25.0)
    sid = await legacy_session(session, b, "2026-09-30", je_ids=[foreign], balance=OPENING + 7.0)
    snapshot = "SELECT status, completed_at, statement_balance, reconciled_je_ids FROM reconciliation_sessions"
    before = (await session.execute(text(snapshot))).all()
    events = (await session.execute(text("SELECT count(*) FROM ledger"))).scalar()
    assert (await history(client, h, b["id"])).json()["items"][0]["needs_review"] is True
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["needs_review"] is True
    session.expire_all()
    assert (await session.execute(text(snapshot))).all() == before
    assert (await session.execute(text("SELECT count(*) FROM ledger"))).scalar() == events


# --- One session per statement (A) and real dates (C) ---------------------------------

async def test_start_of_a_completed_statement_opens_it_instead_of_duplicating(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = await completed(client, h, b, "2026-09-30")
    r = await start(client, h, b["id"], "2026-09-30")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == sid and r.json()["status"] == "completed"
    assert await _session_count(session, b["id"]) == 1


async def test_repeated_start_resumes_the_same_session(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    first = await start(client, h, b["id"], "2026-09-30")
    again = await start(client, h, b["id"], "2026-09-30")
    assert first.json()["id"] == again.json()["id"] and again.json()["status"] == "open"
    assert again.json()["statement_date"] == "2026-09-30"
    assert await _session_count(session, b["id"]) == 1


async def test_start_refusal_is_shown_on_the_start_page(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    await start(client, h, b["id"], "2026-09-30")
    async with _ui_as(tok) as ui:
        r = await ui.post("/accounting/reconcile/start", data={
            "bank_account_id": b["id"], "statement_date": "2026-09-30", "statement_balance": "1234.00"})
        assert r.status_code in (302, 303), r.text
        loc = r.headers["location"]
        assert loc.startswith("/accounting/reconcile/start") and "msg=" in loc
        page = (await ui.get(loc)).text
    assert "already" in page.lower()


async def test_start_refusal_keeps_what_the_user_entered(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    await bank(client, h, name="Second Bank", number="0003")
    await start(client, h, b["id"], "2026-09-30")
    async with _ui_as(tok) as ui:
        r = await ui.post("/accounting/reconcile/start", data={
            "bank_account_id": b["id"], "statement_date": "2026-09-30", "statement_balance": "1234.00"})
        page = (await ui.get(r.headers["location"])).text
    assert re.search(rf'<option[^>]*value="{b["id"]}"[^>]*selected|<option[^>]*selected[^>]*value="{b["id"]}"', page)
    assert 'value="2026-09-30"' in page and 'value="1234.00"' in page


async def test_statement_date_must_be_a_real_date(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    for bad in ("2026-02-30", "30/01/2026", "", "soon"):
        r = await start(client, h, b["id"], bad)
        assert r.status_code == 422, (bad, r.text)
    assert await _session_count(session, b["id"]) == 0


async def test_start_prefers_the_open_session_of_a_duplicated_date(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    await legacy_session(session, b, "2026-09-30")
    open_ = await legacy_session(session, b, "2026-09-30", status="open")
    r = await start(client, h, b["id"], "2026-09-30")
    assert r.status_code == 200 and r.json()["id"] == open_, r.text
    c2 = await bank(client, h, name="Second Bank", number="0003")
    await legacy_session(session, c2, "2026-09-30")
    latest = await legacy_session(session, c2, "2026-09-30")
    r = await start(client, h, c2["id"], "2026-09-30")
    assert r.status_code == 200 and r.json()["id"] == latest, r.text
    assert await _session_count(session, c2["id"]) == 2


# --- Currency (B) and precision (C) ---------------------------------------------------

async def test_currency_change_refused_once_a_posting_carries_a_foreign_amount(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=0.0)
    await fx_deposit(client, h, b, 100, 1.08, 108)
    for target in ("USD", "GBP"):
        r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": target}, headers=h)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "EUR" in detail and "new bank account" in detail and "transfer" in detail
        assert (await client.get(f"/accounting/bank-accounts/{b['id']}", headers=h)).json()["currency"] == "EUR"


async def test_currency_change_refused_on_a_ledger_account_another_bank_has_used(client, session):
    from test_recon_guard import second_bank_on
    _, h = await admin(client)
    a = await bank(client, h, currency="EUR", opening=0.0)
    await fx_deposit(client, h, a, 100, 1.08, 108)
    b = await second_bank_on(client, h, a)
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "EUR"}, headers=h)
    assert r.status_code == 409, r.text
    assert (await client.get(f"/accounting/bank-accounts/{b['id']}", headers=h)).json()["currency"] == "USD"


async def test_base_only_account_relabels_to_the_books_currency_and_back(client, session):
    """Postings carrying books-currency amounts only read the same whatever the account is
    labelled: relabelling such an account changes no amount, so it is allowed and undoable."""
    _, h = await admin(client)
    b = await bank(client, h, opening=0.0)
    je = await deposit(client, h, b, "2026-09-05", 10.0)
    await completed(client, h, b, "2026-09-30", 10.0, [je])
    for target in ("THB", "USD", "THB", "USD"):
        r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": target}, headers=h)
        assert r.status_code == 200 and r.json()["currency"] == target, r.text
    assert (await history(client, h, b["id"])).json()["items"][0]["needs_review"] is False


async def test_currency_change_allowed_before_use(client, session):
    _, h = await admin(client)
    b = await bank(client, h, opening=0.0)
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "EUR"}, headers=h)
    assert r.status_code == 200 and r.json()["currency"] == "EUR", r.text
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"bank_name": "Renamed"}, headers=h)
    assert r.status_code == 200, r.text


async def test_currency_change_waits_for_a_first_foreign_posting(monkeypatch, real_engine, real_client):
    from celerp_accounting import routes
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    b = await bank(real_client, h, currency="EUR", opening=0.0)
    reached, release = asyncio.Event(), asyncio.Event()
    real = routes.emit_event

    async def held(*args, **kw):
        out = await real(*args, **kw)
        if kw.get("event_type") == "acc.journal_entry.created" and not release.is_set():
            reached.set()
            await release.wait()
        return out
    monkeypatch.setattr(routes, "emit_event", held)
    posting = asyncio.create_task(fx_deposit(real_client, h, b, 100, 1.08, 108))
    await asyncio.wait_for(reached.wait(), 10)
    patching = asyncio.create_task(real_client.patch(
        f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"}, headers=h))
    try:
        for _ in range(500):
            if await _waiting_on_a_lock(real_engine):
                break
            if patching.done():
                raise AssertionError("the currency change did not wait for the first posting")
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the currency change did not wait for the first posting")
    finally:
        release.set()
    await posting
    r = await patching
    assert r.status_code == 409, r.text
    async with maker(real_engine)() as s:
        assert (await s.execute(text("SELECT currency FROM bank_accounts WHERE id = :b"),
                                {"b": b["id"]})).scalar() == "EUR"


async def test_three_decimal_currency_completes_at_its_own_precision(client, session):
    _, h = await admin(client)
    for statement, refused in ((1000.001, True), (1000.0, False)):
        b = await bank(client, h, currency="KWD", opening=0, number=str(statement))
        je = await fx_deposit(client, h, b, 1000, 3.25, 3250)
        sid = (await start(client, h, b["id"], "2026-09-30", statement)).json()["id"]
        assert (await match(client, h, sid, [je])).status_code == 200
        if refused:
            r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
            assert r.status_code == 422, r.text
            assert "0.001" in r.json()["detail"]
        else:
            await complete(client, h, sid)


async def fx_deposit(client, h, b: dict, amount, rate, base_amount, day="2026-09-10") -> str:
    """A bank posting written in the bank account's own currency, converted to base."""
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": day, "memo": "Foreign deposit", "idempotency_token": uuid.uuid4().hex,
        "entries": [
            {"account": b["chart_account_code"], "debit": amount, "credit": 0, "currency": b["currency"], "rate": rate},
            {"account": "4100", "debit": 0, "credit": base_amount}]})
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("je_id") or body["id"]


async def test_foreign_currency_account_reconciles_in_its_own_currency(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    je = await fx_deposit(client, h, b, 1850, 0.0067, 12.40)
    sid = (await start(client, h, b["id"], "2026-09-30", 1850)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    got = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert got["reconciled_entries"][0]["amount"] == 1850
    assert got["book_balance"] == 1850 and got["difference"] == 0 and got["balanced"] is True
    await complete(client, h, sid)
    items = (await history(client, h, b["id"])).json()["items"]
    assert items[0]["needs_review"] is False


async def test_foreign_currency_account_refuses_the_base_figure(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    je = await fx_deposit(client, h, b, 1850, 0.0067, 12.40)
    sid = (await start(client, h, b["id"], "2026-09-30", 12)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 422 and "1838" in r.json()["detail"], r.text


async def test_entry_with_no_amount_in_the_account_currency_is_flagged_not_converted(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="JPY", opening=0)
    je = await legacy_deposit(client, h, session, b, "2026-09-10", 12.40)
    sid = await legacy_session(session, b, "2026-09-30", status="open", je_ids=[je], balance=12)
    got = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert got["reconciled_entries"][0]["amount"] is None
    assert got["difference"] is None and got["balanced"] is False and got["book_balance"] is None
    for action in ("complete", "write-off"):
        r = await client.post(f"/accounting/reconciliation/{sid}/{action}", headers=h, json={})
        assert r.status_code == 422 and "JPY" in r.json()["detail"], r.text


async def test_default_bank_account_is_kept_in_the_base_currency(client, session):
    from celerp_accounting.routes import _DEFAULT_BANK_CODE, _base_currency, _seed_default_bank_account
    from celerp_accounting.models import BankAccount
    _, h = await admin(client)
    company_id = (await session.execute(text("SELECT id FROM companies LIMIT 1"))).scalar()
    await session.execute(text("UPDATE companies SET settings = settings::jsonb - 'currency' WHERE id = :c"), {"c": company_id})
    await session.execute(text("DELETE FROM bank_accounts WHERE company_id = :c"), {"c": company_id})
    await session.execute(text("DELETE FROM accounts WHERE company_id = :c AND code = :code"),
                          {"c": company_id, "code": _DEFAULT_BANK_CODE})
    await session.commit()
    await _seed_default_bank_account(session, company_id)
    await session.commit()
    seeded = (await session.execute(select(BankAccount).where(BankAccount.company_id == company_id))).scalar_one()
    assert seeded.currency == await _base_currency(session, company_id)


async def test_completed_balances_and_events_are_never_rewritten(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    sid = await completed(client, h, b, "2026-09-30", OPENING + 50.0, [je])
    snapshot = "SELECT status, completed_at, statement_balance, reconciled_je_ids FROM reconciliation_sessions"
    before = (await session.execute(text(snapshot))).all()
    events = (await session.execute(text("SELECT count(*) FROM ledger"))).scalar()
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-09-15", "memo": "Late", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": b["chart_account_code"], "debit": 5.0, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": 5.0}]})
    assert r.status_code == 409, r.text
    r = await client.post(f"/accounting/journal-entries/{je}/void", headers=h, json={})
    assert r.status_code == 409, r.text
    session.expire_all()
    assert (await session.execute(text(snapshot))).all() == before
    assert (await session.execute(text("SELECT count(*) FROM ledger"))).scalar() == events
    got = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert got["status"] == "completed" and got["difference"] == 0 and got["needs_review"] is False


async def test_a_difference_of_one_minor_unit_offers_the_write_off_not_complete(client, session):
    tok, h = await admin(client)
    b = await bank(client, h, currency="KWD", opening=0)
    je = await fx_deposit(client, h, b, 1000, 3.25, 3250)
    sid = (await start(client, h, b["id"], "2026-09-30", 1000.001)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    got = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert got["difference"] == 0.001 and got["balanced"] is False
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
    assert f'hx-post="/accounting/reconcile/{sid}/write-off"' in page
    assert f'hx-post="/accounting/reconcile/{sid}/complete"' not in page
    assert "recon-diff--warn" in page


async def test_a_three_decimal_statement_balance_can_be_entered(client, session):
    tok, h = await admin(client)
    await bank(client, h, currency="KWD", opening=0)
    async with _ui_as(tok) as ui:
        page = (await ui.get("/accounting/reconcile/start")).text
    assert re.search(r'name="statement_balance"[^>]*step="0\.001"', page), page


async def test_a_zero_decimal_currency_steps_in_whole_units():
    from fasthtml.common import to_xml
    from ui.routes.reconciliation import _create_form, _split_form
    line = {"id": "l1", "amount": -1850, "description": "Fee", "date": "2026-09-10"}
    for form in (_create_form, _split_form):
        html = to_xml(form("s1", "l1", line, [], {"currency": "JPY", "books_currency": "JPY"}))
        assert 'step="1"' in html and 'step="0.01"' not in html, form.__name__
