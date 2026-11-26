# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The withholding correction of an upgraded bill (Settings > Taxes > Withholding corrections).

Before tax codes a bill's withholding was a negative tax rate, so its entry debited input tax
net of the withholding. The upgrade keeps that entry and flags the bill (``legacy_wht_entry``).
The correction moves the withholding out of input tax into what is owed to the tax office, on a
date the user picks, and clears the flag; undo reverses it and flags the bill again. The
correction is a recognition entry of the bill (``wht-fix:{n}``), so voiding the bill reverses it
and unvoiding restores it with the rest of the bill's entries."""
from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from sqlalchemy import or_, select

from celerp.accounting_roles import AccountRole as R, catalog_refusal
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.account_roles import resolve_many
from celerp.services.auto_je import (
    _doc_recognition_jes, _emit_auto_posted_je, _ledger_withheld, _line, _recognition_root, _void_je_if_posted,
    company_currency, finalized_entry_day,
)
from celerp.services.company_lock import lock_projections
from celerp.services.je_keys import je_idempotency_key
from celerp.services.posting_dates import correction_day

FIX = "wht-fix"


async def listed(session, company_id) -> dict:
    """The issued bills and supplier credit notes still waiting for their correction (``bills``)
    and those corrected, whose correction can be undone (``corrected``), each newest first."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        or_(Projection.state["legacy_wht_entry"].as_boolean().is_(True),
            Projection.state["withholding_correction"].as_string().isnot(None))))).scalars().all()
    docs = sorted(((row.entity_id, row.state) for row in rows if row.state.get("status") not in (None, "draft", "void")),
                  key=lambda d: (str(d[1].get("issue_date") or ""), d[0]), reverse=True)

    def view(doc_id: str, s: dict) -> dict:
        return {"doc_id": doc_id, "doc_type": s.get("doc_type"), "number": _number(doc_id, s),
                "contact_id": s.get("contact_id"), "contact_name": s.get("contact_name"),
                "issue_date": s.get("issue_date"), "withheld": s.get("withheld"), "currency": s.get("currency")}
    return {"bills": [view(d, s) for d, s in docs if s.get("legacy_wht_entry")],
            "corrected": [view(d, s) for d, s in docs if not s.get("legacy_wht_entry") and s.get("withholding_correction")]}


async def _doc(session, company_id, doc_id: str) -> dict:
    row = (await lock_projections(session, company_id, [doc_id])).get(doc_id)
    if row is None or row.entity_type != "doc":
        raise HTTPException(status_code=404, detail=catalog_refusal("documents.document_not_found"))
    return row.state


def _number(doc_id: str, state: dict) -> str:
    return str(state.get("doc_number") or state.get("ref_id") or doc_id.removeprefix("doc:"))


async def _mark(session, company_id, user_id, doc_id: str, fields: dict, key: str) -> None:
    await emit_event(session, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.updated",
                     data={"fields_changed": fields}, actor_id=user_id, location_id=None, source="api",
                     idempotency_key=key, metadata_={})


async def correct(session, company_id, user_id, doc_id: str, day: date) -> str:
    """Post the correction of a flagged document on ``day`` and clear its flag; the entry's id."""
    state = await _doc(session, company_id, doc_id)
    withheld = float(_ledger_withheld(state, await company_currency(session, company_id)))
    if not state.get("legacy_wht_entry") or not withheld or state.get("status") in (None, "draft", "void"):
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "tax.withholding_correction_not_needed", document=_number(doc_id, state)))
    ts = await correction_day(session, company_id, finalized_entry_day(state), chosen=day.isoformat())
    prefix = f"je:auto:{doc_id}:"
    roots = {_recognition_root(suffix) for suffix in await _doc_recognition_jes(session, company_id, doc_id)}
    n = sum(1 for root in roots if root and root.startswith(f"{FIX}:"))
    je_id = f"{prefix}{FIX}:{n}"
    acc = await resolve_many(session, company_id, [R.TAX_INPUT, R.TAX_WITHHELD_PAYABLE])
    entries = [_line(acc[R.TAX_INPUT], R.TAX_INPUT, debit=withheld),
               _line(acc[R.TAX_WITHHELD_PAYABLE], R.TAX_WITHHELD_PAYABLE, credit=withheld)]
    if state.get("doc_type") == "debit_note":
        # A supplier credit note's entry credited input tax net of the withholding, so its
        # correction runs the other way.
        entries = [{**e, "debit": e["credit"], "credit": e["debit"]} for e in entries]
    await _emit_auto_posted_je(
        session, company_id=company_id, user_id=user_id, je_id=je_id, record_id=doc_id,
        idem_create=je_idempotency_key(doc_id, f"{FIX}:{n}", "c"),
        idem_posted=je_idempotency_key(doc_id, f"{FIX}:{n}", "p"),
        memo=f"Withholding correction for {_number(doc_id, state)}", ts=ts, entries=entries,
        metadata_={"trigger": "doc.withholding_corrected", "source_doc": doc_id})
    await _mark(session, company_id, user_id, doc_id,
                {"legacy_wht_entry": {"old": True, "drop": True}, "withholding_correction": {"old": None, "new": je_id}},
                f"{je_id}:doc")
    return je_id


async def undo(session, company_id, user_id, doc_id: str) -> None:
    """Reverse a document's correction, every entry standing for it, and flag it again."""
    state = await _doc(session, company_id, doc_id)
    je_id = state.get("withholding_correction")
    if not je_id or state.get("status") in (None, "draft", "void"):
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "tax.withholding_correction_none", document=_number(doc_id, state)))
    prefix = f"je:auto:{doc_id}:"
    root = je_id.removeprefix(prefix)
    for suffix in await _doc_recognition_jes(session, company_id, doc_id):
        if _recognition_root(suffix) == root:
            await _void_je_if_posted(
                session, company_id=company_id, user_id=user_id, doc_id=doc_id, je_id=f"{prefix}{suffix}",
                idem_key=f"{prefix}{suffix}:undo", reason="Withholding correction undone",
                trigger="doc.withholding_correction_undone")
    await _mark(session, company_id, user_id, doc_id,
                {"legacy_wht_entry": {"old": None, "new": True}, "withholding_correction": {"old": je_id, "drop": True}},
                f"{je_id}:undo-doc")
