# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""What a document's physical stock lines have been sent and still owe."""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import VALUED_FROM_KEY
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.units import is_non_stock_line


def _to_uuid(val) -> _uuid.UUID:
    """Coerce str or UUID to UUID."""
    return val if isinstance(val, _uuid.UUID) else _uuid.UUID(str(val))


async def returned_lots(session: AsyncSession, cid, doc_ids: list[str]) -> dict[str, list[tuple[str | None, str, float]]]:
    """Per document, the goods still received back on credit notes raised on it: for each
    returned lot, the sold lot it was valued from (None when it names none), its SKU and
    its quantity."""
    from sqlalchemy import select

    from celerp.models.ledger import LedgerEntry

    notes = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string() == "credit_note",
        Projection.state["original_doc_id"].as_string().in_(doc_ids),
    ))).scalars().all()
    back = {r["item_id"]: (n.state["original_doc_id"], str(r.get("sku") or "").strip(), float(r.get("quantity") or 0))
            for n in notes for r in n.state.get("return_received_items") or [] if r.get("item_id")}
    if not back:
        return {}
    made = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.event_type == "item.created",
        LedgerEntry.entity_id.in_(list(back)),
    ))).scalars().all()
    sold_from = {e.entity_id: (e.metadata_ or {}).get(VALUED_FROM_KEY) for e in made}
    out: dict[str, list[tuple[str | None, str, float]]] = {}
    for lot, (doc_id, sku, qty) in back.items():
        out.setdefault(doc_id, []).append((sold_from.get(lot), sku, qty))
    return out


@dataclass(frozen=True)
class OutstandingLine:
    """A physical stock line of a document: what it ordered and what it has received."""
    index: int
    item_id: str
    ordered: float
    fulfilled: float

    @property
    def outstanding(self) -> float:
        return max(0.0, self.ordered - self.fulfilled)


async def outstanding_physical_lines(session: AsyncSession, company_id, docs: list[Projection]) -> dict[str, list[OutstandingLine]]:
    """For each document, its physical stock lines with what each ordered and what has been
    sent against it and not taken back. Service, freight and other non-stock lines, and lines
    naming no item, are left out; document lines are never changed.

    What is sent is read from the fulfillment record, as the invoice's cost of sales reads
    it: each lot whose latest fulfillment event for the document ships it counts at its
    quantity, on the line it belongs to (auto_je.line_of_lot), so a reversal asks for the
    goods again. Goods an invoice bills from a memo were sent under the memo, so the memo's
    record counts for the invoice. A lot no line can claim is put on the document's lines
    of its SKU in order, each taking at most what it ordered.

    Goods a customer sent back on a credit note raised on the document were taken back too:
    each returned lot still received comes off the line of the sold lot it was valued from,
    or, when it names none, off the document's lines of its SKU from the last."""
    from sqlalchemy import select

    from celerp.models.ledger import LedgerEntry

    docs = [d for d in docs if d is not None]
    if not docs:
        return {}
    cid = _to_uuid(company_id)
    # The document whose deliveries a fulfillment event records: its own, or the invoice a
    # memo was billed on.
    owner: dict[str, str] = {d.entity_id: d.entity_id for d in docs}
    for d in docs:
        memo = (d.state or {}).get("source_memo_id")
        if memo:
            owner[memo] = d.entity_id
    events = (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.company_id == cid,
            LedgerEntry.entity_type == "item",
            LedgerEntry.event_type.in_(("item.fulfilled", "item.fulfillment_reversed")),
            LedgerEntry.data["source_doc_id"].as_string().in_(list(owner)),
        ).order_by(LedgerEntry.id)
    )).scalars().all()
    latest: dict[tuple[str, str], LedgerEntry] = {}  # (document, lot) -> latest event
    recorded: dict[tuple[str, str], int | None] = {}  # (document, lot) -> line its latest fulfillment named
    for e in events:
        source = (e.data or {}).get("source_doc_id")
        key = (owner[source], e.entity_id)
        latest[key] = e
        if e.event_type == "item.fulfilled":
            # A memo's line numbers are the memo's, not the invoice's.
            recorded[key] = auto_je.recorded_line_index(e) if source == owner[source] else None
    out_lots = {key: e for key, e in latest.items() if e.event_type == "item.fulfilled"}
    returned = await returned_lots(session, cid, [d.entity_id for d in docs])

    wanted = {lot for _doc, lot in out_lots} | {sold for back in returned.values() for sold, _, _ in back if sold}
    for d in docs:
        for li in (d.state or {}).get("line_items") or []:
            if li.get("entity_id") or li.get("item_id"):
                wanted.add(li.get("entity_id") or li.get("item_id"))
    items = {r.entity_id: (r.state or {}) for r in (await session.execute(
        select(Projection).where(Projection.company_id == cid, Projection.entity_id.in_(list(wanted)))
    )).scalars().all()} if wanted else {}

    result: dict[str, list[OutstandingLine]] = {}
    for d in docs:
        line_items = (d.state or {}).get("line_items") or []
        physical: dict[int, str] = {}
        for idx, li in enumerate(line_items):
            item_id = li.get("entity_id") or li.get("item_id")
            st = items.get(item_id) if item_id else None
            if st is not None and not is_non_stock_line(st.get("inventory_type"), st.get("sell_by")):
                physical[idx] = item_id
        ordered = {idx: float(line_items[idx].get("quantity") or 0) for idx in physical}
        sent = dict.fromkeys(physical, 0.0)
        unclaimed: list[tuple[str, float]] = []
        line_of: dict[str, int | None] = {}  # lot sent -> the line it went on
        for (doc_id, lot), e in out_lots.items():
            if doc_id != d.entity_id:
                continue
            lot_state = items.get(lot)
            qty = float((lot_state or {}).get("quantity") or (e.data or {}).get("quantity_fulfilled") or 0)
            idx = auto_je.line_of_lot(line_items, lot, lot_state or {}, recorded.get((doc_id, lot)))
            line_of[lot] = idx
            if idx in sent:
                sent[idx] += qty
            else:
                unclaimed.append((str((lot_state or {}).get("sku") or "").strip(), qty))
        for sku, qty in unclaimed:
            for idx in physical:
                if qty <= 1e-9:
                    break
                if str(line_items[idx].get("sku") or "").strip() != sku:
                    continue
                take = min(qty, max(0.0, ordered[idx] - sent[idx]))
                sent[idx] += take
                qty -= take
        for sold, sku, qty in returned.get(d.entity_id, []):
            lines = [line_of[sold]] if line_of.get(sold) in sent else [
                idx for idx in reversed(physical) if str(line_items[idx].get("sku") or "").strip() == sku]
            for idx in lines:
                take = min(qty, sent[idx])
                sent[idx] -= take
                qty -= take
        result[d.entity_id] = [
            OutstandingLine(index=idx, item_id=item_id, ordered=ordered[idx], fulfilled=min(sent[idx], ordered[idx]))
            for idx, item_id in physical.items()
        ]
    return result
