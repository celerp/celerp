# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Cash-basis tax: a code marked cash basis is due as the document is paid.

At issue its tax posts to the pending tax role of the document's side (auto_je._tax_by_target).
After every allocation that changes what the document still owes (a payment, a credit note
applied, a refund, a void of any of them), ``settle`` moves the pending tax to the side's tax
role so that what stays pending is the tax share of what is still outstanding. Each move is its
own entry of the document (``cashbasis:{n}``), sized from the ledger, so it never drifts and a
void needs no reversal of its own: the next settle puts it right. Voiding the document reverses
the moves with its other entries (a recognition family).

At the cut-off the country pack declares (``cash_basis_cutoff``: month, day and years after the
issue year), tax still pending on a document is due whatever was paid: ``preview`` lists those
documents and ``confirm`` posts each one's remainder (``cashcutoff:{n}``) on its cut-off date. Undo is
voiding that entry."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import AccountRole as R, catalog_refusal
from celerp.events.engine import find_event_by_idempotency, refuse_other_request, request_fingerprint
from celerp.models.projections import Projection
from celerp.services.account_roles import current_settings, line_roles, resolve_many
from celerp.services.auto_je import (
    _doc_tax_lists, _emit_auto_posted_je, _line, _recognition_root, company_currency, has_cash_basis_tax,
)
from celerp.services.business_time import company_today
from celerp.services.doc_balance import amount_due, outstanding_balance
from celerp.services.je_keys import je_idempotency_key
from celerp.services.money import allocate_pro_rata, round_money, to_decimal, to_stored_float
from celerp.services.posting_dates import correction_day
from celerp.tax_regimes import cash_basis_cutoff, charged, doc_side, tax_entries

MOVE, CUTOFF = "cashbasis", "cashcutoff"
_ROLES = {"sales": (R.TAX_OUTPUT_PENDING, R.TAX_OUTPUT), "purchase": (R.TAX_INPUT_PENDING, R.TAX_INPUT)}
_SKIP = (None, "draft", "void")


def _roles(state: dict) -> tuple[R, R]:
    """The pending role and the tax role of a document's side."""
    return _ROLES[doc_side(state.get("doc_type"))]


async def _pending(session, company_id, doc_id: str, pending: R) -> tuple[Decimal, Decimal, bool]:
    """From the document's posted entries: what its pending tax role holds now and what its
    issue put there (moves left out), each as a debit balance, and whether a cut-off entry posted."""
    settings = await current_settings(session, company_id)
    prefix = f"je:auto:{doc_id}:"
    now = issued = Decimal(0)
    cut = False
    for row in (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.entity_id.startswith(prefix, autoescape=True)))).scalars().all():
        if row.state.get("status") != "posted":
            continue
        family = row.entity_id[len(prefix):].split(":")[0]
        cut = cut or family == CUTOFF
        for e in row.state.get("entries") or []:
            if pending.value in line_roles(settings, e):
                net = to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
                now += net
                issued += net if family not in (MOVE, CUTOFF) else 0
    return now, issued, cut


def _targets(state: dict) -> dict[str | None, Decimal]:
    """Where a document's cash-basis tax is due, by the account its code names (None: the
    side's tax role), weighted by the tax charged there."""
    out: dict[str | None, Decimal] = {}
    for tax in (t for taxes in _doc_tax_lists(state) for t in tax_entries(taxes)):
        if charged(tax) and tax.get("cash_basis"):
            out[tax.get("account")] = out.get(tax.get("account"), Decimal(0)) + to_decimal(tax.get("amount") or 0)
    return out


async def _move_lines(session, company_id, state: dict, amount: Decimal, currency: str) -> list[dict]:
    """Lines taking ``amount`` (a debit balance) off the document's pending tax role onto where
    the tax is due (_targets), in proportion."""
    pending, due = _roles(state)
    acc = await resolve_many(session, company_id, [pending, due])
    targets = _targets(state)
    shares = allocate_pro_rata(abs(amount), list(targets.values()), currency)
    debit = amount < 0

    def side(value: Decimal, is_debit: bool) -> dict:
        return {"debit": to_stored_float(value), "credit": 0.0} if is_debit else \
            {"debit": 0.0, "credit": to_stored_float(value)}
    lines = [_line(acc[pending], pending, **side(abs(amount), debit))]
    for account, share in zip(targets, shares):
        lines.append(_line(acc[due], due, **side(share, not debit)) if account in (None, acc[due])
                     else {"account": account, **side(share, not debit)})
    return lines


