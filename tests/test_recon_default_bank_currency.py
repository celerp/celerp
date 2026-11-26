# CLASS money-in-the-wrong-currency: the default bank follows the books' currency.
"""A new install: register creates the company with no currency, so the default bank is
seeded at the fallback base. The setup wizard (or the default location's country) then
sets the books' currency. The default bank follows it while nothing on it was posted in
another currency, so its reconciliation reads every posting and completes."""
from __future__ import annotations

import uuid

import pytest

from ui.i18n import refusal_text, t
from test_reconciliation_history import (admin, bank_complete, complete, deposit, legacy_deposit, start,
                                        stopped_reason)

pytestmark = pytest.mark.asyncio


async def _default_bank(client, h) -> dict:
    banks = (await client.get("/accounting/bank-accounts", headers=h)).json()
    banks = banks.get("items", banks) if isinstance(banks, dict) else banks
    return next(b for b in banks if b["chart_account_code"] == "1111")


async def _wizard_new_install(client, currency="THB"):
    _, h = await admin(client)
    r = await client.patch("/companies/me/books", json={"currency": currency}, headers=h)
    assert r.status_code == 200, r.text
    return h, await _default_bank(client, h)


@pytest.mark.parametrize("currency", ["THB", "JPY"])
async def test_default_bank_follows_the_currency_the_wizard_sets(client, session, currency):
    h, default = await _wizard_new_install(client, currency)
    assert default["currency"] == currency, default


async def test_default_bank_follows_the_currency_of_the_default_locations_country(client, session):
    from celerp.tax_regimes import get_regime
    currency = get_regime("AU")["currency"]
    _, h = await admin(client)
    r = await client.post("/companies/me/locations", headers=h, json={
        "name": "Sydney", "type": "warehouse", "is_default": True, "address": {"country": "AU"}})
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me", headers=h)).json()["settings"]["currency"] == currency
    assert (await _default_bank(client, h))["currency"] == currency


@pytest.mark.parametrize("currency", ["THB", "JPY"])
async def test_default_bank_reconciles_after_the_wizard_sets_the_currency(client, session, currency):
    h, default = await _wizard_new_install(client, currency)
    je = await deposit(client, h, default, "2026-09-10", 500.0)
    r = await start(client, h, default["id"], "2026-09-30", 500.0)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert (await client.post(f"/accounting/reconciliation/{sid}/match", json={"je_ids": [je]}, headers=h)).status_code == 200
    await complete(client, h, sid)


async def test_default_bank_with_a_foreign_amount_keeps_its_currency(client, session):
    """A posting carrying an amount in the bank's own currency fixes it: moving the bank
    with the books would make that amount unreadable."""
    from celerp_accounting.routes import default_bank_follows_books_hook
    from test_reconciliation_history import fx_deposit
    _, h = await admin(client)
    default = await _default_bank(client, h)
    r = await client.patch(f"/accounting/bank-accounts/{default['id']}", json={"currency": "EUR"}, headers=h)
    assert r.status_code == 200, r.text
    await fx_deposit(client, h, r.json(), 100, 1.08, 108)
    me = (await client.get("/companies/me", headers=h)).json()
    await default_bank_follows_books_hook(session=session, company_id=uuid.UUID(me["id"]))
    assert (await _default_bank(client, h))["currency"] == "EUR"


async def test_mislabelled_bank_is_corrected_from_the_notice_and_its_reconciliations_read_again(client, session):
    """An existing install whose default bank was seeded in another currency than the books:
    every posting on it carries a books-currency amount only. Nothing changes it on its own;
    the bank page and the history page say why its figures read "--" and offer the one
    correction. A completed reconciliation of the account would be read again by it, so the
    correction asks for that reconciliation to be reopened first; then it goes through,
    changes no amount, and can be changed back until a reconciliation is completed again."""
    from test_reconciliation_history import bank, legacy_session
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    paid = await legacy_deposit(client, h, session, b, "2026-08-10", 300.0)
    done = await legacy_session(session, b, "2026-08-31", je_ids=[paid], balance=300.0)
    je = await legacy_deposit(client, h, session, b, "2026-09-10", 200.0)
    # The September statement closes on both deposits: the bank's complete opens it on August's close.
    sid = await legacy_session(session, b, "2026-09-30", status="open", je_ids=[je], balance=500.0)

    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert recon["difference"] is None and recon["bank_account"]["relabel_to"] == "USD"
    assert stopped_reason(await bank_complete(client, h, sid))
    action = f'action="/accounting/reconcile/relabel/{b["id"]}"'
    bank_page = f"/accounting/reconcile/bank/{b['id']}"
    back = {"next": bank_page}
    async with _ui_as(tok) as ui:
        for page in (bank_page, f"/accounting/reconcile/history/{b['id']}"):
            assert action in (await ui.get(page)).text, page
        r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data=back)
        assert r.status_code == 303 and "?msg=" in r.headers["location"], r.headers
        assert t("acct.err_bank_currency_reconciled", currency="THB") in (await ui.get(r.headers["location"])).text
        assert (await client.post(f"/accounting/reconciliation/{done}/reopen", headers=h)).status_code == 200
        r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data=back)
        assert r.status_code == 303 and r.headers["location"].startswith(f"{bank_page}?done="), r.headers
        page = (await ui.get(r.headers["location"])).text
        assert action not in page
        # The correction went through, so it reads as done, not as a refusal.
        assert "flash flash--success" in page and t("recon.relabel_done", currency="THB", base="USD") in page

    assert (await client.get(f"/accounting/bank-accounts/{b['id']}", headers=h)).json()["currency"] == "USD"
    # The correction changes no amount, so it can be changed back while no reconciliation is completed.
    for target in ("THB", "USD"):
        r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": target}, headers=h)
        assert r.status_code == 200 and r.json()["currency"] == target, r.text
    await complete(client, h, sid)
    assert (await client.get(f"/accounting/reconciliation/{done}", headers=h)).json()["status"] == "completed"
    items = (await client.get(f"/accounting/bank-accounts/{b['id']}/reconciliations", headers=h)).json()["items"]
    assert not any(i["needs_review"] for i in items), items
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "THB"}, headers=h)
    assert r.status_code == 409 and refusal_text(r.json()["detail"]) == t("acct.err_bank_currency_reconciled", currency="USD"), r.text


