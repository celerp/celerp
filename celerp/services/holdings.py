# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Contact-scoped consignment / memo holdings and realized sale prices.

Three mirror-image questions, answered as pure reads over existing projection state
(no new columns, no migration):

- What is currently out on memo TO a customer? Items with status ``memo_out`` whose
  ``fulfilled_for_docs`` points at one of that customer's memo docs. Valued at what
  the memo LINE charges per unit (``_LineIndex``) times the quantity still out, NOT the
  item's catalog price.
- What do we currently hold on consignment FROM a supplier? Items with
  ``consignment_flag == "in"`` created by one of that supplier's ``consignment_in``
  docs (tracked on the doc as ``received_item_ids``). Valued at cost.
- What did a sold item actually sell for? The per-unit price on its selling
  document's line (``sold_prices``).

Document amounts are valued the way the books value them: the line's per-unit charge
(``line_total`` over quantity, so a line discount is included), less its pro-rata share
of any header discount, converted at the document's ``conversion_rate`` into the
company currency. All arithmetic is Decimal and rounds through celerp.services.money.
A line that cannot be attributed to an item unambiguously, or carries no price, yields
None (shown as ``--``) and is counted, never estimated.

Every function is pure over already-loaded rows so the inventory endpoint can share
one item scan, and so the membership predicate is unit-testable in isolation. The
value each returns is exactly what the list endpoint should display and total for the
scoped view, so the card total and the list reconcile by construction.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from celerp.services.money import round_money, round_rate, to_decimal, to_stored_float


def _dec(value: object) -> Decimal | None:
    """A stored number as Decimal, or None when absent, blank or not a number."""
    if value in (None, ""):
        return None
    try:
        return to_decimal(value)  # type: ignore[arg-type]
    except (ArithmeticError, TypeError, ValueError):
        return None


def _header_factor(state: dict) -> Decimal:
    """Share of each line amount the customer actually pays after the header discount:
    (subtotal - discount_amount) / subtotal, so the discount is allocated to lines in
    proportion to their amount. 1 when there is no header discount."""
    subtotal, discount = _dec(state.get("subtotal")), _dec(state.get("discount_amount"))
    if not subtotal or not discount:
        return Decimal(1)
    return (subtotal - discount) / subtotal


class _LineIndex:
    """Every line of a set of documents, resolved to one item by one rule.

    A line belongs to an item by its item reference, else (for lines that carry no item
    reference) by the item's SKU. When several lines match, they resolve only if they all
    charge the same exact per-unit amount; otherwise there is no way to tell which line
    is the item's, and the result is None.
    """

    def __init__(self, docs: Iterable[tuple[str, dict]], base_currency: str) -> None:
        self._base = base_currency
        self._by_ref: dict[tuple[str, str], list[Decimal | None]] = {}
        self._by_sku: dict[tuple[str, str], list[Decimal | None]] = {}
        for doc_id, state in docs:
            state = state or {}
            factor = _header_factor(state) * (_dec(state.get("conversion_rate")) or Decimal(1))
            for line in state.get("line_items", []) or []:
                unit = self._line_unit(line)
                value = unit * factor if unit is not None else None
                ref = line.get("entity_id") or line.get("item_id")
                sku = str(line.get("sku") or "").strip()
                if ref:
                    self._by_ref.setdefault((str(doc_id), str(ref)), []).append(value)
                elif sku:
                    self._by_sku.setdefault((str(doc_id), sku), []).append(value)

    @staticmethod
    def _line_unit(line: dict) -> Decimal | None:
        """What a line charges per unit in its document currency, before header discount:
        ``line_total`` over ``quantity`` when both are stored, else ``unit_price``."""
        total, qty = _dec(line.get("line_total")), _dec(line.get("quantity"))
        if total is not None and qty:
            return total / qty
        return _dec(line.get("unit_price"))

    def unit_value(self, doc_id: str, item_id: str, sku: str | None) -> Decimal | None:
        """Per-unit value, in the company currency, of the line that carries this item."""
        doc_id = str(doc_id)
        values = self._by_ref.get((doc_id, str(item_id)))
        if values is None and sku and sku.strip():
            values = self._by_sku.get((doc_id, sku.strip()))
        if not values or values[0] is None or any(v != values[0] for v in values):
            return None
        return values[0]


