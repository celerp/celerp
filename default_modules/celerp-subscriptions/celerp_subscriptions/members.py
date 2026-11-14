# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Members and their standing, derived on read; nothing is stored as paid.

A cancelled membership, or one whose term has ended, reads ended; a void one never made anyone
a member and is not listed. Otherwise standing comes from the agreement's latest bill: the
invoice of its latest billed period, or one a quantity change in that period issued after it.
Settled (nothing outstanding, whether paid or fully credited) or not yet due reads current, up
to ``grace_days`` past its due date reads in grace, later reads lapsed. A membership that has
billed nothing yet owes nothing, so it reads current. A lifetime member is the contact of an
issued invoice that sold a lifetime membership item (category ``membership``, attribute
``membership_term`` Lifetime, as the item stood when the invoice was issued: its
``items_at_sale``), listed once that invoice is settled.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp_subscriptions import billing
from celerp_subscriptions.periods import shown
from celerp_subscriptions.search import _base_where

MEMBERSHIP_CATEGORY = "membership"
LIFETIME_TERM = "Lifetime"


def _settled(doc: dict) -> bool:
    return float(doc.get("amount_outstanding") or 0) <= 0


def standing(doc: dict | None, grace_days: int, today: date) -> str:
    """The standing one membership's latest bill gives it on ``today``."""
    if doc is None or _settled(doc):
        return "current"
    due = date.fromisoformat(str(doc.get("due_date") or doc.get("issue_date") or today.isoformat())[:10])
    if today <= due:
        return "current"
    return "in_grace" if today <= due + timedelta(days=grace_days) else "lapsed"


async def _states(session: AsyncSession, company_id, ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id.in_(ids)))).all()
    return {eid: state or {} for eid, state in rows}


def _bill_ids(agreement: dict) -> list[str]:
    """The agreement's bills, latest first: each billed period's document, then the invoices
    the quantity changes inside that period issued, in the order they took effect."""
    ranked = [((int(k), 0), p.get("doc_id")) for k, p in billing.periods_of(agreement).items()
              if k.lstrip("-").isdigit() and isinstance(p, dict) and p.get("doc_id") and p.get("state") == "billed"]
    ranked += [((int(a["period_index"]), n), a["invoice_id"])
               for n, a in enumerate(billing.amendments_of(agreement), start=1)
               if a.get("invoice_id") and isinstance(a.get("period_index"), int)]
    return [d for _, d in sorted(ranked, key=lambda r: r[0], reverse=True)]


async def _latest_bills(session: AsyncSession, company_id, agreements: list[dict]) -> dict[str, dict]:
    """Each agreement's latest bill that still bills (billing.bills), by agreement id."""
    claimed = {a["id"]: _bill_ids(a) for a in agreements}
    docs = await _states(session, company_id, {d for ids in claimed.values() for d in ids})
    return {eid: next((docs[d] | {"entity_id": d} for d in ids if billing.bills(docs.get(d))), None)
            for eid, ids in claimed.items()}


def _ended(agreement: dict, today: date) -> bool:
    """Cancelled, or past its term's (exclusive) end."""
    end = agreement.get("term_end")
    return agreement.get("status") == "cancelled" or bool(end and today >= date.fromisoformat(str(end)[:10]))


async def _lifetime_rows(session: AsyncSession, company_id) -> list[dict]:
    invoices = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string() == "invoice"))).all()
    issued = [(eid, s) for eid, s in invoices if billing.bills(s) and _settled(s)]

    def lifetime(sold: dict) -> bool:
        return sold.get("category") == MEMBERSHIP_CATEGORY and (
            sold.get("attributes") or {}).get("membership_term") == LIFETIME_TERM

    return [{"doc_id": eid, "ref": s.get("ref_id") or s.get("doc_number") or eid, "contact_id": s.get("contact_id"),
             "member_id": s.get("contact_id"), "member_name": s.get("contact_name") or "",
             "contact_name": s.get("contact_name") or "", "standing": "lifetime", "since": s.get("issue_date")}
            for eid, s in issued if any(lifetime(sold) for sold in (s.get("items_at_sale") or {}).values()
                                        if isinstance(sold, dict))]


async def member_rows(session: AsyncSession, company_id, today: date) -> list[dict]:
    """Every member with their standing on ``today``: issued membership agreements, then
    lifetime members, each group newest first."""
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        *_base_where(company_id, "sales"), Projection.state["kind"].as_string() == "membership")
        .order_by(Projection.entity_id.desc()))).all()
    agreements = [s | {"id": eid} for eid, s in rows if s.get("status") not in ("draft", "void")]
    bills = await _latest_bills(session, company_id, agreements)
    names = await _states(session, company_id, {a.get("beneficiary_contact_id") for a in agreements} - {None})
    members = []
    for a in agreements:
        bill, grace = bills[a["id"]], shown("grace_days", a.get("grace_days"))
        member = a.get("beneficiary_contact_id") or a.get("contact_id")
        members.append({
            "entity_id": a["id"], "ref": a.get("ref_id") or a.get("doc_number") or a["id"],
            "member_id": member, "member_name": (names.get(member) or {}).get("name") or a.get("contact_name") or "",
            "contact_id": a.get("contact_id"), "contact_name": a.get("contact_name") or "",
            "status": a.get("status"), "grace_days": grace, "standing": "ended" if _ended(a, today) else standing(bill, grace, today),
            "bill_id": (bill or {}).get("entity_id"), "due_date": (bill or {}).get("due_date"),
            "outstanding": float((bill or {}).get("amount_outstanding") or 0)})
    return members + await _lifetime_rows(session, company_id)
