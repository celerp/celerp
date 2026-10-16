# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Apply peer documents through the canonical import, lifecycle and stock services."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.peer_identity import (
    claim_peer_revision, commit_peer_revision, peer_identity,
)

# The source may supply only document inputs. Payments, receipts and fulfilment arrive as
# the source's own operations and are replayed through the canonical services; no posting,
# attachment, or destination-owned field can cross this path.
_FIELDS = frozenset({
    "doc_type", "ref_id", "status", "contact_id", "contact_name",
    "line_items", "currency", "conversion_rate", "issue_date", "due_date",
    "payment_terms", "notes", "reference", "doc_taxes", "discount",
    "shipping", "subtotal", "total", "tax", "terms_text", "customer_note",
    "purchase_kind", "discount_type", "payments", "amount_paid", "payment_events",
    "fulfilled_items", "stock_receipts",
})
_SOURCE_ITEM_KEYS = ("item_id", "entity_id")
# What a source receipt line may carry into the receiving company's receipt.
_RECEIPT_KEYS = ("po_line_index", "item_id", "quantity_received", "condition", "sku", "name",
                 "cost_price", "receive_as", "category", "weight", "pieces")
# Issued document types and the stock operation each replays.
_RECEIVING_TYPES = frozenset({"bill", "purchase_order"})
_FULFILLING_TYPES = frozenset({"invoice"})
# Issued documents of other types (credit notes, memos, returns, consignments) and other
# statuses are refused with this reason, which the sender shows as needing attention.
_BY_HAND = "{} is not copied automatically. Enter it in the accountant's copy by hand."
_ISSUED_STATUSES = frozenset({
    "final", "sent", "partial", "paid", "awaiting_payment", "received", "partially_received",
})


def requires_stock_movement(data: dict) -> bool:
    """An issued document needing stock operations cannot be copied as a GL-only update."""
    from celerp.services.units import is_non_stock_line

    if (data.get("received_items") or data.get("received_item_ids")
            or data.get("return_received_items")
            or data.get("fulfillment_status") in ("fulfilled", "partial")):
        return True
    lines = data.get("line_items") or []
    if not isinstance(lines, list):
        return True
    for line in lines:
        if not isinstance(line, dict):
            return True
        if (line.get("item_id") or line.get("sku")
                or str(line.get("entity_id") or "").startswith("item:")):
            if not is_non_stock_line(line.get("inventory_type"), line.get("sell_by")):
                return True
    return False


async def _map_identity(session, grant, entity_type: str, source_id: str) -> str:
    mapped = await peer_identity(
        session, target_company=grant.company_id,
        source_company=grant.source_company_id,
        entity_type=entity_type, source_entity_id=source_id)
    if mapped is None:
        raise HTTPException(409, "Source document dependency is not synchronized")
    return mapped


