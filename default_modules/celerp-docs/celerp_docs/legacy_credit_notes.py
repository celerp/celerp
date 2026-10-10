# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time settlement of credit notes issued by an earlier release.

An earlier release took a credit note's amount off its invoice's balance when the credit
note was made, recorded nothing on the credit note itself and posted no entry for it. The
credit note kept its whole balance, so the same credit could be applied, refunded or paid
around a second time, and the receivable stayed in the books.

Each issued credit note an earlier release reduced its invoice by is settled as one issued
now would be: the original reduction becomes ``credited`` on both documents, the credit
note's open balance falls by the same amount, both statuses follow, and the credit note's
entry (Dr revenue and output tax, Cr receivable) is posted on the day it was issued, unless
an entry for it already exists. A day inside a locked period posts on the first open day
instead, with a memo naming the credit note. Document amounts and lines never change. A
draft credit note an earlier release reduced its invoice by gives the reduction back, so
issuing it later settles it as any other. Each document touched is logged.

Runs in the lifespan, gated by a marker so it runs once per database; a company staged for
a migration, or a credit note that could not be settled, leaves the marker unset for the
next start. Settling one twice changes nothing: the settlement it writes is what marks it
settled.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

MARKER_KEY = "legacy_credit_notes_settled"
SOURCE = "legacy_credit_note"
_TITLE = "Earlier credit notes settled"


def _body(numbers: list[str]) -> str:
    return ("Credit notes issued before this update reduced their invoices without recording the credit on "
            "the credit note or posting it. Each is now settled as a credit note issued today would be, "
            "with its entry posted on the day it was issued (or the first open day after a locked period). "
            f"Settled: {', '.join(numbers)}.")


def _number(state: dict, fallback: str) -> str:
    return str(state.get("doc_number") or state.get("ref_id") or fallback)


async def _legacy_effects(session: AsyncSession, company_id=None) -> dict:
    """Per (company, invoice, credit note), the last effect a credit note had on its
    invoice, for the pairs whose last effect is an earlier release's reduction: it says it
    reduced the balance and does not record by how much."""
    from celerp.models.ledger import LedgerEntry

    q = select(LedgerEntry).where(LedgerEntry.event_type == "doc.updated",
                                  LedgerEntry.metadata_["source_credit_note"].as_string().isnot(None))
    if company_id is not None:
        q = q.where(LedgerEntry.company_id == company_id)
    last: dict = {}
    for e in (await session.execute(q.order_by(LedgerEntry.id))).scalars():
        last[(e.company_id, e.entity_id, str(e.metadata_["source_credit_note"]))] = e
    return {k: e for k, e in last.items()
            if (e.metadata_ or {}).get("credit_note_effect", "reduced") == "reduced"
            and "credit_amount" not in (e.metadata_ or {})}


async def _posted_entry(session: AsyncSession, company_id, cn_id: str) -> bool:
    """Whether an issue entry for the credit note is already in the books (an imported one
    is posted when it comes in)."""
    from celerp.models.projections import Projection

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"je:auto:{cn_id}:fin%")))).scalars().all()
    return any((r.state or {}).get("status") == "posted" for r in rows)


async def _first_open_day(session: AsyncSession, company_id, day: str) -> str | None:
    """None when ``day`` is open, else the day after the company's lock date."""
    from celerp.models.company import Company
    from celerp.services.lot_origin import period_open

    if await period_open(session, company_id, day):
        return None
    company = await session.get(Company, company_id)
    lock = date.fromisoformat(str((company.settings or {}).get("lock_date")))
    return (lock + timedelta(days=1)).isoformat()