async def test_relabel_returns_only_to_a_reconciliation_page(client, session):
    from test_reconciliation_history import bank
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    async with _ui_as(tok) as ui:
        for back in ("https://elsewhere.example/", "//elsewhere.example/x", "/settings"):
            r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data={"next": back})
            assert r.headers["location"].startswith(f"/accounting/reconcile/history/{b['id']}"), back


async def _mislabelled_bank_with_lines(client, session):
    """A bank whose postings and statement lines carry books-currency amounts only, then
    labelled in another currency, as an install that seeded its default bank before the
    books' currency was set left it: its amounts would be read in the wrong currency."""
    from sqlalchemy import text
    import bank_statement_helpers as B
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    rows = [("2026-03-05", "ACME SUPPLIES", "-120.00"), ("2026-03-12", "SHOP SALE", "300.00")]
    _sid, lines = await B.lines_on(client, co, bk["id"], rows, opening="0.00", closing="180.00",
                                   start="2026-03-01", end="2026-03-31")
    await deposit(client, co["h"], bk, "2026-03-12", 300.0)
    await session.execute(text("UPDATE bank_accounts SET currency = 'THB' WHERE id = :b"), {"b": bk["id"]})
    await session.commit()
    return B, co, bk, lines


async def test_import_and_posting_on_a_mislabelled_bank_are_refused_naming_the_correction(client, session):
    """Nothing is imported onto or posted from an account whose label and entries disagree:
    the refusal names both currencies and the correction, and nothing is written."""
    B, co, bk, lines = await _mislabelled_bank_with_lines(client, session)
    want = t("acct.err_bank_mislabelled", currency="THB", base="USD")
    preview = await B.read(client, co, [(B.csv_bytes([("2026-04-02", "RENT", "-50.00")]), "apr.csv", "text/csv")],
                           bank_id=bk["id"])
    r = await B.import_statement(client, co, preview, bank_id=bk["id"], expect=422, header={
        "opening_balance": "180.00", "closing_balance": "130.00", "period_start": "2026-04-01", "period_end": "2026-04-30"})
    assert refusal_text(r["detail"]) == want, r
    acme = next(line["id"] for line in lines if line["description"] == "ACME SUPPLIES")
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [acme], "kind": "post", "account_code": "5100"}])
    assert r.status_code == 422 and refusal_text(r.json()["detail"]) == want, r.text
    statuses = {line["status"] for line in B.all_lines(await B.groups(client, co, bk["id"], view="all"))}
    assert statuses == {"unmatched"}, statuses


async def test_the_mislabelled_check_reads_only_the_banks_own_account(client, session):
    """The refusal check runs on every import and posting, so it never reads the whole
    journal: each read it makes is narrowed to the bank's ledger account, and an account in
    the books' currency is not read at all."""
    from unittest.mock import patch
    import celerp_accounting.routes as R
    B, co, bk, lines = await _mislabelled_bank_with_lines(client, session)
    real, reads = R.journal_rows, []

    async def counted(*a, **k):
        reads.append(k.get("account"))
        return await real(*a, **k)
    acme = next(line["id"] for line in lines if line["description"] == "ACME SUPPLIES")
    with patch.object(R, "journal_rows", counted):
        r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [acme], "kind": "post", "account_code": "5100"}])
    assert r.status_code == 422, r.text
    assert reads and set(reads) == {bk["chart_account_code"]}, reads


async def test_a_corrected_bank_imports_and_posts_again(client, session):
    """The neighbouring invariant: once the account is set to the books' currency (the
    correction the refusal names), the same import and posting go through."""
    B, co, bk, lines = await _mislabelled_bank_with_lines(client, session)
    r = await client.patch(f"/accounting/bank-accounts/{bk['id']}", json={"currency": "USD"}, headers=co["h"])
    assert r.status_code == 200, r.text
    acme = next(line["id"] for line in lines if line["description"] == "ACME SUPPLIES")
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [acme], "kind": "post", "account_code": "5100"}])
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.text
    await B.statement(client, co, bk["id"], [("2026-04-02", "RENT", "-50.00")], opening="180.00", closing="130.00",
                      start="2026-04-01", end="2026-04-30")


async def test_a_new_foreign_bank_with_no_entries_is_not_called_mislabelled(client, session):
    """An account with no entries has nothing that disagrees with its label: a new account in
    another currency than the books' gets no correction offered, on its own record or in the
    account list, and it keeps its currency."""
    h, _ = await _wizard_new_install(client, "USD")
    r = await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Euro Bank", "account_number": "EU-1", "currency": "EUR", "bank_type": "checking", "opening_balance": 0})
    assert r.status_code in (200, 201), r.text
    assert r.json()["currency"] == "EUR" and r.json()["relabel_to"] is None, r.json()
    banks = (await client.get("/accounting/bank-accounts", headers=h)).json()
    banks = banks.get("items", banks) if isinstance(banks, dict) else banks
    assert next(b for b in banks if b["id"] == r.json()["id"])["relabel_to"] is None
