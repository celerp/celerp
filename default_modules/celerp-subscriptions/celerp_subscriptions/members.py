# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Members and their standing, derived on read; nothing is stored as paid.

A cancelled membership, or one whose term has ended, reads ended; a void one never made anyone
a member and is not listed. Otherwise standing comes from the agreement's oldest bill that
still owes money (its billed periods' invoices and the ones its quantity changes issued): not
yet due reads current, up to ``grace_days`` past its due date reads in grace, later reads
lapsed. A later bill paid does not clear an earlier one still owing. When no bill owes anything
(each paid or fully credited, or nothing billed yet) it reads current. A lifetime member is the
contact of an issued invoice that sold a lifetime membership item (category ``membership``,
attribute ``membership_term`` Lifetime, as the item stood when the invoice was issued: its
``items_at_sale``), listed while that invoice is settled and the issued credit notes against it
have not credited the lifetime quantity back in full.
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


def _due(doc: dict, today: date) -> date:
    return date.fromisoformat(str(doc.get("due_date") or doc.get("issue_date") or today.isoformat())[:10])


def standing(doc: dict | None, grace_days: int, today: date) -> str:
    """The standing one membership's oldest bill still owing gives it on ``today``."""
    if doc is None or _settled(doc):
        return "current"
    due = _due(doc, today)
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
    """The agreement's bills, oldest first: each billed period's document, then the invoices
    the quantity changes inside that period issued, in the order they took effect."""
    ranked = [((int(k), 0), p.get("doc_id")) for k, p in billing.periods_of(agreement).items()
              if k.lstrip("-").isdigit() and isinstance(p, dict) and p.get("doc_id") and p.get("state") == "billed"]
    ranked += [((int(a["period_index"]), n), a["invoice_id"])
               for n, a in enumerate(billing.amendments_of(agreement), start=1)
               if a.get("invoice_id") and isinstance(a.get("period_index"), int)]
    return [d for _, d in sorted(ranked, key=lambda r: r[0])]


async def _standing_bills(session: AsyncSession, company_id, agreements: list[dict], today: date) -> dict[str, dict]:
    """Each agreement's bill its standing reads, by agreement id: among the bills that still
    bill (billing.bills), the one owing money that fell due first (the oldest of equals), or
    the latest when none owes anything; None when nothing bills."""
    claimed = {a["id"]: _bill_ids(a) for a in agreements}
    docs = await _states(session, company_id, {d for ids in claimed.values() for d in ids})
    out: dict[str, dict] = {}
    for eid, ids in claimed.items():
        billing_docs = [docs[d] | {"entity_id": d} for d in ids if billing.bills(docs.get(d))]
        owing = [d for d in billing_docs if not _settled(d)]
        out[eid] = min(owing, key=lambda d: _due(d, today)) if owing else (billing_docs[-1] if billing_docs else None)
    return out


def _ended(agreement: dict, today: date) -> bool:
    """Cancelled, or past its term's (exclusive) end."""
    end = agreement.get("term_end")
    return agreement.get("status") == "cancelled" or bool(end and today >= date.fromisoformat(str(end)[:10]))


async def _lifetime_rows(session: AsyncSession, company_id) -> list[dict]:
    invoices = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string() == "invoice"))).all()
    def lifetime(sold) -> bool:
        return isinstance(sold, dict) and sold.get("category") == MEMBERSHIP_CATEGORY and (
            sold.get("attributes") or {}).get("membership_term") == LIFETIME_TERM

    def quantity(doc: dict, items: set[str]) -> float:
        return sum(float(li.get("quantity") or 0) for li in doc.get("line_items") or []
                   if isinstance(li, dict) and li.get("item_id") in items)

    sold = {eid: (s, {i for i, at_sale in (s.get("items_at_sale") or {}).items() if lifetime(at_sale)})
            for eid, s in invoices if billing.bills(s) and _settled(s)}
    sold = {eid: (s, items) for eid, (s, items) in sold.items() if items}
    notes = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string() == "credit_note",
        Projection.state["original_doc_id"].as_string().in_(list(sold))))).scalars().all() if sold else []
    credited: dict[str, float] = {}
    for n in notes:
        if billing.bills(n):
            eid = n["original_doc_id"]
            credited[eid] = credited.get(eid, 0.0) + quantity(n, sold[eid][1])
    issued = [(eid, s) for eid, (s, items) in sold.items() if quantity(s, items) - credited.get(eid, 0.0) > 0]
    return [{"doc_id": eid, "ref": s.get("ref_id") or s.get("doc_number") or eid, "contact_id": s.get("contact_id"),
             "member_id": s.get("contact_id"), "member_name": s.get("contact_name") or "",
             "contact_name": s.get("contact_name") or "", "standing": "lifetime", "since": s.get("issue_date")}
            for eid, s in issued]


async def member_rows(session: AsyncSession, company_id, today: date) -> list[dict]:
    """Every member with their standing on ``today``: issued membership agreements, then
    lifetime members, each group newest first."""
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        *_base_where(company_id, "sales"), Projection.state["kind"].as_string() == "membership")
        .order_by(Projection.entity_id.desc()))).all()
    agreements = [s | {"id": eid} for eid, s in rows if s.get("status") not in ("draft", "void")]
    bills = await _standing_bills(session, company_id, agreements, today)
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
