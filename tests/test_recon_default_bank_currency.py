# CLASS money-in-the-wrong-currency: the default bank follows the books' currency.
"""A new install: register creates the company with no currency, so the default bank is
seeded at the fallback base. The setup wizard (or the default location's country) then
sets the books' currency. The default bank follows it while nothing on it was posted in
another currency, so its reconciliation reads every posting and completes."""
from __future__ import annotations

import uuid

import pytest

from ui.i18n import t
from test_reconciliation_history import legacy_deposit, admin, deposit, start

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
    _, h = await admin(client)
    r = await client.post("/companies/me/locations", headers=h, json={
        "name": "Tokyo", "type": "warehouse", "is_default": True, "address": {"country": "JP"}})
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me", headers=h)).json()["settings"]["currency"] == "JPY"
    assert (await _default_bank(client, h))["currency"] == "JPY"


@pytest.mark.parametrize("currency", ["THB", "JPY"])
async def test_default_bank_reconciles_after_the_wizard_sets_the_currency(client, session, currency):
    h, default = await _wizard_new_install(client, currency)
    je = await deposit(client, h, default, "2026-09-10", 500.0)
    r = await start(client, h, default["id"], "2026-09-30", 500.0)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert (await client.post(f"/accounting/reconciliation/{sid}/match", json={"je_ids": [je]}, headers=h)).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text


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
    the workspace and the history page say why its figures read "--" and offer the one
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
    sid = await legacy_session(session, b, "2026-09-30", status="open", je_ids=[je], balance=200.0)

    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert recon["difference"] is None and recon["bank_account"]["relabel_to"] == "USD"
    assert (await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)).status_code == 422
    action = f'action="/accounting/reconcile/relabel/{b["id"]}"'
    back = {"next": f"/accounting/reconcile/{sid}"}
    async with _ui_as(tok) as ui:
        for page in (f"/accounting/reconcile/{sid}", f"/accounting/reconcile/history/{b['id']}"):
            assert action in (await ui.get(page)).text, page
        r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data=back)
        assert r.status_code == 303 and "?msg=" in r.headers["location"], r.headers
        assert t("acct.err_bank_currency_reconciled", currency="THB") in (await ui.get(r.headers["location"])).text
        assert (await client.post(f"/accounting/reconciliation/{done}/reopen", headers=h)).status_code == 200
        r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data=back)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/accounting/reconcile/{sid}?done="), r.headers
        page = (await ui.get(r.headers["location"])).text
        assert action not in page
        # The correction went through, so it reads as done, not as a refusal.
        assert "flash flash--success" in page and t("recon.relabel_done", currency="THB", base="USD") in page

    assert (await client.get(f"/accounting/bank-accounts/{b['id']}", headers=h)).json()["currency"] == "USD"
    # The correction changes no amount, so it can be changed back while no reconciliation is completed.
    for target in ("THB", "USD"):
        r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": target}, headers=h)
        assert r.status_code == 200 and r.json()["currency"] == target, r.text
    for s_id in (done, sid):
        r = await client.post(f"/accounting/reconciliation/{s_id}/complete", headers=h)
        assert r.status_code == 200, r.text
    items = (await client.get(f"/accounting/bank-accounts/{b['id']}/reconciliations", headers=h)).json()["items"]
    assert not any(i["needs_review"] for i in items), items
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "THB"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_bank_currency_reconciled", currency="USD"), r.text


async def test_relabel_returns_only_to_a_reconciliation_page(client, session):
    from test_reconciliation_history import bank
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    async with _ui_as(tok) as ui:
        for back in ("https://elsewhere.example/", "//elsewhere.example/x", "/settings"):
            r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data={"next": back})
            assert r.headers["location"].startswith(f"/accounting/reconcile/history/{b['id']}"), back
