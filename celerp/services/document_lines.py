# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Document-line identity, the linked-item reference rule, the new-reference eligibility
rule, and the physical-item uniqueness invariant.

A line linked to an item must link to a real item of the same company: a stale form
or an import can carry the id of an item that was removed (an undone import), and a
document must never keep a reference to it. A non-splittable physical inventory item must appear at most once on an outbound
customer-stock document (invoice, memo). The rule is enforced once, at the event
boundary, so every current and future outbound document writer inherits it.
Inbound and internal documents (bill, consignment_in, novel types), splittable
items, and unlinked / free-text lines may repeat.
"""
from __future__ import annotations

from collections import Counter

from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.company_lock import lock_company
from celerp.services.line_measures import splitting_allowed
from celerp.services.lot_origin import DELETED

# The uniqueness invariant is an OUTBOUND customer-stock rule: a customer-facing
# invoice or memo must not list the same non-splittable physical lot twice.
# Inbound and internal documents (bill, consignment_in, novel types) legitimately
# may, so they are not governed. This mirrors the outbound set the codebase already
# names in celerp_docs.doc_constants.FULFILLABLE_STATUSES and must stay in lockstep
# with it. The same two types are the ones that claim stock, so they are also the ones
# that may not newly take an item another record has reserved.
DOCUMENT_ITEM_UNIQUE_DOC_TYPES: frozenset[str] = frozenset({"invoice", "memo"})


def doc_line_index(lines: list[dict], po_line_index: int, item_id: str | None, sku: str | None) -> int | None:
    """The document line received goods are for: the line at po_line_index, else the line
    naming their item or SKU."""
    if 0 <= po_line_index < len(lines):
        return po_line_index
    return next((i for i, li in enumerate(lines)
                 if (item_id and li.get("item_id") == item_id)
                 or (sku and str(li.get("sku") or "").strip() == sku.strip())), None)


def line_item_id(line: dict) -> str | None:
    """The single authoritative identity of a document line.

    A line's identity is its linked item/entity id only, never the SKU or
    description (two distinct lots can share a SKU; a free-text line has none).
    Returns None for an unlinked / free-text line, which may repeat freely.
    """
    return line.get("item_id") or line.get("entity_id")


async def listing_record(session, company_id, item_id: str) -> Projection | None:
    """A document or List with a line linked to ``item_id``, or None. Lines are keyed by
    ``item_id`` or ``entity_id`` depending on the writer (``line_item_id``), so either matches."""
    from sqlalchemy import cast, or_
    from sqlalchemy.dialects.postgresql import JSONB

    lines = cast(Projection.state["line_items"], JSONB)
    return (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type.in_(("doc", "list")),
        or_(lines.contains([{"item_id": item_id}]), lines.contains([{"entity_id": item_id}])),
    ).limit(1))).scalars().first()


def line_id_counts(line_items) -> Counter:
    """How many lines of a line set link to each item id (free-text lines are not counted)."""
    ids = (line_item_id(line) for line in line_items or [] if isinstance(line, dict))
    return Counter(ident for ident in ids if ident)


async def linked_items(session, company_id, line_items, *, known: Counter | None = None) -> dict[str, Projection]:
    """Resolve every linked line to its item, refusing a line whose item does not exist.

    The one rule for every document and List line writer: a line carrying both keys
    names one item under both, else 422 ``conflicting_reference``; each supplied item_id /
    entity_id must resolve to an item projection of ``company_id``, else 422
    ``invalid_reference`` naming the line (1-based) and the id. ``known`` counts the
    lines per id already on the stored record (``line_id_counts``): a save may carry
    that many lines for an id forward without re-proving it, so an old record whose item
    has since gone stays editable, but every line beyond the stored count is a new
    reference and must exist. Free-text lines (no id) are not checked. One
    company-scoped query.

    Returns the resolved projections keyed by id. A save that references an id more
    often than the stored record did takes the company lock before the check (a no-op
    for a writer that already holds it), so a removal that holds the lock (Undo) either
    commits first and the add is refused, or waits until the add has committed and then
    sees it.
    """
    known = known or Counter()
    for n, line in enumerate(line_items or [], 1):
        if isinstance(line, dict) and line.get("item_id") and line.get("entity_id") \
                and line["item_id"] != line["entity_id"]:
            raise HTTPException(status_code=422, detail={
                "code": "conflicting_reference",
                "message": f"Line {n} names two different items ({line['item_id']} and {line['entity_id']}). "
                           "Keep one item per line.",
                "line": n,
            })
    linked = [(n, line, line_item_id(line)) for n, line in enumerate(line_items or [], 1)
              if isinstance(line, dict) and line_item_id(line)]
    counts = Counter(ident for _, _, ident in linked)
    if not counts:
        return {}
    if any(n > known[ident] for ident, n in counts.items()):
        await lock_company(session, company_id)
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.entity_id.in_(counts),
        ).execution_options(populate_existing=True)
    )).scalars().all()
    items = {row.entity_id: row for row in rows}
    carried = Counter()
    for n, line, ident in linked:
        if ident in items:
            continue
        carried[ident] += 1
        if carried[ident] > known[ident]:
            label = line.get("name") or line.get("sku")
            where = f"Line {n} ({label})" if label else f"Line {n}"
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_reference",
                    "message": f"{where} references an item that does not exist: {ident}",
                    "item_id": ident,
                    "line": n,
                },
            )
    return items


def assert_new_references_eligible(
    items: dict[str, Projection], line_items, *, known: Counter, doc_type: str | None, entity_id: str | None,
) -> None:
    """Refuse a line set that newly references an item the record may not take.

    A draft item is not stock yet, and a deleted one is out of use until restored, so no
    document or List may newly reference either. An
    item reserved by another record, or by a status edit that no record owns, is held,
    so an invoice or memo (which claim stock) may not newly reference it; quotations,
    other documents and Lists may. "Newly" counts occurrences: a line beyond the number
    the stored record (``known``, ``line_id_counts``) held for that id is new, so a second
    line for an item the record already lists is judged like a first, while the lines it
    already held stay editable. ``items`` is what ``linked_items`` resolved, read under
    the company lock it takes for any such increase, which every change to an item's
    draft or reserved status also takes.

    422 whose message names every refused item; ``conflicts`` lists the reserved ones
    with the record holding each, for the page to link to.
    """
    reasons: list[str] = []
    conflicts: list[dict] = []
    for ident in sorted(line_id_counts(line_items) - known):
        if ident not in items:
            continue  # linked_items has already judged a line whose item is gone
        state = items[ident].state or {}
        sku = state.get("sku") or ident
        if state.get("status") == DELETED:
            reasons.append(f"{sku}: item was deleted - restore it from the Deleted list or pick another item")
        elif str(state.get("status") or "").lower() == "draft":
            reasons.append(f"{sku}: item is a draft - make it available first")
        elif (doc_type in DOCUMENT_ITEM_UNIQUE_DOC_TYPES and state.get("status") == "reserved"
              and state.get("status_doc_id") != entity_id):
            owner = state.get("status_doc_number") or state.get("status_doc_id") or "another document"
            reasons.append(f"{sku}: reserved on {owner} - release it there first")
            conflicts.append({"entity_id": ident, "sku": sku, "doc_id": state.get("status_doc_id"),
                              "doc_number": state.get("status_doc_number"), "message": reasons[-1]})
    if reasons:
        raise HTTPException(status_code=422, detail={"message": "; ".join(reasons), "conflicts": conflicts})


async def assert_document_item_uniqueness(session, company_id, doc_type, line_items) -> None:
    """Reject an OUTBOUND document line set that repeats a non-splittable physical item.

    Only invoice and memo are governed (``DOCUMENT_ITEM_UNIQUE_DOC_TYPES``); any other
    ``doc_type`` returns early, so inbound/internal and novel document types may repeat
    the same physical item freely.

    Fast path: if no linked id repeats, return without touching the database.
    Otherwise resolve the repeated ids in one company-scoped query:
      - a repeated id that resolves to a non-splittable item -> 409 duplicate;
      - a repeated id that resolves to a splittable item or to none -> allowed here
        (``linked_items`` has already refused every line for a missing item beyond the
        number the stored document held).
    """
    if doc_type not in DOCUMENT_ITEM_UNIQUE_DOC_TYPES:
        return
    if not line_items:
        return

    counts = line_id_counts(line_items)
    repeated = [ident for ident, n in counts.items() if n > 1]
    if not repeated:
        return  # fast path: no linked id repeats, no DB read

    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.entity_id.in_(repeated),
        )
    )).scalars().all()
    items = {row.entity_id: row for row in rows}

    for ident in repeated:
        item = items.get(ident)
        if item is not None and splitting_allowed(item.state) is False:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_document_item",
                    "message": "This item is already on the document.",
                    "item_id": ident,
                },
            )
