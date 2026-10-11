# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Document-line identity, the linked-item reference rule, the new-reference eligibility
rules (other records' holds, and holds for another line of the same record), and the
physical-item uniqueness invariant.

A line linked to an item must link to a real item of the same company: a stale form
or an import can carry the id of an item that was removed (an undone import), and a
document must never keep a reference to it. A non-splittable physical inventory item must appear at most once on an outbound
customer-stock document (invoice, memo). The rule is enforced once, at the event
boundary, so every current and future outbound document writer inherits it.
Inbound and internal documents (bill, consignment_in, novel types), splittable
items, and unlinked / free-text lines may repeat.
"""
from __future__ import annotations

import uuid
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import refusal
from celerp.models.projections import Projection
from celerp.services.company_lock import lock_company
from celerp.services.line_measures import splitting_allowed
from ui.i18n import t
from celerp.services.lot_origin import DELETED

# The uniqueness invariant is an OUTBOUND customer-stock rule: a customer-facing
# invoice or memo must not list the same non-splittable physical lot twice.
# Inbound and internal documents (bill, consignment_in, novel types) legitimately
# may, so they are not governed. This mirrors the outbound set the codebase already
# names in celerp_docs.doc_constants.FULFILLABLE_STATUSES and must stay in lockstep
# with it. The same two types are the ones that claim stock, so they are also the ones
# that may not newly take an item another record has reserved.
DOCUMENT_ITEM_UNIQUE_DOC_TYPES: frozenset[str] = frozenset({"invoice", "memo"})


def _holds_receipt(line: dict, entry: dict, *, in_place: bool = False) -> bool:
    """Whether ``line`` holds the goods a receipt entry names: its item (by whichever key its
    writer used, line_item_id) or SKU. An entry naming
    neither (an expense or asset line) is held by a line naming neither, the one ``in_place``
    at its recorded position whatever it is called, any other only under the same name."""
    item_id = entry.get("item_id")
    sku = str(entry.get("sku") or "").strip()
    if item_id or sku:
        return bool((item_id and line_item_id(line) == item_id)
                    or (sku and str(line.get("sku") or "").strip() == sku))
    if line_item_id(line) or str(line.get("sku") or "").strip():
        return False
    name = str(entry.get("name") or "").strip()
    return in_place or (bool(name) and str(line.get("name") or line.get("description") or "").strip() == name)


def received_line_index(lines: list[dict], entry: dict) -> int | None:
    """The document line a receipt (or return) entry is for, or None when it cannot be told.

    An entry recorded with its line's id names that line wherever it now sits. An older entry
    names its line by position, trusted only while the line there still holds the entry's
    goods; otherwise the one line holding them. Two lines holding them leave it untold.
    """
    line_id = entry.get("source_line_id")
    if line_id:
        found = [i for i, li in enumerate(lines) if li.get("line_id") == line_id]
        return found[0] if len(found) == 1 else None
    index = int(entry.get("po_line_index", -1))
    if 0 <= index < len(lines) and _holds_receipt(lines[index], entry, in_place=True):
        return index
    found = [i for i, li in enumerate(lines) if _holds_receipt(li, entry)]
    return found[0] if len(found) == 1 else None


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
            where = t("acct.field_line", n=n) + (f" ({label})" if label else "")
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_reference",
                    "message": t("documents.err_item_missing", where=where, ident=ident),
                    "item_id": ident,
                    "line": n,
                },
            )
    return items


def out_on_another_memo(state: dict, entity_id: str | None, source_memo_id: str | None) -> bool:
    """A lot out on a memo belongs to that memo until it comes back or is invoiced from it:
    only the memo itself (``entity_id``) or an invoice made from it (``source_memo_id``)
    may take it. A memo_out lot that names no memo has no record that may take it."""
    holder = state.get("status_doc_id")
    return state.get("status") == "memo_out" and (not holder or holder not in (entity_id, source_memo_id))


def shipped_elsewhere(by_doc: dict[str, list[str]]) -> dict:
    """The refusal for goods that went out on other documents: ``by_doc`` maps each
    document number to the lines it shipped. The goods come back only by reverting those
    fulfillments, so whatever wants them (another record's line, a write that drops or
    rebinds the line that shipped them) is told every place they went, each with its own
    lines, and then to set them available there."""
    went = [refusal("lines.went_out_on", f"{', '.join(names)} went out on {doc}.",
                    lines=", ".join(names), doc=doc) for doc, names in by_doc.items()]
    docs = ", ".join(by_doc)
    return refusal("lines.shipped_elsewhere",
                   " ".join(w["message"] for w in went)
                   + f" Revert fulfillment first: set the goods as available on {docs}.",
                   went=went, docs=docs)


def memo_out_refusal(state: dict, sku: str) -> dict:
    """The refusal for a lot out on a memo other than the one this record may take it from,
    naming that memo when the lot carries its number."""
    memo = state.get("status_doc_number")
    if not memo:
        return refusal("documents.lot_on_memo_elsewhere", f"{sku}: out on another memo - take it back there "
                       "or invoice it from that memo", code=sku)
    return refusal("documents.lot_on_memo_by", f"{sku}: out on memo {memo} - take it back there or invoice it "
                   "from that memo", code=sku, doc=memo)


def assert_new_references_eligible(
    items: dict[str, Projection], line_items, *, known: Counter, doc_type: str | None, entity_id: str | None,
    source_memo_id: str | None = None,
) -> None:
    """Refuse a line set that newly references an item the record may not take.

    A draft item is not stock yet, and a deleted one is out of use until restored, so no
    document or List may newly reference either. An
    item reserved by another record, or by a status edit that no record owns, is held,
    and an item out on a memo is that memo's (``out_on_another_memo``: an invoice made
    from the memo, ``source_memo_id``, may take it), so an invoice or memo (which claim
    stock) may not newly reference either; quotations, other documents and Lists may.
    "Newly" counts occurrences: a line beyond the number
    the stored record (``known``, ``line_id_counts``) held for that id is new, so a second
    line for an item the record already lists is judged like a first, while the lines it
    already held stay editable. ``items`` is what ``linked_items`` resolved, read under
    the company lock it takes for any such increase, which every change to an item's
    draft or reserved status also takes.

    422 with one refusal per refused item in ``errors`` and their English text joined in
    ``message``; ``conflicts`` lists the reserved ones with the record holding each, for
    the page to link to.
    """
    errors: list[dict] = []
    conflicts: list[dict] = []
    for ident in sorted(line_id_counts(line_items) - known):
        if ident not in items:
            continue  # linked_items has already judged a line whose item is gone
        state = items[ident].state or {}
        sku = state.get("sku") or ident
        claims_stock = doc_type in DOCUMENT_ITEM_UNIQUE_DOC_TYPES
        if state.get("status") == DELETED:
            errors.append(refusal(
                "lines.item_deleted", f"{sku}: item was deleted - restore it from the Deleted list or pick another item",
                sku=sku))
        elif str(state.get("status") or "").lower() == "draft":
            errors.append(refusal("item.draft", f"{sku}: item is a draft - make it available first", sku=sku))
        elif claims_stock and state.get("status") == "reserved" and state.get("status_doc_id") != entity_id:
            owner = state.get("status_doc_number") or state.get("status_doc_id") or "another document"
            errors.append(refusal("documents.lot_reserved_by", f"{sku}: reserved on {owner} - release it there first",
                                  code=sku, doc=owner))
            conflicts.append({"entity_id": ident, "sku": sku, "doc_id": state.get("status_doc_id"),
                              "doc_number": state.get("status_doc_number"), "message": errors[-1]["message"]})
        elif claims_stock and out_on_another_memo(state, entity_id, source_memo_id):
            errors.append(memo_out_refusal(state, sku))
    if errors:
        raise HTTPException(status_code=422, detail={
            "message": "; ".join(e["message"] for e in errors), "errors": errors, "conflicts": conflicts})


def assert_line_holds_respected(items: dict[str, Projection], line_items, stored_lines, *, entity_id: str | None) -> None:
    """Refuse a line that newly takes a lot this record holds for another of its lines.

    Reserving a line attributes the hold to it (the item's ``status_line_entity_id`` is
    that line's ``line_id``), so the lot is that line's stock: no other line of the record
    may newly reference it. "Newly" counts occurrences per (line_id, item) against the
    stored record, so the holding line stays editable and a reference another line already
    had is carried forward, while moving the reference to a different line is new. A hold
    with no line attribution belongs to the whole record (``assert_new_references_eligible``
    judges holds of other records). ``items`` is what ``linked_items`` resolved.

    422 ``{"errors": [...]}`` with one refusal per refused line.
    """
    def pairs(lines) -> Counter:
        return Counter((line.get("line_id"), line_item_id(line)) for line in lines or []
                       if isinstance(line, dict) and line_item_id(line))

    errors: list[dict] = []
    for line_id, ident in sorted(pairs(line_items) - pairs(stored_lines), key=str):
        state = (items[ident].state or {}) if ident in items else {}
        holder = state.get("status_line_entity_id")
        if (state.get("status") == "reserved" and entity_id and state.get("status_doc_id") == entity_id
                and holder and holder != line_id):
            sku = state.get("sku") or ident
            errors.append(refusal(
                "item.held_for_other_line",
                f"{sku} is held for another line of this record: pick another item or release it there first.",
                sku=sku))
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})


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
                    "message": t("documents.duplicate_item_on_document"),
                    "item_id": ident,
                },
            )


# ---------------------------------------------------------------------------
# Line identity: every document and List line carries a stable ``line_id`` (a UUID
# string) so an action, a hold or a shipment can name one line even when two lines
# share an item. The id is assigned where lines are written (emit_event), never
# regenerated, and unique within its record.
# ---------------------------------------------------------------------------

def is_line_id(value) -> bool:
    """True for a well-formed line id: a UUID, in its 36-character or 32-hex form."""
    if not isinstance(value, str) or len(value) not in (32, 36):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def new_line_id() -> str:
    return str(uuid.uuid4())


def _line_key(line: dict):
    """What a line is about, for carrying a stored id onto an id-less incoming line: its
    linked item, or for a free-text line its description."""
    return line_item_id(line) or ("text", str(line.get("description") or ""))


def normalize_line_ids(line_set, stored_lines) -> None:
    """Give every line of ``line_set`` a line id, in place, and refuse a malformed or
    repeated one (422 ``invalid_line_id`` / ``duplicate_line_id``).

    A line that arrives without an id keeps the id its stored counterpart already had when
    the evidence is unambiguous: the stored line at the same position is about the same
    item (or, for a free-text line, has the same description), or it is the only stored
    line for that item and the only incoming one. Otherwise it is a new line and gets a
    new id. Whether an existing line may be removed, moved or rebound at all is
    ``assert_protected_lines_kept``'s rule, judged after this."""
    seen: set[str] = set()
    for n, line in enumerate(line_set or [], 1):
        if not isinstance(line, dict) or line.get("line_id") in (None, ""):
            continue
        lid = line["line_id"]
        if not is_line_id(lid):
            raise HTTPException(status_code=422, detail=refusal(
                "line.invalid_line_id", f"Line {n} has an invalid line id.", line=n))
        canon = uuid.UUID(lid).hex
        if canon in seen:
            raise HTTPException(status_code=422, detail=refusal(
                "line.duplicate_line_id", f"Line {n} repeats the id of another line.", line=n))
        seen.add(canon)
    missing = [i for i, line in enumerate(line_set or []) if isinstance(line, dict) and not line.get("line_id")]
    if not missing:
        return
    stored = [line for line in stored_lines or [] if isinstance(line, dict)]
    free = {i: line for i, line in enumerate(stored)
            if is_line_id(line.get("line_id")) and uuid.UUID(line["line_id"]).hex not in seen}
    incoming_keys = Counter(_line_key(line_set[i]) for i in missing)
    stored_keys = Counter(_line_key(line) for line in free.values())
    for i in missing:
        line = line_set[i]
        key = _line_key(line)
        pick = None
        if i in free and _line_key(free[i]) == key:
            pick = i
        elif line_item_id(line) and incoming_keys[key] == 1 and stored_keys[key] == 1:
            pick = next(j for j, s in free.items() if _line_key(s) == key)
        if pick is not None:
            line["line_id"] = free.pop(pick)["line_id"]
            stored_keys[key] -= 1
        else:
            line["line_id"] = new_line_id()


def strip_line_ids(line_items) -> list[dict]:
    """Copies of ``line_items`` without their line ids, for a new record built from an old
    one that must not share its line identities (a duplicate)."""
    return [{k: v for k, v in line.items() if k != "line_id"} if isinstance(line, dict) else line
            for line in line_items or []]


# ---------------------------------------------------------------------------
# Protected lines: a line that holds stock for its record, shipped stock for it, or had
# goods received on it is what that stock or receipt points at. Holds name their line by
# id; shipments and receipts recorded before line ids existed name it by position. So no
# write may remove such a line, give it another id or bind it to another item, and a
# shipped or received line also keeps its position. Quantities, prices and descriptions
# stay editable; moving stock or receipts goes through the line actions themselves.
# ---------------------------------------------------------------------------

_PROTECTED_MESSAGES = {
    "held": ("line.protected_held",
             "Line {line} ({sku}) holds reserved stock. Set it as available before removing it or "
             "changing its item."),
    "received": ("line.protected_received",
                 "Line {line} ({sku}) has received goods. Undo or return them before removing, moving "
                 "or changing the line's item or type."),
}


def _counterpart(stored: dict, index: int, line_set: list, stored_ids: set) -> int | None:
    """The position in ``line_set`` of the stored line at ``index``: the line with its id,
    or for an older line without one the line at the same position, which is given its id
    on this write unless that id is another stored line's."""
    lid = stored.get("line_id")
    for n, line in enumerate(line_set):
        if isinstance(line, dict) and lid and line.get("line_id") == lid:
            return n
    if not lid and index < len(line_set) and isinstance(line_set[index], dict) \
            and line_set[index].get("line_id") not in stored_ids:
        return index
    return None


def _untouched(stored_lines: list, line_set: list) -> bool:
    """True when every stored line is still in place with the same id, item and kind:
    nothing any protection is about has changed."""
    from celerp.services.auto_je import bill_line_kind
    if len(line_set) < len(stored_lines):
        return False
    for old, new in zip(stored_lines, line_set):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        if old.get("line_id") != new.get("line_id") or line_item_id(old) != line_item_id(new) \
                or bill_line_kind(old) != bill_line_kind(new):
            return False
    return True


async def _protected_lines(session, company_id, owner_id: str, stored: dict) -> dict[int, tuple[str, set[str]]]:
    """Each protected stored line: its index to (why, the lots it holds or shipped)."""
    from celerp.services.auto_je import doc_line_of_lot
    from celerp.services.pick import attribute_holds

    lines = stored.get("line_items") or []
    out: dict[int, tuple[str, set[str]]] = {}

    def mark(index: int, why: str, lots=()) -> None:
        if 0 <= index < len(lines):
            kind, own = out.get(index, (why, set()))
            # Shipped and received outrank held: they also pin the line's position.
            out[index] = (kind if kind != "held" else why, own | set(lots))

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "item",
        Projection.state["status_doc_id"].as_string() == owner_id,
    ))).scalars().all()
    held = {r.entity_id: r.state for r in rows if (r.state or {}).get("status") == "reserved"}
    by_line, _orphans, ambiguous = attribute_holds(lines, held)
    for index, lots in by_line.items():
        mark(index, "held", lots)
    for eid, indices in ambiguous.items():
        for index in indices:
            mark(index, "held", [eid])
    for r in rows:
        st = r.state or {}
        if st.get("status") not in ("sold", "memo_out"):
            continue
        index = await doc_line_of_lot(session, company_id, owner_id, stored, r.entity_id, st)
        if index is not None:
            mark(index, "shipped", [r.entity_id])
            continue
        # Nothing tells which line shipped it: every line it could belong to stays put.
        sku = str(st.get("sku") or "").strip()
        for n, line in enumerate(lines):
            if line_item_id(line) == r.entity_id or (sku and str(line.get("sku") or "").strip() == sku):
                mark(n, "shipped", [r.entity_id])

    index_of = {li.get("line_id"): n for n, li in enumerate(lines) if isinstance(li, dict) and li.get("line_id")}
    for entry in [*(stored.get("received_items") or []), *(stored.get("returned_items") or [])]:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source_line_id")
        if source:
            index = index_of.get(source)
        else:
            try:
                index = int(entry.get("po_line_index"))
            except (TypeError, ValueError):
                index = None
        if index is not None:
            mark(index, "received")
    return out