def memo_holdings(
    items: Iterable[tuple[str, dict]],
    memo_docs: Iterable[tuple[str, dict]],
    base_currency: str,
) -> dict[str, float | None]:
    """Map item_id -> quoted value for items still out on memo to a customer.

    ``items``: (entity_id, state) for the company's items.
    ``memo_docs``: (entity_id, state) for that customer's issued memo docs.

    An item is out on memo when its status is ``memo_out`` and its
    ``fulfilled_for_docs`` intersects the memo docs supplied. The value is what the
    memo line charges per unit times the quantity still out (so partial returns step
    the value down). An item whose line cannot be resolved or priced is still returned,
    valued None: the point of the view is that nothing out on memo is quietly missing.
    """
    memo_docs = list(memo_docs)
    doc_ids = {str(doc_id) for doc_id, _ in memo_docs}
    lines = _LineIndex(memo_docs, base_currency)

    out: dict[str, float | None] = {}
    for item_id, state in items:
        state = state or {}
        if str(state.get("status") or "").lower() != "memo_out":
            continue
        member_docs = doc_ids & {str(d) for d in state.get("fulfilled_for_docs") or []}
        if not member_docs:
            continue
        value: float | None = None
        # Sorted so the priced line is chosen deterministically: an item is normally out
        # on exactly one memo, but never let the value depend on set iteration order.
        for doc_id in sorted(member_docs):
            unit = lines.unit_value(doc_id, item_id, state.get("sku"))
            if unit is not None:
                qty = _dec(state.get("quantity")) or Decimal(0)
                value = to_stored_float(round_money(unit * qty, base_currency))
                break
        out[item_id] = value
    return out


def consignment_holdings(
    items: Iterable[tuple[str, dict]],
    consignment_docs: Iterable[tuple[str, dict]],
    base_currency: str,
) -> dict[str, float | None]:
    """Map item_id -> cost value for items still held on consignment from a supplier.

    ``items``: (entity_id, state) for the company's items.
    ``consignment_docs``: (entity_id, state) for that supplier's issued consignment_in docs.

    An item is still held when it was created by one of those docs (its id is in the
    doc's ``received_item_ids``) and it still carries ``consignment_flag == "in"``
    (the flag clears when the goods are fully returned to the supplier). Value is the
    item's ``cost_total``, which already tracks remaining quantity after partial returns,
    else cost_price x quantity, else None.
    """
    received: set[str] = set()
    for _doc_id, state in consignment_docs:
        received.update((state or {}).get("received_item_ids") or [])

    out: dict[str, float | None] = {}
    for item_id, state in items:
        state = state or {}
        if item_id not in received or state.get("consignment_flag") != "in":
            continue
        cost = _dec(state.get("cost_total"))
        if cost is None:
            unit = _dec(state.get("cost_price"))
            cost = unit * (_dec(state.get("quantity")) or Decimal(0)) if unit is not None else None
        out[item_id] = to_stored_float(round_money(cost, base_currency)) if cost is not None else None
    return out


def sold_prices(
    items: Iterable[tuple[str, dict]],
    sold_docs: Iterable[tuple[str, dict]],
    base_currency: str,
) -> dict[str, float | None]:
    """Map item_id -> realized per-unit sale price, in the company currency, for sold items.

    ``items``: (entity_id, state) for the sold items to price.
    ``sold_docs``: (entity_id, state) for the documents that sold them.

    The price is read from the line of the document that sold the item (its
    ``status_doc_id``), resolved by ``_LineIndex``. Returns None for an item whose
    selling line or price cannot be resolved, so the view shows an honest ``--``
    rather than a fabricated 0.
    """
    lines = _LineIndex(sold_docs, base_currency)
    out: dict[str, float | None] = {}
    for item_id, state in items:
        state = state or {}
        doc_id = state.get("status_doc_id")
        unit = lines.unit_value(doc_id, item_id, state.get("sku")) if doc_id else None
        out[item_id] = to_stored_float(round_rate(unit, base_currency)) if unit is not None else None
    return out


def value_total(values: Iterable[float | None], base_currency: str) -> tuple[float, int]:
    """Sum of the resolved *values* at company-currency precision, and how many were None."""
    total, missing = Decimal(0), 0
    for v in values:
        if v is None:
            missing += 1
        else:
            total += to_decimal(v)
    return to_stored_float(round_money(total, base_currency)), missing


def sold_value_total(rows: Iterable[dict], prices: dict[str, float | None], base_currency: str) -> tuple[float, int]:
    """Realized value of sold *rows* (flattened item records): unit price x quantity, each
    row rounded at company-currency precision, summed over every row whose price resolved
    in *prices*. Returns (total, rows_without_price). A row with no numeric quantity is a
    single piece."""
    def row_value(r: dict) -> float | None:
        price = prices.get(r.get("id"))
        if price is None:
            return None
        qty = _dec(r.get("quantity"))
        return to_stored_float(round_money(to_decimal(price) * (qty if qty is not None else 1), base_currency))
    return value_total((row_value(r) for r in rows), base_currency)