async def receive_document(
    session: AsyncSession, *, grant, source_entity_id: str,
    source_revision: int, data: dict,
) -> str:
    """Create, issue, settle and move the stock of one document the way the source did,
    through the canonical services, without bypassing their status and money checks."""
    from celerp_docs.doc_constants import LIFECYCLE_OWNED_FIELDS
    from celerp_docs.import_service import import_doc_records
    from celerp_docs.routes import DocImportRecord, LineItem, _reject_line_attributes, finalize_document
    from celerp_docs.sequences import require_doc_type

    if not isinstance(data, dict):
        raise HTTPException(422, "Invalid document")
    if set(data) - _FIELDS or set(data) & (
            LIFECYCLE_OWNED_FIELDS - {"status", "payments", "amount_paid", "fulfilled_items"}):
        raise HTTPException(422, "Unapproved document fields")
    if (data.get("doc_type") is None or not isinstance(data.get("line_items", []), list)
            or len(data.get("line_items", [])) > 200):
        raise HTTPException(422, "Invalid document")
    if len(json.dumps(data, separators=(",", ":"))) > 1_000_000:
        raise HTTPException(413, "Document exceeds integration size limit")
    try:
        require_doc_type(data["doc_type"])
    except (ValueError, TypeError):
        raise HTTPException(422, "Unsupported document type") from None
    from celerp.services.peer_payments import (
        validate_source_payments, validate_source_refunds, apply_source_payments,
    )
    refunds = validate_source_refunds(data)
    payments = validate_source_payments(data)
    receipts, fulfilled = _validate_stock(data)
    if data.get("status") == "void" and (data.get("payments") or data.get("payment_events")):
        raise HTTPException(409, "Previously settled void requires payment reconciliation")
    claim = await claim_peer_revision(
        session, grant=grant, entity_type="doc",
        source_entity_id=source_entity_id, source_revision=source_revision,
        data=data)
    if claim.replay:
        return "skipped"
    source_status = data.get("status", "draft")
    if source_status not in _ISSUED_STATUSES | {"draft", "void"}:
        raise HTTPException(409, _BY_HAND.format(f"A {source_status} document"))
    if source_status != "draft" and data["doc_type"] not in _RECEIVING_TYPES | _FULFILLING_TYPES:
        raise HTTPException(409, _BY_HAND.format(
            "An issued " + str(data["doc_type"]).replace("_", " ")))
    if (receipts and data["doc_type"] not in _RECEIVING_TYPES) or (
            fulfilled and data["doc_type"] not in _FULFILLING_TYPES):
        raise HTTPException(422, "Stock operations do not match the document type")
    if source_status == "sent" and data["doc_type"] != "invoice":
        raise HTTPException(409, "Only invoices support a sent document transition")

    incoming = dict(data)
    incoming.pop("payments", None)
    incoming.pop("amount_paid", None)
    incoming.pop("payment_events", None)
    incoming.pop("fulfilled_items", None)
    incoming.pop("stock_receipts", None)
    # Settlement and stock movement belong to separate domain operations.
    incoming["status"] = "draft" if source_status == "draft" else "final"
    if source_status != "draft" and data["doc_type"] == "bill":
        incoming["import_treatment"] = "record_now"
    source_contact = incoming.get("contact_id")
    if source_contact:
        if not isinstance(source_contact, str):
            raise HTTPException(422, "Invalid contact identity")
        incoming["contact_id"] = await _map_identity(session, grant, "contact", source_contact)
    # A line names the parcel its receipt created; until that receipt is replayed here the
    # parcel does not exist, and the replayed receipt names the receiving company's own.
    parcels = {str(x) for receipt in receipts for x in receipt.get("created_item_ids") or []}
    mapped_lines = []
    for line in incoming.get("line_items", []):
        if not isinstance(line, dict):
            raise HTTPException(422, "Invalid document line")
        moved = dict(line)
        if moved.get("entity_id") in parcels and await peer_identity(
                session, target_company=grant.company_id, source_company=grant.source_company_id,
                entity_type="item", source_entity_id=moved["entity_id"]) is None:
            moved.pop("entity_id")
        for key in _SOURCE_ITEM_KEYS:
            if moved.get(key):
                if not isinstance(moved[key], str):
                    raise HTTPException(422, "Invalid item reference")
                if key == "item_id" or moved[key].startswith("item:"):
                    moved[key] = await _map_identity(session, grant, "item", moved[key])
        try:
            LineItem.model_validate(moved)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid document line") from exc
        mapped_lines.append(moved)
    if "line_items" in incoming:
        _reject_line_attributes(mapped_lines)
        incoming["line_items"] = mapped_lines

    company = await session.get(Company, grant.company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(403, "Company unavailable")
    member = (await session.execute(select(User.id, UserCompany.role).join(
        UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == grant.company_id,
        UserCompany.role == "owner", UserCompany.is_active.is_(True),
        User.is_active.is_(True),
    ).order_by(User.id).limit(1))).first()
    if member is None:
        raise HTTPException(403, "Company owner unavailable")
    key = hashlib.sha256(source_entity_id.encode("utf-8")).hexdigest()
    record = DocImportRecord(
        entity_id=claim.target_id, event_type="doc.created",
        data=incoming, source="connector",
        idempotency_key=f"peer:doc:{grant.source_company_id}:{key}",
    )
    from celerp.models.projections import Projection
    target = await session.get(Projection, (grant.company_id, claim.target_id))
    target_status = (target.state or {}).get("status") if target is not None else None
    if target is not None and "line_items" in incoming:
        _keep_receipt_fields(incoming["line_items"], target.state or {}, parcels)
    if source_status == "void":
        from celerp.services.money import round_money
        from celerp_docs.routes import DocVoidBody, void_document
        if target is None or target.entity_type != "doc" or claim.mapping is None:
            raise HTTPException(409, "Source document must be synchronized before void")
        state = target.state or {}
        if state.get("doc_type") != data["doc_type"] or state.get("currency") != data.get("currency"):
            raise HTTPException(409, "Receiving document does not match voided source")
        currency = state.get("currency") or "USD"
        if ("total" in data and round_money(data["total"], currency)
                != round_money(state.get("total") or 0, currency)):
            raise HTTPException(409, "Document total must be reconciled before void")
        if target_status == "draft":
            raise HTTPException(409, "Receiving document must be issued before void")
        if target_status != "void":
            await void_document(
                claim.target_id,
                DocVoidBody(reason="Synchronized source void", idempotency_key=(
                    f"peer:doc-void:{grant.source_company_id}:{key}:{source_revision}")),
                str(grant.company_id), SimpleNamespace(id=member.id), session, commit=False,
            )
        await commit_peer_revision(
            session, grant=grant, claim=claim, entity_type="doc",
            source_entity_id=source_entity_id, source_revision=source_revision)
        return "skipped" if target_status == "void" else "updated"
    if source_status == "draft" and target_status not in (None, "draft"):
        raise HTTPException(409, "Previously issued document requires reconciliation")
    if source_status != "draft" and target_status == "draft":
        # Update its draft fields before the canonical finalization posts the books.
        incoming["status"] = "draft"
        incoming.pop("import_treatment", None)
        record.data = incoming
    outcome = await import_doc_records(
        session, grant.company_id, SimpleNamespace(id=member.id), member.role,
        dict(company.settings or {}), [record], upsert=True, post_ledger=True)
    if len(outcome.records) != 1 or outcome.records[0].status not in (
        "created", "updated", "skipped",
    ):
        raise HTTPException(409, "Receiving document needs review")
    if source_status != "draft" and target_status == "draft":
        await finalize_document(claim.target_id, str(grant.company_id),
                                SimpleNamespace(id=member.id), session, commit=False)
    if source_status == "sent" and target_status != "sent":
        from celerp_docs.routes import DocSendBody, mark_doc_sent
        sent_key = f"peer:doc-sent:{grant.source_company_id}:{key}:{source_revision}"
        await mark_doc_sent(
            session, str(grant.company_id), claim.target_id,
            SimpleNamespace(id=member.id),
            DocSendBody(idempotency_key=sent_key), source="connector",
        )
    if receipts or (target is not None and (target.state or {}).get("received_items")):
        await _replay_receipts(session, grant, claim.target_id, receipts, member.id,
                               dict(company.settings or {}))
    if fulfilled or (target is not None and (target.state or {}).get("fulfilled_items")):
        await _replay_fulfilment(session, grant, claim.target_id, fulfilled, member.id)
    await apply_source_payments(
        session, grant=grant, source_entity_id=source_entity_id,
        target_doc_id=claim.target_id, payments=payments,
        actor_id=member.id, source_status=source_status, refunds=refunds,
        expected_refunded={p["index"]: p.get("refunded") or 0
                           for p in data.get("payments") or []})
    await commit_peer_revision(
        session, grant=grant, claim=claim, entity_type="doc",
        source_entity_id=source_entity_id, source_revision=source_revision)
    return outcome.records[0].status


def _keep_receipt_fields(lines: list[dict], target: dict, parcels: set[str]) -> None:
    """What a line received and the parcel its receipt created belong to the receiving
    company's own receipts, which are brought to the source's after the import; each line
    keeps the receiving document's values for them meanwhile."""
    received = set(target.get("received_item_ids") or [])
    held = target.get("line_items") or []
    for index, line in enumerate(lines):
        before = held[index] if index < len(held) and isinstance(held[index], dict) else {}
        if "quantity_received" in before:
            line["quantity_received"] = before["quantity_received"]
        else:
            line.pop("quantity_received", None)
        if before.get("entity_id") in received:
            line["entity_id"] = before["entity_id"]
        elif line.get("entity_id") in received or line.get("entity_id") in parcels:
            line.pop("entity_id")


def _validate_stock(data: dict) -> tuple[list[dict], set[str]]:
    """The source's receipts and the items its fulfilment shipped, checked for shape."""
    receipts = data.get("stock_receipts") or []
    if not isinstance(receipts, list) or len(receipts) > 200:
        raise HTTPException(422, "Invalid stock receipts")
    for receipt in receipts:
        location = receipt.get("location") if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict) or type(receipt.get("event_id")) is not int
                or not isinstance(receipt.get("key"), str) or not receipt["key"]
                or len(receipt["key"]) > 255 or not isinstance(location, dict)
                or not isinstance(receipt.get("received_items"), list)
                or not 1 <= len(receipt["received_items"]) <= 200
                or not isinstance(receipt.get("created_item_ids", []), list)
                or not isinstance(receipt.get("parcels", {}), dict)
                or not all(isinstance(it, dict) for it in receipt["received_items"])):
            raise HTTPException(422, "Invalid stock receipt")
    shipped = data.get("fulfilled_items") or []
    if not isinstance(shipped, list) or not all(isinstance(x, dict) for x in shipped):
        raise HTTPException(422, "Invalid fulfilled items")
    return receipts, {str(x["item_id"]) for x in shipped if str(x.get("item_id") or "").startswith("item:")}


