# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Contact-scoped consignment / memo holdings and realized sale prices.

Three mirror-image questions, answered as pure reads over existing projection state
(no new columns, no migration):

- What is currently out on memo TO a customer? Items with status ``memo_out`` whose
  ``fulfilled_for_docs`` points at one of that customer's memo docs. Valued at the
  price the customer was quoted, i.e. the memo LINE's ``unit_price`` (per unit,
  post-discount) times the quantity still out, NOT the item's catalog price.
- What do we currently hold on consignment FROM a supplier? Items with
  ``consignment_flag == "in"`` created by one of that supplier's ``consignment_in``
  docs (tracked on the doc as ``received_item_ids``). Valued at cost.
- What did a sold item actually sell for? The per-unit price on its selling
  document's line, matched by item reference or SKU (``sold_prices``).

Every function is pure over already-loaded rows so the inventory endpoint can share
one item scan, and so the membership predicate is unit-testable in isolation. The
value each returns is exactly what the list endpoint should display and total for the
scoped view, so the card total and the list reconcile by construction.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


def _doc_lines(docs: Iterable[tuple[str, dict]]) -> Iterator[tuple[str, dict]]:
    """Yield (doc_id, line) for every line item across the given docs' states."""
    for doc_id, state in docs:
        for line in (state or {}).get("line_items", []) or []:
            yield str(doc_id), line


def _lines_by_ref(docs: Iterable[tuple[str, dict]]) -> dict[tuple[str, str], dict]:
    """(doc_id, item-ref) -> line, for every doc line carrying an item reference."""
    out: dict[tuple[str, str], dict] = {}
    for doc_id, line in _doc_lines(docs):
        eid = line.get("entity_id") or line.get("item_id")
        if eid:
            out[(doc_id, eid)] = line
    return out


def _num(value: object) -> float | None:
    """Coerce a stored price/quantity to float, or None when absent/blank/garbage."""
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def memo_holdings(
    items: Iterable[tuple[str, dict]],
    memo_docs: Iterable[tuple[str, dict]],
) -> dict[str, float]:
    """Map item_id -> quoted value for items still out on memo to a customer.

    ``items``: (entity_id, state) for the company's items.
    ``memo_docs``: (entity_id, state) for that customer's issued memo docs.

    An item is out on memo when its status is ``memo_out`` and its
    ``fulfilled_for_docs`` intersects the memo docs supplied. The value is the
    memo line's ``unit_price`` times the quantity still out (so partial returns
    step the value down), falling back to the line's ``line_total`` when no unit
    price is stored, and to 0.0 when the line carries no price or cannot be
    matched. An unpriceable item is still returned: the point of the view is that
    nothing out on memo is quietly missing from it.
    """
    memo_docs = list(memo_docs)
    doc_ids = {doc_id for doc_id, _ in memo_docs}
    # (doc_id, item_id) -> line, so an item that was on memo to A, returned, then
    # re-sent to B resolves against the doc that is actually in fulfilled_for_docs.
    line_by = _lines_by_ref(memo_docs)

    out: dict[str, float] = {}
    for item_id, state in items:
        state = state or {}
        if str(state.get("status") or "").lower() != "memo_out":
            continue
        member_docs = doc_ids & set(state.get("fulfilled_for_docs") or [])
        if not member_docs:
            continue
        remaining = _num(state.get("quantity")) or 0.0
        value = 0.0
        # Sorted so the priced line is chosen deterministically: an item is normally out
        # on exactly one memo, but never let the value depend on set iteration order.
        for doc_id in sorted(member_docs):
            line = line_by.get((doc_id, item_id))
            if line is None:
                continue
            unit = _num(line.get("unit_price"))
            if unit is not None:
                value = unit * remaining
            else:
                value = _num(line.get("line_total")) or 0.0
            break
        out[item_id] = round(value, 2)
    return out


def consignment_holdings(
    items: Iterable[tuple[str, dict]],
    consignment_docs: Iterable[tuple[str, dict]],
) -> dict[str, float]:
    """Map item_id -> cost value for items still held on consignment from a supplier.

    ``items``: (entity_id, state) for the company's items.
    ``consignment_docs``: (entity_id, state) for that supplier's issued consignment_in docs.

    An item is still held when it was created by one of those docs (its id is in the
    doc's ``received_item_ids``) and it still carries ``consignment_flag == "in"``
    (the flag clears when the goods are fully returned to the supplier). Value is the
    item's ``cost_total``, which already tracks remaining quantity after partial returns.
    """
    received: set[str] = set()
    for _doc_id, state in consignment_docs:
        received.update((state or {}).get("received_item_ids") or [])

    out: dict[str, float] = {}
    for item_id, state in items:
        state = state or {}
        if item_id not in received or state.get("consignment_flag") != "in":
            continue
        cost = _num(state.get("cost_total"))
        if cost is None:
            unit = _num(state.get("cost_price"))
            cost = (unit * (_num(state.get("quantity")) or 0.0)) if unit is not None else 0.0
        out[item_id] = round(cost, 2)
    return out


def sold_prices(
    items: Iterable[tuple[str, dict]],
    sold_docs: Iterable[tuple[str, dict]],
) -> dict[str, float | None]:
    """Map item_id -> realized per-unit sale price for sold items.

    ``items``: (entity_id, state) for the sold items to price.
    ``sold_docs``: (entity_id, state) for the documents that sold them.

    The price is read from the line of the document that sold the item
    (its ``status_doc_id``). The line is matched by its item reference where the doc
    carries one (imported docs, per-line fulfillment), else by SKU (engine-fulfilled
    invoices created from a SKU carry no item reference on the line, only the sku the
    item still holds). The value is the line's ``unit_price`` (per sell-unit,
    post-discount); when only a ``line_total`` is stored it is divided by the line
    quantity to a per-unit figure. Returns None for an item whose selling line or
    price cannot be resolved, so the view shows an honest ``--`` rather than a
    fabricated 0.
    """
    sold_docs = list(sold_docs)
    line_by_ref = _lines_by_ref(sold_docs)
    line_by_sku: dict[tuple[str, str], dict] = {}
    for doc_id, line in _doc_lines(sold_docs):
        sku = str(line.get("sku") or "").strip()
        if sku:
            line_by_sku.setdefault((doc_id, sku), line)  # first line for the sku wins

    out: dict[str, float | None] = {}
    for item_id, state in items:
        state = state or {}
        doc_id = state.get("status_doc_id")
        if not doc_id:
            out[item_id] = None
            continue
        line = line_by_ref.get((str(doc_id), item_id))
        if line is None:
            sku = str(state.get("sku") or "").strip()
            line = line_by_sku.get((str(doc_id), sku)) if sku else None
        if line is None:
            out[item_id] = None
            continue
        unit = _num(line.get("unit_price"))
        if unit is None:
            total, qty = _num(line.get("line_total")), _num(line.get("quantity"))
            unit = (total / qty) if (total is not None and qty) else None
        out[item_id] = round(unit, 2) if unit is not None else None
    return out
