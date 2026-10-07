# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from celerp.events.schemas import EVENT_SCHEMA_MAP
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ITEM_BIRTHS, ProjectionEngine
from celerp.services.document_lines import (
    assert_document_item_uniqueness,
    assert_new_references_eligible,
    line_id_counts,
    linked_items,
)
from celerp.services.business_time import business_date_of


def apply_event(state: dict, event: LedgerEntry) -> dict:
    return ProjectionEngine._apply(state, event.event_type, event.data)


STRIPE_OWNED_PAYMENT = (
    "This payment was received through Stripe, so it can only be refunded or reversed in Stripe."
)
STRIPE_RECEIPT_KEPT = (
    "This payment was received through Stripe, so it was real and cannot be deleted. Void or refund it instead."
)
PAYMENT_NOT_NAMED = "A payment can be taken off a document only by naming it, and a deletion keeps its place."
# Every event that takes a received payment back off a document.
PAYMENT_REMOVAL_EVENTS = frozenset({"doc.payment.voided", "doc.payment.deleted", "doc.payment.refunded"})


async def _stripe_receipts(session, company_id, entity_id) -> list[dict]:
    """The ``doc.payment.received`` events the Stripe intake wrote on this document.
    The ledger records which writer received each payment; the method is free text
    that a connector or a person can also set to "stripe"."""
    return list((await session.execute(
        select(LedgerEntry.data).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == entity_id,
            LedgerEntry.event_type == "doc.payment.received",
            LedgerEntry.source == "stripe",
        )
    )).scalars().all())


async def stripe_receipt_references(session, company_id, entity_id, *, managed: bool = False) -> set[str]:
    """References (Stripe PaymentIntents) of the payments on this document received
    through Stripe: each is real for good, managed or not, linked to Stripe or not, so
    it is never deleted. With *managed*, only those the intake recorded as Stripe's
    to manage: paid on a page that carried the books it is recorded on
    (``stripe_managed``). A payment taken before payment pages carried their books is
    the company's to manage, like any other.

    A payment is found by its reference, never by its index: a deletion made before
    deletions kept their place renumbered the payments after it, so the index a
    receipt was recorded at can since belong to another payment."""
    return {data["reference"] for data in await _stripe_receipts(session, company_id, entity_id)
            if data.get("reference") and (not managed or data.get("stripe_managed") is True)}


def is_stripe_receipt(payment: dict, references: set[str]) -> bool:
    """Whether *payment* is one of the Stripe receipts *references* names."""
    return payment.get("method") == "stripe" and payment.get("reference") in references


async def stripe_payment_indexes(session, company_id, entity_id, payments: list[dict]) -> set[int]:
    """Indexes of the payments on this document that Stripe manages
    (``stripe_receipt_references``) and that are still linked to Stripe.

    Stripe holds the money for these, so only Stripe can give it back. Once Stripe is
    disconnected a payment is no longer linked to it (``stripe_released_at``) and is
    refunded or voided here like any other.
    """
    if not any(p.get("method") == "stripe" for p in payments):
        return set()
    managed = await stripe_receipt_references(session, company_id, entity_id, managed=True)
    return {p.get("index") for p in payments if is_stripe_receipt(p, managed) and not p.get("stripe_released_at")}


async def refuse_stripe_payment_removal(session, company_id, entity_id, payments: list[dict],
                                        index, event_type: str) -> None:
    """422 when *event_type* would take the payment at *index* off the document while
    Stripe holds its money (``stripe_payment_indexes``), or would delete a payment
    received through Stripe (``stripe_receipt_references``). A payment a person
    recorded on the document from the unmatched payments, and that was never
    refunded, may be deleted: that puts it back with them (``payments.return_unmatched``)."""
    payment = next((p for p in payments if p.get("index") == index), None)
    if event_type == "doc.payment.deleted" and payment is not None and not payment.get("refunded"):
        from celerp.services.payments import recorded_unmatched
        if payment.get("reference") in await recorded_unmatched(session, company_id, entity_id):
            return
    if index in await stripe_payment_indexes(session, company_id, entity_id, payments):
        raise HTTPException(status_code=422, detail=STRIPE_OWNED_PAYMENT)
    if (event_type == "doc.payment.deleted" and payment is not None
            and is_stripe_receipt(payment, await stripe_receipt_references(session, company_id, entity_id))):
        raise HTTPException(status_code=422, detail=STRIPE_RECEIPT_KEPT)