async def _replay_receipts(session: AsyncSession, grant, target_id: str, receipts: list[dict],
                           actor_id, settings: dict) -> None:
    """Bring the receiving document's receipts to the source's present ones. A receipt the
    source undid is undone here (undo takes back every receipt on the bill, and the source's
    remaining ones are then received again); each receipt is received once per undo."""
    from sqlalchemy import select
    from celerp.models.ledger import LedgerEntry
    from celerp_docs.routes import undo_receive

    company_id = grant.company_id
    prefix = f"peer:receive:{grant.source_company_id}:"
    events = (await session.execute(select(
        LedgerEntry.event_type, LedgerEntry.idempotency_key,
    ).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == target_id,
        LedgerEntry.event_type.in_(("doc.received", "doc.receive_undone", "doc.reverted")),
    ).order_by(LedgerEntry.id))).all()
    cycle, here = 0, []
    for event_type, key in events:
        if event_type != "doc.received":
            cycle, here = cycle + 1, []
        else:
            here.append(key)
    # A receipt is here as replayed (its id after the prefix) or as copied (its own key).
    source = {str(receipt["event_id"]): receipt.get("key") for receipt in receipts}
    held = {key[len(prefix):].split(":")[0] if key.startswith(prefix) else key for key in here}
    if held - set(source) - set(source.values()):
        await undo_receive(entity_id=target_id, company_id=str(company_id), _=None,
                           user=SimpleNamespace(id=actor_id), session=session)
        cycle, held = cycle + 1, set()
    for receipt in receipts:
        if str(receipt["event_id"]) not in held and receipt.get("key") not in held:
            await _replay_receipt(session, grant, target_id, receipt, actor_id, settings,
                                  key=prefix + str(receipt["event_id"]) + (f":{cycle}" if cycle else ""))


