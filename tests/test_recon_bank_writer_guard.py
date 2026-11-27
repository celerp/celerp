# CLASS money-in-the-wrong-currency (guard over every bank writer).
"""Every entry that moves money through a bank account kept in another currency than the
books' carries its amount in that currency, so the account's reconciliation can read it.

Two halves. The first walks every writer on a EUR and a JPY bank in USD books, checks
each bank line it posted, and completes a reconciliation over all of them. The second
reads the source for every function that posts a journal entry touching a bank account
and fails on one this file has not reviewed, so a new writer is checked before it ships.
"""
from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from sqlalchemy import select

import bank_statement_helpers as B
from test_reconciliation_history import admin, bank, match
from ui.i18n import refusal_text, t
from shipped_sources import SHIPPED_ROOTS

ROOT = pathlib.Path(__file__).resolve().parents[1]
RATES = {"EUR": 1.08, "JPY": 0.0067}
DAY = "2026-09-10"


def _claims(tok: str) -> dict:
    from celerp.services.auth import get_token_claims
    return get_token_claims(tok)


async def _bank_lines(session, company_id, code: str) -> list[tuple[str, dict]]:
    from celerp.models.projections import Projection
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).scalars().all()
    return [(row.entity_id, line) for row in rows for line in (row.state or {}).get("entries") or []
            if line.get("account") == code]


async def _invoice(client, h, currency: str, total: float) -> str:
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "currency": currency, "conversion_rate": RATES[currency], "total": total,
        "line_items": [{"name": "X", "quantity": 1, "unit_price": total, "line_total": total}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200
    return doc_id


async def _pay(client, h, doc_id: str, code: str, amount: float, currency: str) -> None:
    r = await client.post(f"/docs/{doc_id}/payment", headers=h, json={
        "amount": amount, "payment_date": DAY, "bank_account": code, "conversion_rate": RATES[currency]})
    assert r.status_code == 200, r.text


async def _online_fees(session, tok: str, doc_id: str, currency: str) -> None:
    """Provider fees settled in the payment's currency against its deposit account."""
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp.services import auto_je
    claims = _claims(tok)
    cid = uuid.UUID(claims["company_id"])
    doc = await session.get(Projection, {"company_id": cid, "entity_id": doc_id})
    payment = doc.state["payments"][0]
    minor = 250 if currency == "JPY" else 270
    await auto_je.create_for_online_fees(
        session, company_id=cid, user_id=uuid.UUID(claims["sub"]), doc_id=doc_id, reference="pi_guard",
        payment=payment, fees={"version": 1, "currency": currency.lower(), "transaction_id": f"txn_{doc_id}",
                               "total_minor": minor, "processor_minor": minor, "platform_minor": 0,
                               "other_minor": 0},
        occurred_at=datetime(2026, 9, 10, 12, tzinfo=timezone.utc))
    await session.commit()


async def _dispute(session, tok: str, doc_id: str, currency: str) -> None:
    """Part of the payment taken back by a card dispute, from its deposit account."""
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp.services import auto_je
    claims = _claims(tok)
    cid = uuid.UUID(claims["company_id"])
    session.expire_all()
    doc = await session.get(Projection, {"company_id": cid, "entity_id": doc_id})
    assert await auto_je.post_dispute_adjustment(
        session, company_id=cid, user_id=uuid.UUID(claims["sub"]), doc_id=doc_id, payment=doc.state["payments"][0],
        dispute_id="dp_guard", step="funds_withdrawn", amount_minor=500,
        currency=currency.lower(), occurred_at=datetime(2026, 9, 10, 12, tzinfo=timezone.utc))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["EUR", "JPY"])