async def _refuse_stripe_payment_removal(session, kwargs: dict) -> None:
    """``refuse_stripe_payment_removal`` for every writer, except a refund Stripe
    itself reports (``payments.receive_refund``)."""
    if kwargs.get("event_type") == "doc.payment.refunded" and kwargs.get("source") == "stripe":
        return
    row = await session.get(Projection, (kwargs.get("company_id"), kwargs.get("entity_id")))
    if row is None or row.entity_type != "doc":
        return
    # A refund naming no payment, or a deletion by list position (the shape deletions
    # had before they kept their place), cannot be checked against the payment it
    # takes off; no writer may emit either.
    data = kwargs.get("data") or {}
    if data.get("payment_index") is None or (kwargs["event_type"] == "doc.payment.deleted"
                                             and data.get("tombstone") is not True):
        raise HTTPException(status_code=422, detail=PAYMENT_NOT_NAMED)
    await refuse_stripe_payment_removal(session, kwargs["company_id"], kwargs["entity_id"],
                                        (row.state or {}).get("payments", []),
                                        data["payment_index"], kwargs["event_type"])


async def find_event_by_idempotency(session, company_id, idempotency_key: str | None) -> LedgerEntry | None:
    """Return the event already committed for this company/key, if any.

    Idempotency belongs to the canonical ledger, not to individual callers.
    Routes use this before allocating numbers/codes or repeating secondary work;
    ``emit_event`` remains the unique-constraint race backstop.
    """
    if not idempotency_key:
        return None
    return (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key == idempotency_key,
        ).limit(1)
    )).scalars().first()


def write_period_lock(company, lock_date: str | None, user_id) -> None:
    """Lock *company*'s books through the ISO *lock_date*, recorded as set by *user_id*,
    or unlock them when *lock_date* is None. The caller holds the company row and commits."""
    settings = dict(company.settings or {})
    if lock_date:
        settings["lock_date"] = lock_date
        settings["lock_date_set_by"] = str(user_id)
        settings["lock_date_set_at"] = datetime.now(timezone.utc).isoformat()
    else:
        for key in ("lock_date", "lock_date_set_by", "lock_date_set_at"):
            settings.pop(key, None)
    company.settings = settings


