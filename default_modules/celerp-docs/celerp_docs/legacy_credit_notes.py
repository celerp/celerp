# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time settlement of credit notes issued by an earlier release.

An earlier release took a credit note's amount off its invoice's balance when the credit
note was made, recorded nothing on the credit note itself and posted no entry for it.
Every later payment on the invoice recomputed its balance as total less paid, wiping the
reduction, and the credit note kept its whole balance, so the same credit could be paid
around, applied or refunded a second time.

Each invoice's balance is put back to total less paid less what issued credit notes
settled, and each earlier credit note is then settled as one issued now would be:

- An issued credit note settles, up to what it still has open, what its invoice still
  owes of the reduction it made: that amount is ``credited`` on both and owed by
  neither. What the invoice no longer owes (it was paid in full since) stays open on
  the credit note as the customer's credit, to refund or apply. What the credit note
  already paid out by refund or application elsewhere was a second use of the same
  credit, so the invoice owes it again. Its entry (Dr revenue and output tax, Cr
  receivable) posts at its full amount on the day it was issued, unless an entry for it
  already exists; a day inside a locked period posts on the business date today instead,
  with a memo naming the credit note. A credit note at another rate than its invoice
  takes the invoice's rate, and an entry already posted at its own rate is brought to
  the invoice's against exchange gain or loss.
- A credit note in draft, void or deleted gives its invoice the reduction back.
- An issued credit note whose invoice is void is voided with it, posting nothing. One
  that already paid out is posted and keeps only what it paid out; what it still had
  open is released and the credit note is closed (close_credit_on_void_invoice).
- An issued credit note whose invoice is in draft is posted and settled when the invoice
  is issued again (the invoice's finalize runs this settlement for it).

Document amounts and lines never change. The owner is told each outcome by credit note
number, and each document touched is logged.

Runs in the lifespan, gated by a marker so it runs once per database; a company staged for
a migration, a credit note that could not be settled, or one still waiting for its invoice
to be issued again leaves the marker unset for the next start. Settling one twice changes nothing: the settlement it writes on the invoice is
what marks the pair settled.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

MARKER_KEY = "legacy_credit_notes_settled"
SOURCE = "legacy_credit_note"
_NOTICE = "notice.legacy_credit_notes"
_ISSUED = ("sent", "final", "partial", "paid", "awaiting_payment")


def _number(state: dict, fallback: str) -> str:
    return str(state.get("doc_number") or state.get("ref_id") or fallback.removeprefix("doc:"))


def _outcome(key: str, **params) -> dict:
    """One line of the owner's notice: English text and the key it is shown from."""
    from celerp.accounting_roles import refusal
    from ui.i18n import t

    return refusal(f"{_NOTICE}.{key}", t(f"{_NOTICE}.{key}", "en", **params), **params)


async def _legacy_effects(session: AsyncSession, company_id=None, invoice_id: str | None = None) -> dict:
    """Per (company, invoice, credit note), the last effect a credit note had on its
    invoice, for the pairs whose last effect is an earlier release's reduction: it says it
    reduced the balance and does not record by how much."""
    from celerp.models.ledger import LedgerEntry

    q = select(LedgerEntry).where(LedgerEntry.event_type == "doc.updated",
                                  LedgerEntry.metadata_["source_credit_note"].as_string().isnot(None))
    if company_id is not None:
        q = q.where(LedgerEntry.company_id == company_id)
    if invoice_id is not None:
        q = q.where(LedgerEntry.entity_id == invoice_id)
    last: dict = {}
    for e in (await session.execute(q.order_by(LedgerEntry.id))).scalars():
        last[(e.company_id, e.entity_id, str(e.metadata_["source_credit_note"]))] = e
    return {k: e for k, e in last.items()
            if (e.metadata_ or {}).get("credit_note_effect", "reduced") == "reduced"
            and "credit_amount" not in (e.metadata_ or {})}


async def _posted_entry(session: AsyncSession, company_id, cn_id: str) -> bool:
    """Whether an issue entry for the credit note is already in the books (one posted by
    hand or by a later release before this settlement ran)."""
    from celerp.models.projections import Projection

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"je:auto:{cn_id}:fin%")))).scalars().all()
    return any((r.state or {}).get("status") == "posted" for r in rows)