async def _settle(session: AsyncSession, company_id, invoice_id: str, cn_id: str, effect) -> str | None:
    """Settle one credit note. Returns what was done ("settled", "restored") or None."""
    from celerp.models.company import Company
    from celerp.models.projections import Projection
    from celerp.services import auto_je
    from celerp.services.money import round_money, to_decimal
    from celerp_docs.routes import _credit_note_owed, emit_credit_settlement, legacy_credit_reduction

    invoice = await session.get(Projection, {"company_id": company_id, "entity_id": invoice_id}, populate_existing=True)
    cn = await session.get(Projection, {"company_id": company_id, "entity_id": cn_id}, populate_existing=True)
    inv, note = (invoice.state if invoice else None) or {}, (cn.state if cn else None) or {}
    label = f"credit note {_number(note, cn_id)} on invoice {_number(inv, invoice_id)}"
    if inv.get("status") in (None, "draft", "void"):
        log.warning("Earlier %s left as it is: the invoice is %s", label, inv.get("status") or "missing")
        return None
    if note.get("status") == "draft":
        await _credit_note_owed(session, company_id, None, cn_id, note, invoice, "legacy-backfill",
                                "doc.reverted_to_draft")
        log.info("Earlier draft %s: gave the invoice back the reduction made when it was drafted", label)
        return "restored"
    if note.get("status") in (None, "void"):
        log.warning("Earlier %s left as it is: the credit note is %s", label, note.get("status") or "missing")
        return None

    currency = str(inv.get("currency") or "USD").upper()

    def money(state, key):
        return round_money(to_decimal(state.get(key) or 0), currency)

    amount = legacy_credit_reduction(effect, inv)
    metadata = {"source_credit_note": cn_id, "credit_note_effect": "reduced",
                "credit_amount": float(amount), SOURCE: True}
    await emit_credit_settlement(session, company_id, None, invoice_id, inv,
                                 outstanding=money(inv, "amount_outstanding"), credited=money(inv, "credited") + amount,
                                 idempotency_key=f"credit-note-backfill:{cn_id}:invoice", metadata=metadata)
    left = money(note, "amount_outstanding") - amount
    if left < 0:
        log.warning("Earlier %s had already spent %s of the credit it gave its invoice", label, -left)
    await emit_credit_settlement(session, company_id, None, cn_id, note,
                                 outstanding=max(Decimal(0), left), credited=money(note, "credited") + amount,
                                 idempotency_key=f"credit-note-backfill:{cn_id}:credit-note", metadata=metadata)
    if await _posted_entry(session, company_id, cn_id):
        log.info("Earlier %s settled for %s; its entry was already posted", label, amount)
        return "settled"
    company = await session.get(Company, company_id)
    base = (company.settings or {}).get("currency", "USD") if company else "USD"
    issued = str(note.get("finalized_at") or note.get("issue_date") or date.today().isoformat())[:10]
    open_day = await _first_open_day(session, company_id, issued)
    memo = (f"Credit note {_number(note, cn_id)} issued {issued}, posted on the first open day after "
            f"the locked period") if open_day else None
    await auto_je.create_for_credit_note_finalized(session, company_id=company_id, user_id=None, doc_id=cn_id,
                                                   doc=note, base_currency=base, ts=open_day, memo=memo)
    log.info("Earlier %s settled for %s and posted on %s", label, amount, open_day or issued)
    return "settled"


async def settle_legacy_credit_notes(session: AsyncSession, company_id=None) -> dict:
    """Settle every credit note an earlier release issued (one company's, or all). Caller
    owns the transaction. A credit note that cannot be settled is logged and left."""
    from celerp.models.projections import Projection
    from celerp.notifications.service import notify_once
    from celerp.services.migrations import is_company_migration_staged

    settled: dict = {}
    restored = errored = 0
    staged: dict = {}
    for (cid, invoice_id, cn_id), effect in (await _legacy_effects(session, company_id)).items():
        if cid not in staged:
            staged[cid] = await is_company_migration_staged(session, cid)
        if staged[cid]:
            continue
        try:
            async with session.begin_nested():
                done = await _settle(session, cid, invoice_id, cn_id, effect)
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
        if done == "settled":
            row = await session.get(Projection, {"company_id": cid, "entity_id": cn_id})
            settled.setdefault(cid, []).append(_number((row.state if row else None) or {}, cn_id))
        elif done == "restored":
            restored += 1
    for cid, numbers in settled.items():
        listed = ", ".join(numbers)
        await notify_once(session, cid, "system", _TITLE, _body(numbers),
                          i18n={"title": "notice.legacy_credit_notes.title",
                                "body": "notice.legacy_credit_notes.body", "params": {"numbers": listed}})
    count = sum(len(n) for n in settled.values())
    return {"settled": count, "restored": restored, "errored": errored, "staged": any(staged.values())}


async def legacy_credit_notes_hook(*, session: AsyncSession) -> None:
    from celerp.migrations._data_reconcile import get_meta, set_meta

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, MARKER_KEY)):
        return
    out = await settle_legacy_credit_notes(session)
    if not out["errored"] and not out["staged"]:
        await conn.run_sync(lambda c: set_meta(c, MARKER_KEY, "done"))
    elif out["settled"] or out["errored"]:
        log.info("Settled %d earlier credit note(s); %d failed (marker left unset; the next start retries)",
                 out["settled"], out["errored"])