async def test_every_bank_writer_posts_in_the_bank_currency_and_the_account_reconciles(client, session, currency):
    tok, h = await admin(client)
    rate = RATES[currency]
    b = await bank(client, h, currency=currency, opening=1000, rate=rate)
    usd = await bank(client, h, currency="USD", opening=5000, name="Home Bank", number="0002")
    code = b["chart_account_code"]

    # Transfers in and out.
    r = await client.post("/accounting/transfers", headers=h, json={
        "idempotency_key": uuid.uuid4().hex, "from_bank_id": usd["id"], "to_bank_id": b["id"], "amount": 54.0, "date": DAY, "to_amount": 50})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/transfers", headers=h, json={
        "idempotency_key": uuid.uuid4().hex, "from_bank_id": b["id"], "to_bank_id": usd["id"], "amount": 20, "date": DAY, "rate": rate})
    assert r.status_code == 200, r.text

    # A payment, a refund of part of it, and a second payment voided.
    inv = await _invoice(client, h, currency, 300.0)
    await _pay(client, h, inv, code, 100.0, currency)
    r = await client.post(f"/docs/{inv}/refund", headers=h, json={
        "payment_index": 0, "amount": 40.0, "payment_date": DAY})
    assert r.status_code == 200, r.text
    await _pay(client, h, inv, code, 30.0, currency)
    r = await client.post(f"/docs/{inv}/void-payment", headers=h, json={"payment_index": 1, "refund_date": DAY})
    assert r.status_code == 200, r.text
    await _online_fees(session, tok, inv, currency)
    await _dispute(session, tok, inv, currency)

    # A manual entry on the bank is refused without its amount in the bank's currency.
    lines = [{"account": code, "debit": 15, "credit": 0},
             {"account": "4100", "debit": 0, "credit": 15}]
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Cash sale", "idempotency_token": uuid.uuid4().hex, "entries": lines})
    assert r.status_code == 422 and refusal_text(r.json()["detail"]) == t(
        "acct.err_je_bank_currency", account=code, currency=currency), r.text
    lines[0] |= {"currency": currency, "rate": rate}
    lines[1]["credit"] = float(round(15 * rate, 2))
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Cash sale", "idempotency_token": uuid.uuid4().hex, "entries": lines})
    assert r.status_code == 200, r.text

    # Statement lines posted from the bank page, and a write-off of what is left.
    cid = uuid.UUID(_claims(tok)["company_id"])
    booked = sum((line.get("fx_debit") or 0) - (line.get("fx_credit") or 0)
                 for _, line in await _bank_lines(session, cid, code))
    places = 0 if currency == "JPY" else 2
    money = lambda v: f"{v:.{places}f}"  # noqa: E731
    co = {"h": h, "company_id": str(cid), "user_id": str(_claims(tok)["sub"])}
    opening = round(booked + 1, places)
    await B.statement(client, co, b["id"], [("2026-09-20", "Interest", money(25)), ("2026-09-21", "Mixed", money(30))],
                      opening=money(opening), closing=money(opening + 55), start="2026-09-01", end="2026-09-30")
    table = await B.groups(client, co, b["id"])
    interest, mixed = B.line_by_text(table, "Interest"), B.line_by_text(table, "Mixed")
    r = await B.resolve(client, co, b["id"], decisions=[
        {"line_ids": [interest["id"]], "kind": "post", "account_code": "4100", "rate": rate},
        {"line_ids": [mixed["id"]], "kind": "post", "rate": rate, "splits": [
            {"account_code": "4100", "amount": money(20)}, {"account_code": "4100", "amount": money(10)}]}])
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.text
    sid = interest["statement_id"]
    recon = (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()
    open_ids = [e["je_id"] for e in recon["unreconciled_entries"]]
    assert all(e["amount"] is not None for e in recon["unreconciled_entries"]), recon["unreconciled_entries"]
    assert (await match(client, h, sid, open_ids)).status_code == 200
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", headers=h, json={"rate": rate})
    assert r.status_code == 200, r.text

    posted = await _bank_lines(session, cid, code)
    assert len({je for je, _ in posted}) >= 12, posted
    for je_id, line in posted:
        assert line.get("fx_currency") == currency, (je_id, line)
        assert (line.get("fx_debit") or line.get("fx_credit")), (je_id, line)
    r = await client.post(f"{B.API}/bank/{b['id']}/complete", headers=h, json={"idempotency_key": B.key()})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()["status"] == "completed"


# Every function that posts a journal entry touching a bank account, with how its bank
# line carries the amount in the account's own currency.
_BANK_WRITERS: dict[tuple[str, str], str] = {
    ("default_modules/celerp-accounting/celerp_accounting/routes.py", "post_opening"): "_bank_entries",
    ("default_modules/celerp-accounting/celerp_accounting/routes.py", "create_transfer"): "_bank_entries",
    ("default_modules/celerp-accounting/celerp_accounting/projections.py", "apply_accounting_event"):
        "applies stored events to projections; posts nothing",
    ("default_modules/celerp-accounting/celerp_accounting/routes.py", "write_off_difference"): "_bank_entries",
    ("celerp/services/auto_je.py", "create_for_doc_payment"): "payment_bank_fx",
    ("celerp/services/auto_je.py", "void_for_doc_payment"): "payment_return_entries -> payment_bank_fx",
    ("default_modules/celerp-docs/celerp_docs/routes.py", "reverse_payment_refund"):
        "payment_return_entries -> payment_bank_fx",
    ("celerp/services/auto_je.py", "create_for_online_fees"): "bank_fx_fields",
    ("celerp/services/auto_je.py", "post_dispute_adjustment"): "bank_fx_fields",
    ("default_modules/celerp-accounting/celerp_accounting/migration_sink.py", "_journal_record"):
        "imported lines keep their fx fields; transfers carry the account's currency",
    ("celerp/services/lot_origin.py", "evidence_entries"): "reads entries; posts nothing to a bank",
    ("celerp/events/engine.py", "emit_event"): "the event store itself",
}
_POSTS = ("acc.journal_entry.created", "_emit_auto_posted_je", "JOURNAL_CREATED")
_BANKISH = ("bank", "chart_account_code", "deposit_account")


def _writers_touching_banks() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for root in SHIPPED_ROOTS:
        for path in (ROOT / root).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel:
                continue
            src = path.read_text(encoding="utf-8")
            if not any(p in src for p in _POSTS):
                continue
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "_emit_auto_posted_je":
                    body = ast.get_source_segment(src, node) or ""
                    if any(p in body for p in _POSTS) and any(b in body for b in _BANKISH):
                        found.add((rel, node.name))
    return found


def test_every_function_posting_to_a_bank_account_is_reviewed_here():
    found = _writers_touching_banks()
    assert found - set(_BANK_WRITERS) == set(), (
        "A new function posts a journal entry touching a bank account. Make its bank line carry "
        "the amount in the account's currency (accounting _bank_entries, auto_je.payment_bank_fx "
        "or bank_fx_fields), cover it in the walk above, then list it in _BANK_WRITERS.")
    assert set(_BANK_WRITERS) - found == set(), "a reviewed writer is gone; drop it from _BANK_WRITERS"


# The rule above lives at the journal boundary every writer passes (the accounting module's
# guard_journal_change, run by emit_event), so a line in books currency only on a EUR bank is
# refused with the manual entry's message whoever writes it: emitted directly, imported, or
# brought in by a migration.
def _books_only_lines(code: str) -> list[dict]:
    return [{"account": code, "debit": 15.0, "credit": 0.0}, {"account": "4100", "debit": 0.0, "credit": 15.0}]


async def _eur_bank(client) -> tuple[dict, str, str]:
    co = await B.company(client)
    eur = await B.bank(client, co["h"], currency="EUR", name="Euro Bank")
    return co, eur["chart_account_code"], t("acct.err_je_bank_currency", account=eur["chart_account_code"],
                                            currency="EUR")


async def test_the_journal_boundary_refuses_a_foreign_bank_line_without_its_amount(client, session):
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    co, code, want = await _eur_bank(client)
    with pytest.raises(HTTPException) as e:
        await emit_event(session, company_id=uuid.UUID(co["company_id"]), entity_id=f"je:{uuid.uuid4()}",
                         entity_type="journal_entry", event_type="acc.journal_entry.created",
                         data={"ts": DAY, "memo": "any writer", "status": "posted", "entries": _books_only_lines(code)},
                         actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex,
                         metadata_={})
    await session.rollback()
    assert e.value.status_code == 422 and refusal_text(e.value.detail) == want


async def test_an_imported_foreign_bank_line_without_its_amount_is_refused(client, session):
    co, code, want = await _eur_bank(client)
    r = await client.post("/accounting/import/batch", headers=co["h"], json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex,
        "data": {"status": "posted", "ts": DAY, "memo": "import", "entries": _books_only_lines(code)}}]})
    assert r.status_code == 200 and r.json()["created"] == 0 and want in str(r.json()["errors"]), r.text