async def _check_period_lock(session, company_id, data: dict) -> None:
    """Reject events whose effective date falls within a locked period."""
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    if not company:
        return
    lock_date_str = (company.settings or {}).get("lock_date")
    if not lock_date_str:
        return
    try:
        lock_date = date.fromisoformat(lock_date_str)
    except (ValueError, TypeError):
        return
    event_date_str = data.get("ts") or data.get("issue_date") or data.get("date")
    try:
        event_date = date.fromisoformat(business_date_of(event_date_str, (company.settings or {}).get("timezone")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if event_date <= lock_date:
        raise HTTPException(
            status_code=422,
            detail=f"Period is locked through {lock_date_str}. Unlock in Settings > Accounting to modify past transactions.",
        )


class ConnectorIdentityConflict(Exception):
    """A platform record resolves to two different Celerp records."""


async def _connector_entity_id(
    session, company_id, entity_type: str, idem_key: str,
    external_identity: tuple[str, str] | None = None,
) -> tuple[str | None, bool]:
    """The entity_id of an existing projection this connector record maps to (or None),
    and whether it was found only by ``external_identity``.

    Records are resolved by their stable ``idempotency_key`` (stored in projection
    state), not a freshly-minted entity_id — so a re-import updates the SAME projection
    instead of duplicating it. This includes records imported before the deterministic
    -id scheme, which stored a random-uuid entity_id: the backfill migration
    (`e4f5a6b7c8d9`) stamps ``idempotency_key`` onto those rows so they resolve here too.

    ``external_identity`` is ``(field, value)``: the platform id as a Celerp record
    carries it once Celerp itself created the record on the platform. A record found
    only that way is Celerp's own; one found by each route, differently, is a
    conflict and nothing is written.
    """
    import uuid as _uuid

    scope = (
        Projection.company_id == _uuid.UUID(str(company_id)),
        Projection.entity_type == entity_type,
    )
    by_key = (await session.execute(
        select(Projection.entity_id)
        .where(*scope, Projection.state["idempotency_key"].as_string() == idem_key)
        .limit(1)
    )).scalar()
    if external_identity is None:
        return by_key, False
    field, value = external_identity
    by_external = (await session.execute(
        select(Projection.entity_id)
        .where(*scope, Projection.state[field].as_string() == value)
        .limit(2)
    )).scalars().all()
    ids = {i for i in (by_key, *by_external) if i is not None}
    if len(ids) > 1:
        raise ConnectorIdentityConflict(
            f"{field} {value} belongs to more than one record: {', '.join(sorted(ids))}"
        )
    return next(iter(ids), None), by_key is None and bool(by_external)


async def connector_upsert(
    session, *, company_id, entity_type: str, event_type: str, idem_key: str, data: dict,
    external_identity: tuple[str, str] | None = None, on_create: dict | None = None,
    update=None,
) -> str:
    """Create-or-update a projection from a connector payload.

    Returns "created" (new projection), "updated" (existing projection, changed
    content), or "noop" (this exact content was already applied, or the record is one
    Celerp created on the platform, found only by ``external_identity``: Celerp owns
    it, so nothing is written). Raises ConnectorIdentityConflict when
    ``external_identity`` and ``idem_key`` resolve to different projections.

    ``idem_key`` (the stable platform id) is stored in projection state so a re-import
    resolves the SAME projection; the event's idempotency key varies with the content,
    so an unchanged re-import dedups (no-op) while a changed one updates. ``on_create``
    holds fields written only when the record is created (an item's starting status);
    they are not part of the content, so a re-import never writes them again.

    ``update`` applies a changed re-import to the existing projection the way an edit
    would: ``await update(session, entity_id, data, event_idem)`` writes the change
    under ``event_idem`` and returns False when nothing differs. Without it the
    caller's ``event_type`` is emitted for the existing projection too.
    """
    import hashlib
    import json as _json

    existing_id, external_only = await _connector_entity_id(
        session, company_id, entity_type, idem_key, external_identity
    )
    if external_only:
        return "noop"

    data = {**data, "idempotency_key": idem_key}  # stable identity in state (rebuild-safe)
    entity_id = existing_id or f"{entity_type}:{idem_key}"

    content = _json.dumps(
        {k: v for k, v in data.items() if k != "idempotency_key"}, sort_keys=True, default=str
    )
    event_idem = f"{idem_key}:{hashlib.sha1(content.encode()).hexdigest()[:12]}"

    seen = (await session.execute(
        text("SELECT id FROM ledger WHERE company_id = CAST(:cid AS uuid) AND idempotency_key=:k"),
        {"cid": str(company_id), "k": event_idem},
    )).first()
    if seen:
        return "noop"
    if existing_id and update is not None:
        return "updated" if await update(session, existing_id, data, event_idem) else "noop"
    if not existing_id and on_create:
        data = {**on_create, **data}

    await emit_event(
        session,
        # A platform's codes are recorded as the platform holds them; a duplicate is
        # reported by the resolver and Doctor rather than failing the sync.
        preserve_external_code_conflicts=entity_type == "item",
        company_id=company_id,
        entity_id=entity_id,
        entity_type=entity_type,
        event_type=event_type,
        data=data,
        actor_id=None,
        location_id=None,
        source="connector",
        idempotency_key=event_idem,
        metadata_={},
    )
    return "updated" if existing_id else "created"


def _touches_physical_codes(state: dict, event_type: str, data: dict) -> bool:
    """True when an item event can change which physical codes the item resolves by.

    That is any event setting a barcode or RFID / EPC, by data shape, or any event
    that moves an item out of a status excluded from resolution (merged back to
    available, say), probed by applying the event to the item in an excluded status.
    A probe that cannot be evaluated counts as touching, so the check still runs.
    """
    from celerp.inventory_codes import PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES
    from celerp.services.physical_codes import PHYSICAL_CODE_FIELDS

    changed = data.get("fields_changed")
    if any(
        field in data or (isinstance(changed, dict) and field in changed)
        for field in PHYSICAL_CODE_FIELDS
    ):
        return True
    # A status change on a live item can only drop its codes from resolution, never
    # add one, so only an item in an excluded status needs the probe (and the lock).
    if str(state.get("status") or "").lower() not in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
        return False
    try:
        probe = ProjectionEngine._apply(dict(state), event_type, data)
    except Exception:
        return True
    return str(probe.get("status") or "").lower() not in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES


async def _record_lot_account(session, kwargs: dict, previous_state: dict | None) -> None:
    """A new lot records the inventory account its value is booked into when its writer
    books it: a receipt or a production run names the account it debits, a part of a lot
    keeps the lot's, and stock entered with no purchase behind it takes the opening
    inventory account when it is booked as opening stock (lot_origin.draft_boundary,
    lot_origin.recognize_opening_lots). Stock a migration brings in keeps the account its
    source books held it in. Nothing else is guessed: a snapshot of another system's item
    records none until its stock is placed. No later event may change it: the lot's value
    stays on that account for as long as the lot holds stock."""
    from celerp.accounting_roles import LOT_ACCOUNT_FIELD
    from celerp.services.account_roles import source_lot_account

    data = kwargs["data"]
    if kwargs["event_type"] in ITEM_BIRTHS and previous_state is None:
        if (kwargs["event_type"] == "item.created" and kwargs.get("source") == "migration"
                and LOT_ACCOUNT_FIELD not in data and str(data.get("status") or "").lower() != "draft"):
            code = await source_lot_account(session, kwargs["company_id"])
            if code:
                data[LOT_ACCOUNT_FIELD] = code
        return
    current = (previous_state or {}).get(LOT_ACCOUNT_FIELD)
    if kwargs["event_type"] == "item.inventory_account.recorded":
        # An older lot that recorded none takes the one the upgrade or the user places it on.
        if current:
            raise HTTPException(status_code=409, detail="This stock already records its inventory account.")
        return
    changed = data.get("fields_changed")
    if LOT_ACCOUNT_FIELD in data:
        written = data[LOT_ACCOUNT_FIELD]
    elif isinstance(changed, dict) and LOT_ACCOUNT_FIELD in changed:
        change = changed[LOT_ACCOUNT_FIELD]
        written = change.get("new") if isinstance(change, dict) else change
    else:
        return
    if written != current:
        raise HTTPException(
            status_code=422,
            detail="An item's inventory account is recorded when its stock is booked and cannot be changed.",
        )


# Item events whose writer may say an archived or expired lot keeps its stock on the
# books: Archive, Expire, and the upgrade that recognizes what older releases archived.
_ON_BOOKS_WRITERS = frozenset({"item.status.set", "item.expired", "item.updated", "item.inventory_on_books.recorded"})


def _guard_on_books(kwargs: dict) -> None:
    """Whether retired stock stays on the books follows from the action that retired it
    (projections._keep_on_books); nothing may enter it, an import or a field edit least
    of all."""
    from celerp.accounting_roles import ON_BOOKS_FIELD

    data = kwargs["data"]
    changed = data.get("fields_changed")
    if (ON_BOOKS_FIELD in data and kwargs["event_type"] not in _ON_BOOKS_WRITERS) or (
            isinstance(changed, dict) and ON_BOOKS_FIELD in changed):
        raise HTTPException(
            status_code=422,
            detail="Whether archived or expired stock stays on the books is set by Archive and Expire "
                   "and cannot be entered.",
        )


async def _item_applied(session, entry: LedgerEntry, transition) -> None:
    """Checks and effects of one live item event, on the state its row lock applied it to.

    Births (item.created, item.snapshot) and changes are exclusive. A birth must find no
    item: one that finds an item would overwrite it wholesale, bypassing every check an
    edit carries. Any other change must find one: a change that finds none read an item
    that was removed before it committed (a deleted draft, an undone import) or names
    one that never existed, and writing it would make an item out of the change alone.
    Replay applies events through ProjectionEngine directly and is not affected."""
    from celerp.connectors.outbound_queue import enqueue_item_change
    from celerp.modules.slots import get as get_slot, resolve_handler
    from celerp.services.lot_origin import (
        assert_draft_not_circulated,
        book_draft_boundary,
        book_value_change,
        draft_boundary,
        value_boundary,
    )

    if transition.before is None and entry.event_type not in ITEM_BIRTHS:
        raise HTTPException(status_code=404, detail="Item not found")
    if transition.before is not None and entry.event_type in ITEM_BIRTHS:
        raise HTTPException(status_code=409, detail="This item already exists.")
    assert_draft_not_circulated(entry.event_type, transition)
    # Modules hold an item to what its lineage may still take (a production run's open
    # output, for one); a handler that cannot be resolved fails the event, never skips it.
    for handler in sorted({c["handler"] for c in get_slot("item_lineage_guard")}):
        await resolve_handler(handler)(session=session, entry=entry, transition=transition)
    draft_move = await draft_boundary(session, entry, transition)
    if draft_move is not None:
        await book_draft_boundary(session, entry, draft_move)
    value_change = await value_boundary(session, entry, transition)
    if value_change is not None:
        await book_value_change(session, entry, value_change)
    # Durable connector work belongs to the same savepoint as the item event, so a
    # caller that catches a failure here keeps neither. No network I/O occurs here;
    # the worker re-reads current state before sending. ``outbound_queued`` tells
    # the caller the event will reach a connected store.
    entry.outbound_queued = await enqueue_item_change(session, entry, previous_state=transition.before)
    await session.flush()


async def emit_event(
    session, *, preserve_external_code_conflicts: bool = False, **kwargs
) -> LedgerEntry:
    # A backup represents a clean point in time: while one is building, pause writes
    # (reads, which never emit, are unaffected) so nothing changes mid-backup.
    from celerp.services.backup_state import is_active as _backup_active
    if _backup_active():
        raise HTTPException(status_code=503, detail="Backup in progress, try again shortly.")

    schema = EVENT_SCHEMA_MAP.get(kwargs["event_type"])
    if schema is None:
        raise ValueError(f"Unknown event_type: {kwargs['event_type']}")

    schema(**kwargs["data"])

    # The schema validates a Pydantic-made copy, so any code canonicalization it applies
    # (rfid_epc -> trimmed upper-case, gtin -> validated form) never reaches the raw dict
    # persisted below. Apply the same normalization to the dict that is actually stored, so
    # the stored value equals what the availability check and the partial unique index
    # compare against - otherwise a case-variant identifier is stored raw and never collides.
    _normalize = getattr(schema, "normalize_for_storage", None)
    if _normalize is not None:
        _normalize(kwargs["data"])

    # Enforce period lock
    await _check_period_lock(session, kwargs.get("company_id"), kwargs.get("data", {}))

    # Every journal line, whoever produced it, passes the one account boundary.
    if kwargs["event_type"] == "acc.journal_entry.created":
        from celerp.services.journal_accounts import prepare_journal_entry

        await prepare_journal_entry(session, kwargs.get("company_id"), kwargs["data"])

    # Enforce the line rules on every document and List write. Extract the post-change
    # line set by DATA SHAPE so every writer (create, patch, shared_import, update,
    # conversion, import) is covered by one rule, keyed on the entity type rather than
    # an event list:
    #   - every line a write adds must link to a real item of this company (a stale form
    #     or an import can carry the id of an item Undo removed); lines already on the
    #     stored document are carried forward, so an old document stays editable;
    #   - no line a write adds (counted per occurrence) may reference a draft item, and
    #     none on an invoice or memo may reference an item reserved elsewhere;
    #   - an OUTBOUND document (invoice, memo) never repeats a physical item; the
    #     doc-type scope lives in assert_document_item_uniqueness beside the invariant.
    # Rebuild/replay applies events via apply_event, never emit_event, so historical
    # events are never re-validated.
    if kwargs.get("entity_type") in ("doc", "list"):
        data = kwargs.get("data") or {}
        line_set = None
        if isinstance(data.get("line_items"), list):
            line_set = data["line_items"]
        elif isinstance(data.get("fields_changed"), dict):
            changed = data["fields_changed"].get("line_items")
            if isinstance(changed, dict):
                line_set = changed.get("new")
        if line_set is not None:
            # The stored record, read only when it is of the same type (the Projection PK
            # is (company_id, entity_id) with no type discriminator).
            proj = await session.get(
                Projection, (kwargs.get("company_id"), kwargs.get("entity_id"))
            )
            same = proj is not None and proj.entity_type == kwargs.get("entity_type")
            stored = (proj.state or {}) if same else {}
            known = line_id_counts(stored.get("line_items"))
            items = await linked_items(session, kwargs.get("company_id"), line_set, known=known)
            # Prefer the event's own doc_type; otherwise the stored document's. A List has
            # none, so the invoice/memo rules never apply to it.
            doc_type = data.get("doc_type") or stored.get("doc_type")
            assert_new_references_eligible(
                items, line_set, known=known, doc_type=doc_type, entity_id=kwargs.get("entity_id"),
            )
            await assert_document_item_uniqueness(
                session, kwargs.get("company_id"), doc_type, line_set
            )

    if kwargs.get("entity_type") == "doc" and kwargs.get("event_type") in PAYMENT_REMOVAL_EVENTS:
        await _refuse_stripe_payment_removal(session, kwargs)

    if kwargs.get("event_type") in {"shop.sync.enabled", "shop.sync.disabled"}:
        from celerp.connectors.ownership import lock_connector_key

        await lock_connector_key(session, "shopify")

    # Physical codes (barcode, RFID / EPC) are unique per company among the codes a write
    # INTRODUCES: the item's code set after the event is compared with its set before, so
    # a duplicate already in the data never blocks an unrelated edit. An exact replay is
    # left to the idempotency dedup below. Imports and connectors pass
    # preserve_external_code_conflicts to record the source system's codes as given;
    # the resolver then reports the ambiguity and Doctor lists it.
    previous_item_state = None
    if kwargs.get("entity_type") == "item":
        from copy import deepcopy

        from celerp.services.physical_codes import (
            assert_new_physical_codes_available,
            lock_item_code_namespace,
        )

        key = (kwargs["company_id"], kwargs["entity_id"])
        previous = await session.get(Projection, key, populate_existing=True)
        if previous is not None and previous.entity_type == "item":
            previous_item_state = deepcopy(previous.state or {})
        # A lot's goods cost is never negative, whichever writer sets it. Writers refuse
        # it first in their own response shape; this is the backstop for the rest.
        from celerp.services.goods_cost import event_goods_costs, lot_label, negative_cost_error

        refusal = negative_cost_error(
            lot_label({**(previous_item_state or {}), **kwargs["data"]}, kwargs["entity_id"]),
            *event_goods_costs(kwargs["event_type"], kwargs["data"]),
        )
        if refusal:
            raise HTTPException(status_code=422, detail=refusal)
        check_codes = _touches_physical_codes(
            previous_item_state or {}, kwargs["event_type"], kwargs["data"]
        ) and await find_event_by_idempotency(
            session, kwargs["company_id"], kwargs.get("idempotency_key")
        ) is None
        if check_codes:
            await lock_item_code_namespace(session, kwargs["company_id"])
            # Under the lock the check must see the committed row, not the copy the
            # identity map held from before another writer changed it.
            previous = await session.get(Projection, key, populate_existing=True)
            previous_item_state = (
                deepcopy(previous.state or {})
                if previous is not None and previous.entity_type == "item"
                else None
            )
        if check_codes and not preserve_external_code_conflicts:
            after = ProjectionEngine._apply(
                previous_item_state or {}, kwargs["event_type"], kwargs["data"]
            )
            await assert_new_physical_codes_available(
                session, kwargs["company_id"], kwargs["entity_id"], previous_item_state, after
            )

    item = kwargs.get("entity_type") == "item"
    if item:
        _guard_on_books(kwargs)
        await _record_lot_account(session, kwargs, previous_item_state)

    entry = LedgerEntry(**kwargs)

    # The event and its effect on the projection are one SAVEPOINT: a refused projection
    # change (a change to an item that is gone, or one the draft rule refuses on the state
    # its row lock applied it to) takes its ledger row with it, even when the caller
    # catches the refusal and commits the rest of its work. A duplicate-idempotency
    # collision likewise rolls back only this insert, NOT the caller's whole transaction.
    # (A bare session.rollback() here would silently undo everything the caller already
    # emitted, e.g. the doc.finalized event before its auto-JE.)
    savepoint = await session.begin_nested()
    try:
        session.add(entry)
        await session.flush()
    except BaseException as exc:
        await savepoint.rollback()
        if not isinstance(exc, IntegrityError):
            raise
        # Idempotency is per-company, so dedup within this company only.
        row = (
            await session.execute(
                text("SELECT id FROM ledger WHERE company_id = CAST(:cid AS uuid) AND idempotency_key=:k"),
                {"cid": str(kwargs["company_id"]), "k": kwargs["idempotency_key"]},
            )
        ).first()
        if row is None:
            raise
        original = await session.get(LedgerEntry, row[0])
        # Callers that must distinguish a replay from a fresh insert (e.g. to
        # reject a stale form resubmitted with edited values) read this flag
        # instead of inferring from entity ids.
        original.was_deduped = True
        return original
    try:
        transition = await ProjectionEngine.apply_event(session, entry)
        if item:
            await _item_applied(session, entry, transition)
    except BaseException:
        await savepoint.rollback()
        raise
    await savepoint.commit()

    # Notify listeners (LISTEN/NOTIFY) that an event landed.
    try:
        await session.execute(text("SELECT pg_notify('events', :payload)"), {"payload": str(entry.id)})
    except Exception:
        # Deterministic: do not fail event emission due to notification issues.
        pass

    return entry
