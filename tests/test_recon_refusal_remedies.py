# CLASS money-in-the-wrong-currency (refusal side): every refusal for an entry with no
# amount in the bank account's currency names a way out the user can actually take.
"""An entry posted without an amount in its bank account's currency cannot be matched;
one a reconciliation already counts is let go of with Undo, wherever it was matched;
and an opening balance posted before it carried its own amount still counts."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from test_reconciliation_history import legacy_deposit, admin, bank, deposit, fx_deposit, legacy_session, match, start
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def test_an_entry_with_no_amount_in_the_account_currency_is_refused_naming_the_correction(client, session):
    _, h = await admin(client)
    b = await bank(client, h, currency="THB", opening=0.0)
    je = await legacy_deposit(client, h, session, b, "2026-09-10", 200.0)
    sid = (await start(client, h, b["id"], "2026-09-30", 200.0)).json()["id"]
    r = await match(client, h, sid, [je])
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_recon_book_entry_no_amount"), r.text
    # The correction it names: the account offers to be set to the books' currency.
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["bank_account"]["relabel_to"] == "USD"
    assert (await client.patch(f"/accounting/bank-accounts/{b['id']}", json={"currency": "USD"}, headers=h)).status_code == 200
    assert (await match(client, h, sid, [je])).status_code == 200
    assert (await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)).status_code == 200


async def test_a_counted_entry_with_no_amount_is_let_go_of_with_undo_then_the_session_completes(client, session):
    """A session from before entries were checked counts a books-currency-only entry on a
    EUR account that also holds EUR amounts, so the account cannot be relabelled. The
    refusal says to press Undo; the workspace offers it for an entry no line holds."""
    from test_reconciliation_reopen import _ui_as
    tok, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=0.0)
    eur = await fx_deposit(client, h, b, 100, 1.08, 108)
    base_only = await legacy_deposit(client, h, session, b, "2026-09-12", 50.0)
    sid = await legacy_session(session, b, "2026-09-30", status="open", je_ids=[eur, base_only], balance=100.0)

    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 422 and r.json()["detail"] == t("acct.err_recon_amount_unknown", currency="EUR"), r.text
    async with _ui_as(tok) as ui:
        page = (await ui.get(f"/accounting/reconcile/{sid}")).text
        undo = f"/accounting/reconcile/{sid}/entries/{base_only}/unmatch"
        assert undo in page
        assert (await ui.post(undo)).status_code == 200
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["reconciled_je_ids"] == [eur]
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text


async def test_unmatching_an_entry_a_statement_line_holds_points_to_that_line(client, session):
    _, h = await admin(client)
    b = await bank(client, h, opening=0.0)
    je = await deposit(client, h, b, "2026-09-10", 200.0)
    sid = (await start(client, h, b["id"], "2026-09-30", 200.0)).json()["id"]
    from celerp_accounting.models import BankStatementLine
    company_id = (await session.execute(text("SELECT company_id FROM bank_accounts WHERE id = :b"), {"b": b["id"]})).scalar()
    session.add(BankStatementLine(id=uuid.uuid4(), reconciliation_id=uuid.UUID(sid), company_id=company_id,
                                  line_date="2026-09-10", description="Deposit", amount=200.0,
                                  status="matched", matched_je_id=je, raw_csv_row={}))
    await session.commit()
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/unmatch", json={"je_ids": [je]}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == t("acct.err_recon_book_entry_matched"), r.text


async def test_an_opening_balance_posted_without_its_own_amount_still_counts_on_a_foreign_account(client, session):
    """The opening entry is never offered for matching, so it can never be let go of: an
    opening posted before it carried its own amount reads the figure typed for it."""
    from celerp.models.projections import Projection
    from celerp_accounting.routes import _opening_je_id
    _, h = await admin(client)
    b = await bank(client, h, currency="EUR", opening=1000, rate=1.08)
    row = (await session.execute(select(Projection).where(Projection.entity_id == _opening_je_id(uuid.UUID(b["id"]))))).scalar_one()
    state = dict(row.state)
    state["entries"] = [{k: v for k, v in e.items() if not k.startswith("fx_")} for e in state["entries"]]
    row.state = state
    await session.commit()
    eur = await fx_deposit(client, h, b, 100, 1.08, 108)
    sid = (await start(client, h, b["id"], "2026-09-30", 1100)).json()["id"]
    assert (await match(client, h, sid, [eur])).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text