async def _open_day(session: AsyncSession, company_id, day: str) -> str | None:
    """None when ``day`` is open, else the open date the shared rule gives: the company's
    business date today, never the day after the lock (posting_dates.correction_day)."""
    from celerp.services.posting_dates import correction_day

    return await correction_day(session, company_id, day)


def _spent_elsewhere(note: dict, invoice_id: str) -> Decimal:
    """What a credit note paid out other than to its own invoice: cash refunds and
    applications to other invoices."""
    from celerp.services.money import to_decimal

    return sum((to_decimal(p.get("amount") or 0) for p in note.get("payments") or []
                if p.get("status", "active") == "active"
                and (p.get("method") == "refund"
                     or (p.get("method") == "applied" and p.get("target_doc_id") != invoice_id))),
               Decimal(0))


async def _mark_invoice(session, company_id, invoice_id: str, inv: dict, cn_id: str, *, effect: str,
                        settled: Decimal, credit_amount: Decimal, currency: str) -> None:
    """The invoice's balance as total less paid less what credit notes settled, and the
    effect that marks this pair done."""
    from celerp.services.money import round_money, to_decimal, to_stored_float
    from celerp_docs.doc_projections import _payment_balances
    from celerp_docs.routes import emit_credit_settlement

    credited = round_money(to_decimal(inv.get("credited") or 0), currency) + settled
    _paid, outstanding = _payment_balances({**inv, "credited": to_stored_float(credited)},
                                           to_decimal(inv.get("amount_paid") or 0))
    await emit_credit_settlement(
        session, company_id, None, invoice_id, inv, outstanding=outstanding, credited=credited,
        idempotency_key=f"credit-note-backfill:{cn_id}:invoice",
        metadata={"source_credit_note": cn_id, "credit_note_effect": effect,
                  "credit_amount": to_stored_float(credit_amount), SOURCE: True})


async def _void_with_invoice(session, company_id, cn_id: str, note: dict) -> None:
    """Void an issued credit note whose invoice is void: no sale stands for it to
    reverse, so it keeps no balance and posts nothing."""
    from celerp.events.engine import emit_event
    from celerp.services import auto_je

    for step, event_type, data in (
            ("balance", "doc.updated",
             {"fields_changed": {"amount_outstanding": {"old": note.get("amount_outstanding"), "new": 0.0}}}),
            ("void", "doc.voided", {"pre_void_status": note.get("status"), "reason": "Its invoice is void"})):
        await emit_event(session, company_id=company_id, entity_id=cn_id, entity_type="doc", event_type=event_type,
                         data=data, actor_id=None, location_id=None, source="api",
                         idempotency_key=f"credit-note-backfill:{cn_id}:{step}", metadata_={SOURCE: True})
    if await _posted_entry(session, company_id, cn_id):
        await auto_je.void_for_doc_voided(session, company_id=company_id, user_id=None, doc_id=cn_id)