async def assert_protected_lines_kept(session, company_id, owner_id: str, stored: dict, line_set) -> None:
    """Refuse a write of ``line_set`` over the stored record ``stored`` that removes, re-ids
    or rebinds a line holding or having shipped stock, or that had goods received, or that
    moves a shipped or received line (409 ``line.protected_held`` / ``lines.shipped_elsewhere``
    / ``line.protected_received``). A line may be rebound only to a lot it holds or shipped itself, or to a
    part split off its own item: that is how reserving and shipping part of a lot, and
    recording a historical delivery, name the lot the line now stands for."""
    from celerp.services.auto_je import bill_line_kind

    stored_lines = [li for li in stored.get("line_items") or [] if isinstance(li, dict)]
    line_set = list(line_set or [])
    if not stored_lines or _untouched(stored_lines, line_set):
        return
    protected = await _protected_lines(session, company_id, owner_id, {**stored, "line_items": stored_lines})
    if not protected:
        return
    stored_ids = {li.get("line_id") for li in stored_lines if li.get("line_id")}
    rebound = {line_item_id(line_set[n]) for index, _ in protected.items()
               if (n := _counterpart(stored_lines[index], index, line_set, stored_ids)) is not None
               and line_item_id(line_set[n]) and line_item_id(line_set[n]) != line_item_id(stored_lines[index])}
    rebound.discard(None)
    parts = {}
    if rebound:
        parts = {r.entity_id: (r.state or {}).get("split_from") for r in (await session.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "item",
            Projection.entity_id.in_(rebound)))).scalars().all()}
    shipped: list[str] = []
    for index in sorted(protected):
        why, lots = protected[index]
        old = stored_lines[index]
        n = _counterpart(old, index, line_set, stored_ids)
        ok = n is not None
        if ok:
            new = line_set[n]
            was, now = line_item_id(old), line_item_id(new)
            if now != was and not (now in lots or (was and parts.get(now) == was)):
                ok = False
            if why != "held" and n != index:
                ok = False
            if why == "received" and bill_line_kind(old) != bill_line_kind(new):
                ok = False
        if not ok:
            sku = str(old.get("sku") or old.get("description") or old.get("name") or "")
            if why == "shipped":
                shipped.append(sku)
                continue
            key, text = _PROTECTED_MESSAGES[why]
            raise HTTPException(status_code=409, detail=refusal(
                key, text.format(line=index + 1, sku=sku), line=index + 1, sku=sku))
    if shipped:
        doc = str(stored.get("doc_number") or stored.get("ref_id") or owner_id)
        raise HTTPException(status_code=409, detail=shipped_elsewhere({doc: shipped}))