async def test_a_migrated_foreign_bank_line_without_its_amount_is_refused(client, session):
    from celerp_accounting.import_service import AccImportRecord, import_journal_records

    co, code, want = await _eur_bank(client)
    kept = dict(_books_only_lines(code)[0], fx_debit=13.89, fx_currency="EUR", fx_rate=1.08)
    bare, carried = (AccImportRecord(entity_id=f"je:{uuid.uuid4()}", event_type="acc.journal_entry.created",
                                     source="migration", idempotency_key=uuid.uuid4().hex,
                                     data={"status": "posted", "ts": DAY, "memo": "Migrated", "entries": lines})
                     for lines in (_books_only_lines(code), [kept, _books_only_lines(code)[1]]))
    outcome = await import_journal_records(session, co["company_id"], co["user_id"], [bare, carried], origin="migration")
    await session.commit()
    status = {rec.entity_id: (rec.status, str(rec.message)) for rec in outcome.records}
    assert status[bare.entity_id][0] == "failed" and want in status[bare.entity_id][1], status
    assert status[carried.entity_id][0] == "created", status


async def test_a_line_in_one_of_two_foreign_banks_sharing_an_account_is_stored(client, session):
    """Two banks in different foreign currencies on one ledger account: a line with its amount in
    either one's currency is read by that bank's reconciliation, so it is not refused."""
    from celerp.events.engine import emit_event
    from celerp_accounting.models import BankAccount

    co, code, _ = await _eur_bank(client)
    session.add(BankAccount(company_id=uuid.UUID(co["company_id"]), chart_account_code=code, bank_name="Pound Bank",
                            account_number="****0002", bank_type="checking", currency="GBP"))
    await session.commit()
    lines = [dict(_books_only_lines(code)[0], fx_debit=13.89, fx_currency="EUR", fx_rate=1.08),
             _books_only_lines(code)[1]]
    await emit_event(session, company_id=uuid.UUID(co["company_id"]), entity_id=f"je:{uuid.uuid4()}",
                     entity_type="journal_entry", event_type="acc.journal_entry.created",
                     data={"ts": DAY, "memo": "shared account", "status": "posted", "entries": lines},
                     actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex, metadata_={})
    await session.commit()