async def _replay_receipt(session: AsyncSession, grant, target_id: str, receipt: dict,
                          actor_id, settings: dict, *, key: str) -> None:
    """Receive the goods of one source receipt onto the receiving document: into the
    receiving company's matching location, onto the mapped items, with each parcel it
    created paired with the source's parcel and given the source's current product data."""
    from celerp.models.company_peer import CompanyPeerIdentity
    from celerp.services.peer_identity import _digest, receive_product, resolve_peer_location
    from celerp_docs.routes import (
        ReceiveBody, ReceivedItem, _get_doc, _refuse_receipt_when_not_open, record_receipt,
    )

    company_id = grant.company_id
    location = receipt["location"]
    location_id = await resolve_peer_location(
        session, grant=grant, source_id=str(location.get("id") or ""),
        name=location.get("name"), location_type=location.get("type"))
    lines = []
    for source_line in receipt["received_items"]:
        line = {k: source_line[k] for k in _RECEIPT_KEYS if source_line.get(k) is not None}
        if line.get("item_id"):
            line["item_id"] = await _map_identity(session, grant, "item", str(line["item_id"]))
        try:
            lines.append(ReceivedItem.model_validate(line))
        except ValidationError as exc:
            raise HTTPException(422, "Invalid stock receipt line") from exc
    body = ReceiveBody(location_id=location_id, received_items=lines)
    row = await _get_doc(session, company_id, target_id, for_update=True)
    _refuse_receipt_when_not_open(row.state)
    before = len(row.state.get("received_item_ids") or [])
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    await record_receipt(session, company_id, row, body, key, digest, role="owner",
                         settings=settings, user=SimpleNamespace(id=actor_id))
    row = await _get_doc(session, company_id, target_id)
    made = list(row.state.get("received_item_ids") or [])[before:]
    sources = [str(x) for x in receipt.get("created_item_ids") or []]
    if len(made) != len(sources):
        raise HTTPException(409, "Received parcels do not match the source receipt")
    for source_id, target_item in zip(sources, made):
        session.add(CompanyPeerIdentity(
            company_id=company_id, source_company_id=grant.source_company_id,
            entity_type="item", source_entity_id=source_id, source_key_hash=_digest(source_id),
            target_entity_id=target_item, last_revision=0))
    await session.flush()
    for source_id, parcel in (receipt.get("parcels") or {}).items():
        if source_id not in sources or not isinstance(parcel, dict):
            raise HTTPException(422, "Invalid received parcel")
        from celerp.services.peer_import import ProductData
        await receive_product(session, grant=grant, source_entity_id=source_id,
                              source_revision=parcel.get("revision"),
                              data=ProductData.model_validate(parcel.get("data")).model_dump())