async def _post(session, company_id, cn_id: str, note: dict, inv: dict, label: str) -> None:
    """Post the credit note's entry at its full amount at its invoice's rate, or bring
    one already posted at another rate to the invoice's, with what its refunds and
    applications already put back on the receivable (auto_je.true_up_credit_note_rate).
    A refund keeps the books it posted on, so undoing it gives back the cash it paid."""
    from celerp.events.engine import emit_event
    from celerp.models.company import Company
    from celerp.services import auto_je
    from celerp.services.money import to_decimal

    company = await session.get(Company, company_id)
    base = (company.settings or {}).get("currency", "USD") if company else "USD"
    rate = inv.get("conversion_rate")
    if rate not in (None, "") and to_decimal(note.get("conversion_rate") or rate) != to_decimal(rate):
        own = float(note.get("conversion_rate") or 1)
        payments = [{**p, "books": p.get("books") or {"bank_account": p.get("bank_account"), "base_currency": base,
                                                        "doc_rate": own, "settlement_rate": own}}
                    if p.get("method") == "refund" else p for p in note.get("payments") or []]
        changed = {"conversion_rate": {"old": note.get("conversion_rate"), "new": rate}}
        if payments != (note.get("payments") or []):
            changed["payments"] = {"old": note.get("payments"), "new": payments}
        await emit_event(session, company_id=company_id, entity_id=cn_id, entity_type="doc", event_type="doc.updated",
                         data={"fields_changed": changed, "takes_invoice_rate": True},
                         actor_id=None, location_id=None, source="api",
                         idempotency_key=f"credit-note-backfill:{cn_id}:rate", metadata_={SOURCE: True})
        log.info("Earlier %s takes its invoice's rate %s instead of %s", label, rate, note.get("conversion_rate"))
        note = {**note, "conversion_rate": rate, "payments": payments}
    issued = str(note.get("finalized_at") or note.get("issue_date") or date.today().isoformat())[:10]
    open_day = await _open_day(session, company_id, issued)
    memo = (f"Credit note {_number(note, cn_id)} issued {issued}, in a locked period, posted on "
            f"{open_day}") if open_day else None
    if await _posted_entry(session, company_id, cn_id):
        log.info("Earlier %s: its entry was already posted", label)
    else:
        await auto_je.create_for_credit_note_finalized(session, company_id=company_id, user_id=None, doc_id=cn_id,
                                                       doc=note, base_currency=base, ts=open_day, memo=memo)
        log.info("Earlier %s posted on %s", label, open_day or issued)
    await auto_je.true_up_credit_note_rate(session, company_id=company_id, user_id=None, doc_id=cn_id, doc=note,
                                           base_currency=base, ts=open_day, memo=memo)


async def _settle(session: AsyncSession, company_id, invoice_id: str, cn_id: str, effect) -> tuple[str, list] | None:
    """Settle one credit note. Returns what was done ("settled", "restored", or "pending"
    while its invoice is in draft) with the notice lines it adds, or None when it was
    left as it is."""
    from celerp.models.projections import Projection
    from celerp.services.money import round_money, to_decimal
    from celerp_docs.routes import emit_credit_settlement, legacy_credit_reduction

    invoice = await session.get(Projection, {"company_id": company_id, "entity_id": invoice_id}, populate_existing=True)
    cn = await session.get(Projection, {"company_id": company_id, "entity_id": cn_id}, populate_existing=True)
    inv, note = (invoice.state if invoice else None) or {}, (cn.state if cn else None) or {}
    number = _number(note, cn_id)
    label = f"credit note {number} on invoice {_number(inv, invoice_id)}"
    if inv.get("status") is None:
        log.warning("Earlier %s left as it is: the invoice is missing", label)
        return None
    if inv.get("status") == "draft":
        # Settled when the invoice is issued again (settle_legacy_credit_notes from the
        # finalize). Until then an issued credit note stands open, so its entry posts.
        if note.get("status") in _ISSUED:
            await _post(session, company_id, cn_id, note, inv, label)
        log.info("Earlier %s waits for its invoice to be issued again", label)
        return "pending", []
    currency = str(inv.get("currency") or "USD").upper()

    def money(state, key):
        return round_money(to_decimal(state.get(key) or 0), currency)

    reduction = legacy_credit_reduction(effect, inv)
    issued = note.get("status") in _ISSUED
    if inv.get("status") == "void" or not issued:
        await _mark_invoice(session, company_id, invoice_id, inv, cn_id, effect="restored", settled=Decimal(0),
                            credit_amount=reduction, currency=currency)
        if inv.get("status") == "void" and issued and not money(note, "amount_paid"):
            await _void_with_invoice(session, company_id, cn_id, note)
            log.info("Earlier %s voided: its invoice is void", label)
            return "restored", [("voided", number)]
        if inv.get("status") == "void":
            # It paid out before its invoice was voided: posted, it keeps only what it paid
            # out, and what it still had open is released (close_credit_on_void_invoice).
            from celerp_docs.routes import close_credit_on_void_invoice

            await _post(session, company_id, cn_id, note, inv, label)
            await close_credit_on_void_invoice(session, company_id, None, cn_id)
            log.warning("Earlier %s paid out %s before its invoice was voided", label, money(note, "amount_paid"))
            return "settled", [("settled", number)]
        log.info("Earlier %s is %s: gave the invoice back the %s it reduced", label,
                 note.get("status") or "deleted", reduction)
        return "restored", [("restored", number)]

    room = max(Decimal(0), money(inv, "total") - money(inv, "amount_paid") - money(inv, "credited"))
    left = money(note, "amount_outstanding")
    settled = min(left, room, reduction)
    await _mark_invoice(session, company_id, invoice_id, inv, cn_id, effect="reduced", settled=settled,
                        credit_amount=settled, currency=currency)
    if settled:
        await emit_credit_settlement(
            session, company_id, None, cn_id, note, outstanding=left - settled,
            credited=money(note, "credited") + settled, idempotency_key=f"credit-note-backfill:{cn_id}:credit-note",
            metadata={"source_credit_note": cn_id, "credit_note_effect": "reduced",
                      "credit_amount": float(settled), SOURCE: True})
    await _post(session, company_id, cn_id, note, inv, label)
    lines: list = [("settled", number)]
    unsettled = reduction - settled
    twice = min(unsettled, round_money(_spent_elsewhere(note, invoice_id), currency))
    if twice > 0:
        lines.append(("double_credit", {"number": number, "amount": f"{twice} {currency}"}))
        log.warning("Earlier %s had already paid out %s of the credit it gave its invoice; the invoice owes it again",
                    label, twice)
    kept = min(unsettled, left - settled)
    if kept > 0:
        lines.append(("open_credit", {"number": number, "amount": f"{kept} {currency}"}))
        log.info("Earlier %s keeps %s open as customer credit: its invoice was paid since", label, kept)
    log.info("Earlier %s settled for %s", label, settled)
    return "settled", lines