async def _post(session, company_id, user_id, doc_id: str, state: dict, family: str, amount: Decimal,
                day: str | None, idem: str = "", binding: str | None = None) -> str:
    currency = await company_currency(session, company_id)
    prefix = f"je:auto:{doc_id}:"
    rows = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_id.startswith(f"{prefix}{family}:", autoescape=True)))).scalars().all()
    suffix = f"{family}:{len({_recognition_root(r[len(prefix):]) for r in rows})}"
    await _emit_auto_posted_je(
        session, company_id=company_id, user_id=user_id, je_id=f"{prefix}{suffix}", record_id=doc_id,
        idem_create=idem or je_idempotency_key(doc_id, suffix, "c"),
        idem_posted=f"{idem}:p" if idem else je_idempotency_key(doc_id, suffix, "p"),
        memo=f"Cash-basis tax due for {doc_id}", ts=day,
        entries=await _move_lines(session, company_id, state, amount, currency),
        metadata_={"trigger": f"doc.{family}", "source_doc": doc_id},
        extra={"binding": request_fingerprint(binding)} if binding else None)
    return f"{prefix}{suffix}"


async def settle(session, company_id, user_id, doc_ids, day: str | None) -> None:
    """Move each document's pending cash-basis tax to where it is due, so what stays pending is
    its issue's pending tax times the share still outstanding (none once its cut-off posted),
    dated ``day``. Reads the document after the allocation; a no-op when nothing moves."""
    currency = await company_currency(session, company_id)
    for doc_id in dict.fromkeys(d for d in doc_ids if d):
        row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
        state = row.state if row is not None and row.entity_type == "doc" else {}
        if state.get("status") in _SKIP or not has_cash_basis_tax(state):
            continue
        now, issued, cut = await _pending(session, company_id, doc_id, _roles(state)[0])
        due, owed = amount_due(state), outstanding_balance(state)
        target = Decimal(0) if cut or not due or owed is None else round_money(issued * owed / due, currency)
        if round_money(now - target, currency):
            await _post(session, company_id, user_id, doc_id, state, MOVE, now - target, day)


def _cutoff(settings: dict, issue_day: str) -> dt.date | None:
    """The cut-off of a document issued on ``issue_day`` under the applied pack, if it declares one."""
    rule = cash_basis_cutoff(settings)
    if not rule or not issue_day:
        return None
    return dt.date(int(str(issue_day)[:4]) + int(rule.get("years_after") or 0), int(rule["month"]), int(rule["day"]))


async def _due(session, company_id) -> list[dict]:
    settings = await current_settings(session, company_id)
    today = await company_today(session, company_id)
    out = []
    for row in (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all():
        state = row.state
        if state.get("status") in _SKIP or not has_cash_basis_tax(state):
            continue
        cutoff = _cutoff(settings, state.get("issue_date"))
        if cutoff is None or cutoff > today:
            continue
        pending = _roles(state)[0]
        now, _, cut = await _pending(session, company_id, row.entity_id, pending)
        if cut or not now:
            continue
        shown = -now if pending == R.TAX_OUTPUT_PENDING else now
        out.append({"doc_id": row.entity_id, "doc_type": state.get("doc_type"),
                    "number": state.get("doc_number") or row.entity_id.removeprefix("doc:"),
                    "issue_date": state.get("issue_date"), "cutoff": cutoff.isoformat(),
                    "pending": to_stored_float(shown), "_now": now, "_state": state})
    return sorted(out, key=lambda d: (d["cutoff"], d["doc_id"]))


def _shown(docs: list[dict]) -> list[dict]:
    return [{k: v for k, v in d.items() if not k.startswith("_")} for d in docs]


async def preview(session, company_id) -> dict:
    """``{"documents", "fingerprint", "message"}``: each document whose cut-off has passed with
    tax still pending, and the fingerprint a confirm must send. ``message`` says when there is none."""
    from ui.i18n import t
    docs = _shown(await _due(session, company_id))
    message = None if docs else t("tax.cash_basis_none_pending",
                                  date=(await company_today(session, company_id)).isoformat())
    return {"documents": docs, "fingerprint": request_fingerprint(docs), "message": message}


async def confirm(session, company_id, user_id, binding: str, idempotency_key: str) -> list[str]:
    """Post each listed document's pending remainder on its cut-off date, as the preview showed;
    the entry ids. A retry with the same key answers the first; 409 on a stale preview, 422
    when nothing is due or a cut-off falls in a locked period."""
    key = f"{CUTOFF}:{idempotency_key}"
    done = await find_event_by_idempotency(session, company_id, f"{key}:0")
    if done is not None:
        refuse_other_request((done.data or {}).get("binding"), binding)
        ids = []
        while done is not None:
            ids.append(done.entity_id)
            done = await find_event_by_idempotency(session, company_id, f"{key}:{len(ids)}")
        return ids
    docs = await _due(session, company_id)
    if not docs:
        raise HTTPException(status_code=422, detail=catalog_refusal("tax.cash_basis_none_due"))
    if binding != request_fingerprint(_shown(docs)):
        raise HTTPException(status_code=409, detail=catalog_refusal("localization.stale_preview"))
    for d in docs:
        await correction_day(session, company_id, d["issue_date"], chosen=d["cutoff"])
    ids = []
    for n, d in enumerate(docs):
        ids.append(await _post(session, company_id, user_id, d["doc_id"], d["_state"], CUTOFF, d["_now"],
                               d["cutoff"], f"{key}:{n}", binding))
    return ids