async def _replay_fulfilment(session: AsyncSession, grant, target_id: str, shipped: set[str],
                             actor_id) -> None:
    """Bring the receiving document's shipments to the source's: take back the goods it
    shipped that the source took back, then ship the lines whose items the source shipped
    and it has not."""
    from celerp_docs.routes import (
        RevertLinesRequest, _fulfill_lines_impl, _get_doc, _revert_lines_impl,
    )

    wanted = {await _map_identity(session, grant, "item", source) for source in shipped}
    row = await _get_doc(session, grant.company_id, target_id, for_update=True)
    done = {str(x.get("item_id")) for x in row.state.get("fulfilled_items") or []
            if isinstance(x, dict)}
    if back := sorted(done - wanted):
        await _revert_lines_impl(row, RevertLinesRequest(line_entity_ids=back), [],
                                 SimpleNamespace(id=actor_id), session)
        row = await _get_doc(session, grant.company_id, target_id, for_update=True)
        done -= set(back)
    indices = [i for i, line in enumerate(row.state.get("line_items") or [])
               if (line.get("entity_id") or line.get("item_id")) in wanted - done]
    if indices:
        await _fulfill_lines_impl(target_id, indices, str(grant.company_id),
                                  SimpleNamespace(id=actor_id), session, commit=False)
