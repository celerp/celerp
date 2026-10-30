# CLASS money-in-the-wrong-currency: the default bank follows the books' currency.
"""A new install: register creates the company with no currency, so the default bank is
seeded at the fallback base. The setup wizard (or the default location's country) then
sets the books' currency. The default bank follows it while nothing on it was posted in
another currency, so its reconciliation reads every posting and completes."""
from __future__ import annotations

import uuid

import pytest

from test_reconciliation_history import admin, deposit, start

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
    correction, which changes no amount and can be changed back."""
    from test_reconciliation_history import bank, legacy_session
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    paid = await deposit(client, h, b, "2026-08-10", 300.0)
    done = await legacy_session(session, b, "2026-08-31", je_ids=[paid], balance=300.0)
    je = await deposit(client, h, b, "2026-09-10", 200.0)
    sid = (await start(client, h, b["id"], "2026-09-30", 200.0)).json()["id"]
    assert (await client.post(f"/accounting/reconciliation/{sid}/match", json={"je_ids": [je]}, headers=h)).status_code == 200

    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    assert recon["difference"] is None and recon["bank_account"]["relabel_to"] == "USD"
    assert (await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)).status_code == 422
    action = f'action="/accounting/reconcile/relabel/{b["id"]}"'
    async with _ui_as(tok) as ui:
        for page in (f"/accounting/reconcile/{sid}", f"/accounting/reconcile/history/{b['id']}"):
            assert action in (await ui.get(page)).text, page
        r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data={"next": f"/accounting/reconcile/{sid}"})
        assert r.status_code == 303 and r.headers["location"].startswith(f"/accounting/reconcile/{sid}?msg="), r.headers
        assert action not in (await ui.get(r.headers["location"])).text

    assert (await client.get(f"/accounting/bank-accounts/{b['id']}", headers=h)).json()["currency"] == "USD"
    items = {i["id"]: i for i in (await client.get(
        f"/accounting/bank-accounts/{b['id']}/reconciliations", headers=h)).json()["items"]}
    assert items[done]["needs_review"] is False
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text
    # The correction changes no amount, so it can be changed back while that holds.
    r = await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "THB"}, headers=h)
    assert r.status_code == 200 and r.json()["currency"] == "THB", r.text


async def test_relabel_returns_only_to_a_reconciliation_page(client, session):
    from test_reconciliation_history import bank
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    async with _ui_as(tok) as ui:
        for back in ("https://elsewhere.example/", "//elsewhere.example/x", "/settings"):
            r = await ui.post(f"/accounting/reconcile/relabel/{b['id']}", data={"next": back})
            assert r.headers["location"].startswith(f"/accounting/reconcile/history/{b['id']}"), back
