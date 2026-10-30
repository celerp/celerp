# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A completed bank reconciliation keeps what it reconciled. Every journal entry change
passes one check where events are written: an entry a reconciliation counts cannot be
voided, reversed or changed, and a bank posting cannot appear, disappear or change on a
day inside a completed statement's period. Anything else goes through. The refusal names
the account and the statements it would change, and tells the reader what they can do:
someone who manages accounting is told to reopen that reconciliation, anyone else who to
ask."""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from company_backup_support import token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor, _waiting_on_a_lock
from test_helpers import invite_user
from test_payments_stripe import BOOKS, PAID, _company_id, _fee_notices, _payable_invoice, payments_on  # noqa: F401
from test_reconciliation_history import (
    OPENING, admin, bank, complete, completed, deposit, legacy_session, match, post_je, start,
)

pytestmark = pytest.mark.asyncio

ROOT = pathlib.Path(__file__).resolve().parents[1]
REVERSAL = ":reversal"


async def _company(session, b: dict) -> uuid.UUID:
    return (await session.execute(text(
        "SELECT company_id FROM bank_accounts WHERE id = :b"), {"b": b["id"]})).scalar()


async def _status(session, company_id, je_id) -> str | None:
    from celerp.models.projections import Projection
    row = await session.get(Projection, (company_id, je_id), populate_existing=True)
    return (row.state or {}).get("status") if row is not None else None


async def void(client, h, je_id, reversal_date=None):
    body = {"reason": "Correction"}
    if reversal_date:
        body["reversal_date"] = reversal_date
    return await client.post(f"/accounting/journal-entries/{je_id}/void", json=body, headers=h)


async def _created_data(session, company_id, je_id) -> dict:
    from celerp.models.ledger import LedgerEntry
    return dict((await session.execute(select(LedgerEntry.data).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == je_id,
        LedgerEntry.event_type == "acc.journal_entry.created").order_by(LedgerEntry.id))).scalars().first())


async def _emit(session, company_id, je_id, event_type, data, metadata_=None):
    """One journal event written the way every writer writes one, committed."""
    from celerp.events.engine import emit_event
    await emit_event(session, company_id=company_id, entity_id=je_id, entity_type="journal_entry",
                     event_type=event_type, data=data, actor_id=None, location_id=None,
                     source="manual", idempotency_key=uuid.uuid4().hex, metadata_=metadata_ or {})
    await session.commit()


async def _refused(session, company_id, je_id, event_type, data, metadata_=None) -> str:
    with pytest.raises(HTTPException) as refused:
        await _emit(session, company_id, je_id, event_type, data, metadata_)
    await session.rollback()
    assert refused.value.status_code == 409, refused.value.detail
    return str(refused.value.detail)


async def _unvoid(session, company_id, je_id) -> dict:
    """The event that restores a voided entry under its own id, as a document unvoid does."""
    return {**await _created_data(session, company_id, je_id), "status": "posted"}


def _swapped(data: dict, day: str, reverses: str) -> dict:
    return {**data, "ts": day, "reverses": reverses, "memo": "Reversal",
            "entries": [{**line, "debit": line.get("credit", 0), "credit": line.get("debit", 0)}
                        for line in data["entries"]]}


# --- The held rule --------------------------------------------------------------------

async def test_void_of_an_entry_a_completed_reconciliation_holds_is_refused(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    await completed(client, h, b, "2026-09-30", OPENING + 50.0, [je])
    r = await void(client, h, je, "2026-10-15")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Harbor Bank" in detail and "2026-09-30" in detail
    assert await _status(session, await _company(session, b), je) == "posted"


async def test_void_of_an_entry_an_open_reconciliation_holds_is_refused(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    sid = (await start(client, h, b["id"], "2026-09-30", OPENING + 50.0)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await void(client, h, je, "2026-10-15")
    assert r.status_code == 409, r.text
    assert "Harbor Bank" in r.json()["detail"] and "2026-09-30" in r.json()["detail"]
    assert await _status(session, await _company(session, b), je) == "posted"


async def test_reversal_of_a_held_entry_is_refused(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    await completed(client, h, b, "2026-09-30", OPENING + 50.0, [je])
    cid = await _company(session, b)
    reversal = _swapped(await _created_data(session, cid, je), "2026-10-15", je)
    detail = await _refused(session, cid, f"{je}:rev", "acc.journal_entry.created", reversal)
    assert "Harbor Bank" in detail and "2026-09-30" in detail
    assert await _status(session, cid, f"{je}:rev") is None


async def test_unvoid_removing_a_matched_reversal_row_is_refused(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    assert (await void(client, h, je, "2026-09-20")).status_code == 200
    await completed(client, h, b, "2026-09-30", OPENING, [je, je + REVERSAL])
    cid = await _company(session, b)
    detail = await _refused(session, cid, je, "acc.journal_entry.created", await _unvoid(session, cid, je),
                            {"restores": je})
    assert "Harbor Bank" in detail and "2026-09-30" in detail
    assert await _status(session, cid, je) == "void"


async def test_transfer_is_guarded_on_both_accounts(client, session):
    _, h = await admin(client)
    a = await bank(client, h)
    b = await bank(client, h, name="Coast Bank", number="0002")
    r = await client.post("/accounting/transfers", json={
        "from_bank_id": a["id"], "to_bank_id": b["id"], "amount": 100.0, "date": "2026-09-10"}, headers=h)
    assert r.status_code == 200, r.text
    je = r.json().get("je_id") or r.json()["id"]
    await completed(client, h, a, "2026-09-30", OPENING - 100.0, [je])
    await completed(client, h, b, "2026-09-30", OPENING + 100.0, [je])
    cid = await _company(session, b)
    detail = await _refused(session, cid, je, "acc.journal_entry.voided",
                            {"reason": "Correction", "ts": "2026-09-10", "reversed_on": "2026-10-15"})
    assert "Harbor Bank" in detail and "Coast Bank" in detail


# --- The period rule ------------------------------------------------------------------

async def test_backdated_bank_posting_into_a_completed_period_is_refused(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    await completed(client, h, b, "2026-09-30")
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-09-15", "memo": "Late", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": b["chart_account_code"], "debit": 5.0, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": 5.0}]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Harbor Bank" in detail and "2026-09-30" in detail
    assert "Changes dated after 2026-09-30" in detail


async def test_backdated_payment_from_another_module_is_refused(client, session):
    tok, h = await admin(client)
    b = await bank(client, h)
    await completed(client, h, b, "2026-09-30")
    inv = await _invoice(client, h)
    r = await client.post(f"/docs/{inv}/payment", headers=h, json={
        "payment_date": "2026-09-15", "amount": 100.0, "bank_account": b["chart_account_code"]})
    assert r.status_code == 409, r.text
    assert "Harbor Bank" in r.json()["detail"]
    await session.rollback()  # production never commits a refused request; the test client shares the session
    doc = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert not [p for p in doc.get("payments", []) if p.get("status") != "deleted"]


async def test_unvoid_into_a_completed_period_is_refused(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    assert (await void(client, h, je, "2026-09-12")).status_code == 200
    await completed(client, h, b, "2026-09-30")
    cid = await _company(session, b)
    detail = await _refused(session, cid, je, "acc.journal_entry.created", await _unvoid(session, cid, je),
                            {"restores": je})
    assert "2026-09-30" in detail
    assert await _status(session, cid, je) == "void"


async def test_legacy_statement_date_bounds_no_period(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-01-10", 50.0)
    await completed(client, h, b, "2026-09-30")
    await legacy_session(session, b, "31/01/2026", je_ids=[je], balance=OPENING + 50.0)
    # Read as January 31 the legacy row would end the September period there; it bounds nothing.
    r = await deposit_response(client, h, b, "2026-01-15")
    assert r.status_code == 409, r.text
    assert "2026-09-30" in r.json()["detail"] and "31/01/2026" not in r.json()["detail"]
    # Its own match is still protected, by id.
    r = await void(client, h, je, "2026-10-15")
    assert r.status_code == 409, r.text
    assert "31/01/2026" in r.json()["detail"]


async def deposit_response(client, h, b, day, amount=5.0):
    return await client.post("/accounting/journal-entries", headers=h, json={
        "ts": day, "memo": "Deposit", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": b["chart_account_code"], "debit": amount, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": amount}]})


async def test_a_session_started_after_completion_does_not_shrink_protection(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    await completed(client, h, b, "2026-09-30")
    assert (await start(client, h, b["id"], "2026-08-31")).status_code == 200
    r = await deposit_response(client, h, b, "2026-08-15")
    assert r.status_code == 409, r.text
    assert "2026-09-30" in r.json()["detail"]


async def test_reopened_earlier_session_bounds_the_later_period(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    first = await completed(client, h, b, "2026-08-31")
    await completed(client, h, b, "2026-09-30")
    assert (await client.post(f"/accounting/reconciliation/{first}/reopen", headers=h)).status_code == 200
    r = await deposit_response(client, h, b, "2026-08-15")
    assert r.status_code == 200, r.text
    r = await deposit_response(client, h, b, "2026-09-15")
    assert r.status_code == 409, r.text
    assert "2026-09-30" in r.json()["detail"] and "2026-08-31" not in r.json()["detail"]


async def test_correcting_an_earlier_statement_names_only_the_affected_later_periods(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    first_je = await deposit(client, h, b, "2026-08-10", 10.0)
    held = await deposit(client, h, b, "2026-08-20", 20.0)
    first = await completed(client, h, b, "2026-08-31", OPENING + 10.0, [first_je])
    await completed(client, h, b, "2026-09-30", OPENING + 20.0, [held])
    await completed(client, h, b, "2026-10-31")
    assert (await client.post(f"/accounting/reconciliation/{first}/reopen", headers=h)).status_code == 200
    r = await void(client, h, held, "2026-08-25")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "2026-09-30" in detail
    assert "2026-10-31" not in detail and "2026-08-31" not in detail


async def test_correcting_an_earlier_statement_needs_no_unrelated_reopen(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    loose = await deposit(client, h, b, "2026-08-10", 10.0)
    first = await completed(client, h, b, "2026-08-31")
    later = await completed(client, h, b, "2026-09-30")
    assert (await client.post(f"/accounting/reconciliation/{first}/reopen", headers=h)).status_code == 200
    assert (await deposit_response(client, h, b, "2026-08-15")).status_code == 200
    assert (await void(client, h, loose, "2026-08-20")).status_code == 200
    r = await client.get(f"/accounting/reconciliation/{later}", headers=h)
    assert r.json()["status"] == "completed"


# --- What passes ----------------------------------------------------------------------

async def test_journal_without_a_bank_line_passes(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    await completed(client, h, b, "2026-09-30")
    await post_je(client, h, "2026-09-15", [("5100", 5.0, 0.0), ("4100", 0.0, 5.0)])


async def test_posting_after_the_last_completed_statement_passes(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    await completed(client, h, b, "2026-09-30")
    await deposit(client, h, b, "2026-10-05", 5.0)


async def test_void_reversed_after_the_completed_periods_passes(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    await completed(client, h, b, "2026-09-30")
    r = await void(client, h, je, "2026-10-05")
    assert r.status_code == 200, r.text


async def test_repeated_identical_journal_event_passes(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    await completed(client, h, b, "2026-09-30", OPENING + 50.0, [je])
    cid = await _company(session, b)
    await _emit(session, cid, je, "acc.journal_entry.created", await _created_data(session, cid, je))
    await _emit(session, cid, je, "acc.journal_entry.posted", {})
    assert await _status(session, cid, je) == "posted"


async def test_guard_is_skipped_without_accounting(client, session, monkeypatch):
    from celerp.services import journal_accounts
    assert "guard_journal_change" in {f.name for f in dataclasses.fields(journal_accounts.ChartAccess)}
    _, h = await admin(client)
    b = await bank(client, h)
    je = await deposit(client, h, b, "2026-09-10", 50.0)
    await completed(client, h, b, "2026-09-30", OPENING + 50.0, [je])
    cid = await _company(session, b)
    monkeypatch.setattr(journal_accounts, "chart_access", lambda: None)
    await _emit(session, cid, je, "acc.journal_entry.voided", {"reason": "x", "ts": "2026-09-10"})
    assert await _status(session, cid, je) == "void"


# --- Payments and who is told what ----------------------------------------------------

async def _invoice(client, h, total=100.0) -> str:
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "total": total,
        "line_items": [{"name": "X", "quantity": 1, "unit_price": total, "line_total": total}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    assert (await client.post(f"/docs/{inv}/finalize", headers=h)).status_code == 200
    return inv


async def _paid(client, h, b, day="2026-09-10", amount=100.0) -> tuple[str, str]:
    inv = await _invoice(client, h, amount)
    r = await client.post(f"/docs/{inv}/payment", headers=h, json={
        "payment_date": day, "amount": amount, "bank_account": b["chart_account_code"]})
    assert r.status_code == 200, r.text
    return inv, f"je:auto:{inv}:pay:0"


async def _live_payments(client, h, inv) -> list:
    doc = (await client.get(f"/docs/{inv}", headers=h)).json()
    return [p for p in doc.get("payments", []) if p.get("status") != "deleted"]


async def test_delete_payment_refused_while_matched_in_a_reconciliation(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    inv, je = await _paid(client, h, b)
    sid = (await start(client, h, b["id"], "2026-09-30", OPENING + 100.0)).json()["id"]
    assert (await match(client, h, sid, [je])).status_code == 200
    r = await client.request("DELETE", f"/docs/{inv}/payments/0", headers=h, json={})
    assert r.status_code == 409, r.text
    assert "Harbor Bank" in r.json()["detail"] and "2026-09-30" in r.json()["detail"]
    await session.rollback()  # production never commits a refused request; the test client shares the session
    assert len(await _live_payments(client, h, inv)) == 1
    assert await _status(session, await _company(session, b), je) == "posted"


async def test_delete_of_an_unmatched_payment_goes_through(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    await completed(client, h, b, "2026-09-30")
    inv, je = await _paid(client, h, b, "2026-10-05")
    r = await client.request("DELETE", f"/docs/{inv}/payments/0", headers=h, json={})
    assert r.status_code == 200, r.text
    assert await _live_payments(client, h, inv) == []


async def test_refusal_copy_follows_the_callers_role(client, session):
    _, h = await admin(client)
    b = await bank(client, h)
    inv, je = await _paid(client, h, b)
    await completed(client, h, b, "2026-09-30", OPENING + 100.0, [je])
    operator = {"Authorization": f"Bearer {await invite_user(client, session, h, 'op@example.test', 'operator')}"}
    manager = {"Authorization": f"Bearer {await invite_user(client, session, h, 'mgr@example.test', 'manager')}"}

    held_ask = await client.request("DELETE", f"/docs/{inv}/payments/0", headers=operator, json={})
    assert held_ask.status_code == 409, held_ask.text
    assert "Ask someone who manages accounting" in held_ask.json()["detail"]
    assert "Unmatch it there" not in held_ask.json()["detail"]
    await session.rollback()  # production never commits a refused request; the test client shares the session

    held = await client.request("DELETE", f"/docs/{inv}/payments/0", headers=manager, json={})
    assert held.status_code == 409, held.text
    assert "Unmatch it there first" in held.json()["detail"]
    assert "Ask someone" not in held.json()["detail"]
    await session.rollback()  # production never commits a refused request; the test client shares the session

    late = await _invoice(client, h)
    period_ask = await client.post(f"/docs/{late}/payment", headers=operator, json={
        "payment_date": "2026-09-15", "amount": 100.0, "bank_account": b["chart_account_code"]})
    assert period_ask.status_code == 409, period_ask.text
    assert "Changes dated after 2026-09-30" in period_ask.json()["detail"]
    assert "ask someone who manages accounting to reopen" in period_ask.json()["detail"]

    for r in (held_ask, held, period_ask):
        detail = r.json()["detail"].lower()
        assert "harbor bank" in detail and "2026-09-30" in detail
        assert "another date" not in detail and "change the date" not in detail and "redate" not in detail


async def test_online_fee_inside_a_completed_period_is_reported_and_retried(client, session, payments_on):
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_fees, receive_payment
    tok, h = await admin(client)
    b = await bank(client, h, opening=0.0)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    assert await receive_payment({"company_id": cid, "entity_id": eid, "reference": "pi_recon",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": {**BOOKS, "deposit_account": b["chart_account_code"]},
                                  "managed": True})
    sid = await completed(client, h, b, "2026-07-31", 0.0)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_recon",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    event = {"company_id": cid, "entity_id": eid, "reference": "pi_recon",
             "fees": fees, "occurred_at": PAID.isoformat()}

    assert await receive_fees(event) is False
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert [n.i18n["params"]["reason"]["message_key"] for n in failed] == ["notice.online_fee_failed.reconciled"]
    fee_rows = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == uuid.UUID(cid), Projection.entity_id.like(f"je:auto:{eid}:online-fees:%")))).all()
    assert fee_rows == []

    assert (await client.post(f"/accounting/reconciliation/{sid}/reopen", headers=h)).status_code == 200
    assert await receive_fees(event) is True
    fee_rows = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == uuid.UUID(cid), Projection.entity_id.like(f"je:auto:{eid}:online-fees:%")))).all()
    assert len(fee_rows) == 1


async def test_peer_journal_refused_by_reconciliation_reports_the_reason(monkeypatch):
    from celerp.connectors import celerp as peer
    from celerp.connectors.base import ConnectorContext
    from celerp.services import peer_journals
    from peer_marks_support import KEY

    message = "This would change the completed reconciliation of Harbor Bank for the statement of 2026-09-30."

    async def refused(*args, **kwargs):
        raise HTTPException(status_code=409, detail=message)

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {
                "firm": "Ledger & Co", "next": "",
                "items": [{"id": "fj-1", "revision": 1, "memo": "Bank fee", "status": "posted"}]})
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    monkeypatch.setattr(peer_journals, "apply_firm_journal", refused)
    result = await peer.CelerpConnector().sync_journals(ConnectorContext(
        company_id=str(uuid.uuid4()), store_handle="https://firm.example", access_token=KEY))
    assert [a["reason"] for a in result.attention] == [message]


# --- The boundary every journal change passes -----------------------------------------

async def test_every_journal_change_goes_through_emit_event():
    """A journal entry changes only through emit_event, so the check sees every change.
    The one other direct projection apply writes received documents, never journals."""
    sources = [p for d in ("celerp", "default_modules", "ui") for p in (ROOT / d).rglob("*.py")
               if "tests" not in p.relative_to(ROOT).parts]
    constructs = sorted(str(p.relative_to(ROOT)) for p in sources if "LedgerEntry(" in p.read_text(errors="ignore")
                        and "class LedgerEntry(" not in p.read_text(errors="ignore"))
    assert constructs == ["celerp/events/engine.py"]
    applies = sorted(str(p.relative_to(ROOT)) for p in sources
                     if "ProjectionEngine.apply_event(" in p.read_text(errors="ignore"))
    assert applies == ["celerp/events/engine.py", "default_modules/celerp-docs/celerp_docs/received_legacy.py"]
    legacy = (ROOT / "default_modules/celerp-docs/celerp_docs/received_legacy.py").read_text()
    assert "journal_entry" not in legacy


# --- Races (real Postgres) ------------------------------------------------------------

async def _waits(engine, task, what: str) -> None:
    for _ in range(500):
        if await _waiting_on_a_lock(engine):
            return
        if task.done():
            raise AssertionError(f"{what} did not wait")
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} did not wait")


def _hold_completion(monkeypatch):
    """Hold a completion once it has locked its reconciliation, before it commits."""
    from celerp_accounting import routes
    reached, release = asyncio.Event(), asyncio.Event()
    real = routes._recon_bank_and_entries

    async def held(*args, **kw):
        out = await real(*args, **kw)
        if not release.is_set():
            reached.set()
            await release.wait()
        return out
    monkeypatch.setattr(routes, "_recon_bank_and_entries", held)
    return reached, release


async def test_backdated_posting_waiting_on_a_completion_is_refused(monkeypatch, real_engine, real_client):
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    b = await bank(real_client, h)
    sid = (await start(real_client, h, b["id"], "2026-09-30")).json()["id"]
    reached, release = _hold_completion(monkeypatch)
    completing = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{sid}/complete", headers=h))
    await asyncio.wait_for(reached.wait(), 10)
    posting = asyncio.create_task(deposit_response(real_client, h, b, "2026-09-15"))
    try:
        await _waits(real_engine, posting, "the backdated posting")
    finally:
        release.set()
    assert (await completing).status_code == 200
    r = await posting
    assert r.status_code == 409, r.text
    assert "2026-09-30" in r.json()["detail"]


async def test_completion_waits_for_a_backdated_posting(monkeypatch, real_engine, real_client):
    from celerp_accounting import routes
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    b = await bank(real_client, h)
    sid = (await start(real_client, h, b["id"], "2026-09-30")).json()["id"]
    reached, release = asyncio.Event(), asyncio.Event()
    real = routes.emit_event

    async def held(*args, **kw):
        out = await real(*args, **kw)
        if kw.get("event_type") == "acc.journal_entry.created" and not release.is_set():
            reached.set()
            await release.wait()
        return out
    monkeypatch.setattr(routes, "emit_event", held)
    posting = asyncio.create_task(deposit_response(real_client, h, b, "2026-09-15"))
    await asyncio.wait_for(reached.wait(), 10)
    completing = asyncio.create_task(real_client.post(f"/accounting/reconciliation/{sid}/complete", headers=h))
    try:
        await _waits(real_engine, completing, "the completion")
    finally:
        release.set()
    assert (await posting).status_code == 200
    r = await completing
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    # Completed after the posting committed, the period now holds that day.
    assert (await deposit_response(real_client, h, b, "2026-09-16")).status_code == 409


async def test_guard_judges_the_entry_as_committed_under_the_lock(real_engine, real_client):
    from celerp.services.company_lock import lock_company
    from celerp_accounting.models import ReconciliationSession
    boss, a, _ = await _harbor(real_engine)
    h = auth(await token(real_engine, boss, a))
    b = await bank(real_client, h)
    await completed(real_client, h, b, "2026-09-30")
    je = await deposit(real_client, h, b, "2026-10-05", 50.0)
    sid = (await start(real_client, h, b["id"], "2026-10-31", OPENING + 50.0)).json()["id"]
    assert (await match(real_client, h, sid, [je])).status_code == 200
    async with maker(real_engine)() as s:
        await lock_company(s, a)
        recon = await s.get(ReconciliationSession, uuid.UUID(sid))
        recon.reconciled_je_ids = []
        await s.flush()
        voiding = asyncio.create_task(void(real_client, h, je, "2026-11-05"))
        try:
            await _waits(real_engine, voiding, "the void")
        finally:
            await s.commit()
    r = await voiding
    assert r.status_code == 200, r.text