async def settle_legacy_credit_notes(session: AsyncSession, company_id=None, invoice_id: str | None = None) -> dict:
    """Settle every credit note an earlier release issued (one company's, or all, or the
    ones on one invoice, as its finalize does). Caller owns the transaction. A credit
    note that cannot be settled is logged and left; one whose invoice is in draft is
    pending until the invoice is issued again."""
    from celerp.notifications.service import notify_once
    from celerp.services.migrations import is_company_migration_staged

    lines: dict = {}
    settled = restored = errored = pending = 0
    staged: dict = {}
    for (cid, inv_id, cn_id), effect in (await _legacy_effects(session, company_id, invoice_id)).items():
        if cid not in staged:
            staged[cid] = await is_company_migration_staged(session, cid)
        if staged[cid]:
            continue
        try:
            async with session.begin_nested():
                done = await _settle(session, cid, inv_id, cn_id, effect)
        except HTTPException as exc:
            if exc.status_code == 503:
                raise  # backup in progress: nothing lands, the next start retries
            errored += 1
            log.warning("Could not settle earlier credit note %s: %s", cn_id, exc.detail)
            continue
        except Exception:
            errored += 1
            log.exception("Could not settle earlier credit note %s", cn_id)
            continue
        if done is None:
            continue
        what, added = done
        pending += what == "pending"
        settled += what == "settled"
        restored += what == "restored"
        if added:
            lines.setdefault(cid, []).extend(added)
    for cid, found in lines.items():
        outcomes = []
        for key in ("settled", "restored", "voided"):
            numbers = [n for k, n in found if k == key]
            if numbers:
                outcomes.append(_outcome(key, numbers=", ".join(numbers)))
        outcomes += [_outcome(k, **p) for k, p in found if k in ("double_credit", "open_credit")]
        await notify_once(session, cid, "system", _outcome("title")["message"],
                          _outcome("body", outcomes=" ".join(o["message"] for o in outcomes))["message"],
                          i18n={"title": f"{_NOTICE}.title", "body": f"{_NOTICE}.body",
                                "params": {"outcomes": outcomes}})
    return {"settled": settled, "restored": restored, "errored": errored, "pending": pending,
            "staged": any(staged.values())}


async def legacy_credit_notes_hook(*, session: AsyncSession) -> None:
    from celerp.migrations._data_reconcile import get_meta, set_meta

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, MARKER_KEY)):
        return
    out = await settle_legacy_credit_notes(session)
    if not out["errored"] and not out["staged"] and not out["pending"]:
        await conn.run_sync(lambda c: set_meta(c, MARKER_KEY, "done"))
    elif out["settled"] or out["errored"] or out["pending"]:
        log.info("Settled %d earlier credit note(s); %d failed, %d wait for their invoice (marker left unset; "
                 "the next start retries)", out["settled"], out["errored"], out["pending"])
