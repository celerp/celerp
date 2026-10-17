# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Send approved company contacts using the canonical connector sync runner."""
from __future__ import annotations

import uuid
from typing import NamedTuple
from urllib.parse import urlsplit

from sqlalchemy import select

from celerp.connectors.base import (
    ConnectorBase, ConnectorCategory, ConnectorContext,
    SyncDirection, SyncEntity, SyncResult,
)
from celerp.connectors.http import RateLimitedClient
from celerp.models.projections import Projection
from celerp.services.peer_access import ENTITY_ROUTE, SOURCE_HEADER

_PAGE = 100
_FIELDS = frozenset({
    "name", "company_name", "email", "phone", "billing_address",
    "shipping_address", "currency", "contact_type",
})
_PRODUCT_FIELDS = ("name", "sku", "description", "sell_by", "category",
                   "barcode", "retail_price", "wholesale_price")


def _target_url(ctx: ConnectorContext, entity: str = "contacts") -> str:
    """The receiving company's route for ``entity``; a file route keeps its ``{entity_id}``."""
    raw = ctx.store_handle
    if not isinstance(raw, str) or len(raw) > 1024:
        raise ValueError("No receiving Celerp installation is connected")
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme == "https" and parsed.hostname is not None
                 and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 443) and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The receiving Celerp address is invalid")
    if entity not in ENTITY_ROUTE:
        raise ValueError("Unsupported peer entity")
    return raw.rstrip("/") + ENTITY_ROUTE[entity]


def peer_headers(ctx: ConnectorContext) -> dict:
    """What every request to the receiving company carries: the key and the sending company."""
    return {"Authorization": "Peer " + ctx.access_token, SOURCE_HEADER: str(ctx.company_id)}


class _Kind(NamedTuple):
    event: str  # the import record event the receiving company writes it as
    entity: SyncEntity  # the sync entity its runs are recorded under
    label: str  # how a record of this kind is named to the user


# Every kind of record the connector moves, in one place.
_KINDS = {
    "contacts": _Kind("crm.contact.created", SyncEntity.CONTACTS, "Contact"),
    "products": _Kind("item.created", SyncEntity.PRODUCTS, "Product"),
    "documents": _Kind("doc.created", SyncEntity.DOCUMENTS, "Document"),
    "inventory": _Kind("list.created", SyncEntity.INVENTORY, "Stock count"),
    "journals": _Kind("acc.journal_entry.created", SyncEntity.JOURNALS, "Journal entry"),
}


async def _read_projection_page(company_id: uuid.UUID, after: str, since, entity: str) -> list[tuple[str, int, dict]]:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        query = select(
            Projection.entity_id, Projection.version, Projection.state,
        ).where(
            Projection.company_id == company_id,
            Projection.entity_type == entity,
            Projection.entity_id > after,
        )
        if since is not None:
            query = query.where(Projection.updated_at >= since)
        rows = (await session.execute(query.order_by(Projection.entity_id).limit(_PAGE))).all()
    return [(str(entity_id), int(version), dict(state or {})) for entity_id, version, state in rows]


async def _read_page(company_id: uuid.UUID, after: str, since=None) -> list[tuple[str, int, dict]]:
    return await _read_projection_page(company_id, after, since, "contact")


async def _read_products(company_id: uuid.UUID, after: str, since=None) -> list[tuple[str, int, dict]]:
    """Products, each parcel a receipt created naming that receipt's document (``received_on``)."""
    from celerp.db import get_session_ctx
    from celerp.models.ledger import LedgerEntry

    out = await _read_projection_page(company_id, after, since, "item")
    if not out:
        return out
    async with get_session_ctx() as session:
        created = (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.metadata_).where(
            LedgerEntry.company_id == company_id, LedgerEntry.event_type == "item.created",
            LedgerEntry.entity_id.in_([eid for eid, _, _ in out])))).all()
    received_on = {str(eid): meta["source_doc"] for eid, meta in created
                   if isinstance(meta, dict) and meta.get("source_doc")}
    return [(eid, revision, {**state, "received_on": received_on[eid]} if eid in received_on else state)
            for eid, revision, state in out]


async def _stock_receipts(session, company_id: uuid.UUID, doc_ids: list[str]) -> dict[str, list[dict]]:
    """Each document's present receipts, in order: those after its last undo or revert, each
    with its location and the current product data of every parcel it created."""
    from celerp.models.company import Location
    from celerp.models.ledger import LedgerEntry

    events = (await session.execute(select(
        LedgerEntry.id, LedgerEntry.entity_id, LedgerEntry.event_type, LedgerEntry.data,
        LedgerEntry.location_id, LedgerEntry.idempotency_key,
    ).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(doc_ids),
        LedgerEntry.event_type.in_(("doc.received", "doc.receive_undone", "doc.reverted")),
    ).order_by(LedgerEntry.id))).all()
    by_doc: dict[str, list[dict]] = {}
    for event_id, entity_id, event_type, data, location_id, key in events:
        if event_type != "doc.received":
            by_doc[str(entity_id)] = []
            continue
        data = data or {}
        location = await session.get(Location, location_id) if location_id else None
        parcels = {}
        for item_id in data.get("created_item_ids") or []:
            item = await session.get(Projection, (company_id, item_id))
            if item is not None:
                parcels[str(item_id)] = {"revision": int(item.version), "data": {
                    key: (item.state or {}).get(key) for key in _PRODUCT_FIELDS}}
        by_doc.setdefault(str(entity_id), []).append({
            "event_id": int(event_id),
            # A receipt from before the company copy was taken is in the copy under this key.
            "key": key,
            "location": {"id": str(location_id or ""),
                         "name": location.name if location is not None else "",
                         "type": location.type if location is not None else ""},
            "received_items": data.get("received_items") or [],
            "created_item_ids": [str(x) for x in data.get("created_item_ids") or []],
            "parcels": parcels,
        })
    return by_doc


async def _read_documents(company_id: uuid.UUID, after: str, since=None) -> list[tuple[str, int, dict]]:
    """Attach each document's present receipts and its source refund entries, preserving
    their original order and accounting dates."""
    from celerp.db import get_session_ctx
    from celerp.models.ledger import LedgerEntry

    async with get_session_ctx() as session:
        query = select(Projection.entity_id, Projection.version, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "doc",
            Projection.entity_id > after,
        )
        if since is not None:
            query = query.where(Projection.updated_at >= since)
        rows = (await session.execute(
            query.order_by(Projection.entity_id).limit(_PAGE))).all()
        out = [(str(eid), int(version), dict(state or {}))
               for eid, version, state in rows]
        received = [entity_id for entity_id, _, state in out
                    if state.get("received_items") or state.get("received_item_ids")]
        receipts = await _stock_receipts(session, company_id, received) if received else {}
        out = [(eid, revision, {**state, "stock_receipts": receipts[eid]} if receipts.get(eid) else state)
               for eid, revision, state in out]
        with_refunds = [
            entity_id for entity_id, _, state in out
            if any((p.get("refund_count") or p.get("refunded"))
                   for p in (state.get("payments") or []) if isinstance(p, dict))
        ]
        if not with_refunds:
            return out
        refunds = (await session.execute(select(
            LedgerEntry.id, LedgerEntry.entity_id, LedgerEntry.event_type, LedgerEntry.data,
        ).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id.in_(with_refunds),
            LedgerEntry.event_type.in_(("doc.payment.refunded", "doc.payment.refund_reversed")),
        ).order_by(LedgerEntry.id).limit(1001))).all()
        if len(refunds) > 1000:
            raise ValueError("Source refund history exceeds the per-run reconciliation limit")
        by_doc: dict[str, list[dict]] = {}
        for event_id, entity_id, event_type, data in refunds:
            kind = "reversed" if event_type == "doc.payment.refund_reversed" else "refunded"
            by_doc.setdefault(str(entity_id), []).append({
                **({"kind": "reversed"} if kind == "reversed" else {}),
                "event_id": event_id,
                "payment_index": (data or {}).get("payment_index"),
                "refund_number": (data or {}).get("refund_number"),
                "amount": (data or {}).get("amount"),
                **({"reversal_date": (data or {}).get("reversal_date")} if kind == "reversed"
                   else {"refund_date": (data or {}).get("refund_date")}),
            })
        return [(eid, revision, {**state, "payment_events": by_doc.get(eid, [])})
                for eid, revision, state in out]


async def _read_audits(company_id: uuid.UUID, after: str, since=None) -> list[tuple[str, int, dict]]:
    """Deliver each completed count in canonical ledger posting order."""
    from sqlalchemy import func, and_
    from celerp.db import get_session_ctx
    from celerp.models.company import Location
    from celerp.models.ledger import LedgerEntry

    try:
        previous_event = int(after or "0")
    except ValueError:
        raise ValueError("Invalid inventory synchronization cursor") from None
    if previous_event < 0:
        raise ValueError("Invalid inventory synchronization cursor")
    # A count may be closed again after an undo. Only its latest closing event
    # represents the present, posted audit. The ledger id is an authoritative,
    # monotonically increasing position within the source installation.
    latest = (
        select(LedgerEntry.entity_id.label("entity_id"),
               func.max(LedgerEntry.id).label("closed_id"))
        .where(LedgerEntry.company_id == company_id,
               LedgerEntry.entity_type == "list",
               LedgerEntry.event_type == "list.closed",
               LedgerEntry.data["result"].as_string() == "stock_adjusted")
        .group_by(LedgerEntry.entity_id).subquery()
    )
    async with get_session_ctx() as session:
        query = (
            select(latest.c.closed_id, Projection.entity_id,
                   Projection.version, Projection.state)
            .join(Projection, and_(
                Projection.company_id == company_id,
                Projection.entity_type == "list",
                Projection.entity_id == latest.c.entity_id))
            .join(LedgerEntry, LedgerEntry.id == latest.c.closed_id)
            .where(Projection.state["list_type"].as_string() == "audit",
                   Projection.state["status"].as_string() == "closed",
                   Projection.state["result"].as_string() == "stock_adjusted",
                   latest.c.closed_id > previous_event)
        )
        if since is not None:
            query = query.where(LedgerEntry.ts >= since)
        rows = (await session.execute(
            query.order_by(latest.c.closed_id).limit(_PAGE))).all()
        out = []
        for closed_id, entity_id, revision, state in rows:
            src = dict(state or {})
            source_location = str(src.get("location_id") or "")
            try:
                location_id = uuid.UUID(source_location)
            except ValueError:
                location_id = None
            location = await session.get(Location, location_id) if location_id else None
            if location is not None and location.company_id != company_id:
                location = None
            counts = [
                {"item_id": line.get("item_id"), "counted_qty": line.get("counted_qty")}
                for line in (src.get("line_items") or [])
                if isinstance(line, dict) and line.get("counted_qty") is not None
            ]
            out.append((str(entity_id), int(revision), {
                "location_id": source_location,
                "location_name": location.name if location is not None else "",
                "location_type": location.type if location is not None else "",
                "counts": counts,
                "source_closed_event_id": int(closed_id),
            }))
        return out


def attention_label(state: dict, fallback: str) -> str:
    """How the user knows a record: its document number, name, stock location, or memo."""
    for key in ("ref_id", "name", "location_name", "memo"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


async def _excluded_stock_document(company_id: uuid.UUID) -> tuple[str, str] | None:
    """An excluded stock transaction cannot be represented as a count variance.
    Returns the document's id and its attention label."""
    from celerp.db import get_session_ctx
    from celerp.services.peer_documents import requires_stock_movement

    after = ""
    async with get_session_ctx() as session:
        while True:
            rows = (await session.execute(select(
                Projection.entity_id, Projection.state,
            ).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
                Projection.entity_id > after,
                Projection.state["accounting_sync_excluded"].as_boolean().is_(True),
            ).order_by(Projection.entity_id).limit(_PAGE))).all()
            for entity_id, state in rows:
                state = state or {}
                if state.get("status") not in ("draft", "void") and requires_stock_movement(state):
                    return str(entity_id), attention_label(state, str(entity_id))
            if len(rows) < _PAGE:
                return None
            after = rows[-1][0]


_LINE_FIELDS = ("account", "debit", "credit", "contact", "fx_debit", "fx_credit",
                "fx_currency", "fx_rate")


async def _read_journals(company_id: uuid.UUID, after: str, since=None) -> list[tuple[str, int, dict]]:
    """The company's own manual journal entries: never one its accountant posted here, and
    never one a document posted, which the document carries. A reversal names what it
    reverses: the accountant's own journal when that is where it came from."""
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        query = select(Projection.entity_id, Projection.version, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.entity_id > after,
            Projection.state["je_type"].as_string() == "manual",
            Projection.state["status"].as_string().in_(("posted", "void")),
            Projection.state["peer_origin"].as_string().is_(None))
        if since is not None:
            query = query.where(Projection.updated_at >= since)
        rows = (await session.execute(query.order_by(Projection.entity_id).limit(_PAGE))).all()
        reversed_ids = {str(s["reverses"]) for _, _, s in rows if (s or {}).get("reverses")}
        origins = {str(eid): (s or {}).get("peer_origin") for eid, s in (await session.execute(
            select(Projection.entity_id, Projection.state).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(reversed_ids)))).all()} if reversed_ids else {}
    out = []
    for entity_id, version, state in rows:
        state = dict(state or {})
        data = {"memo": state.get("memo") or "", "ts": str(state.get("ts") or "")[:10],
                "status": state.get("status"),
                "entries": [{k: line[k] for k in _LINE_FIELDS if line.get(k) is not None}
                            for line in state.get("entries") or []]}
        if state.get("reverses"):
            origin = origins.get(str(state["reverses"]))
            data["reverses"] = ({"origin": origin["journal"]} if isinstance(origin, dict)
                                else {"journal": str(state["reverses"])})
        out.append((str(entity_id), int(version), data))
    return out


def _account_codes(data: dict) -> list[str]:
    """The accounts a journal's lines, or a document's lines and payments, post to."""
    codes = [line.get("account") for line in data.get("entries") or [] if isinstance(line, dict)]
    codes += [line.get("account_code") for line in data.get("line_items") or [] if isinstance(line, dict)]
    codes += [p.get("bank_account") for p in data.get("payments") or [] if isinstance(p, dict)]
    return [code for code in codes if isinstance(code, str) and code]


def _reversed_last(items: list) -> list:
    """Journals reversing these books' own journals, each after the one it reverses when
    both are here, so a reversal, its undo, and so on reach the firm in that order."""
    pending = {item[0][0]: item for item in items}
    ordered = []
    while pending:
        ready = [eid for eid, item in pending.items()
                 if item[0][2]["reverses"]["journal"] not in pending] or list(pending)
        ordered += [pending.pop(eid) for eid in sorted(ready)]
    return ordered


class _Refused(ValueError):
    """The receiving company refused a record and said why."""


def _refusal(errors: list) -> str:
    """The receiving company's reason, without the record id it prefixes."""
    if not errors:
        return "Receiving installation did not acknowledge update"
    return str(errors[0]).partition(": ")[2] or str(errors[0])


class CelerpConnector(ConnectorBase):
    name = "celerp"
    display_name = "Celerp Accountant"
    direction = SyncDirection.BOTH
    category = ConnectorCategory.ACCOUNTING
    # Fetched from the accountant before anything is sent: its journals, then its lock
    # date, so a journal it posted before locking is not declined by its own lock.
    supported_entities = [SyncEntity.JOURNALS, SyncEntity.PERIOD_LOCK]
    store_scoped_ids = False
    conflict_strategy = {SyncEntity.CONTACTS: "source"}
    outbound_dependencies = {
        "journals_out": frozenset({"contacts_out"}),
        "documents_out": frozenset({"contacts_out", "products_out"}),
        "inventory_out": frozenset({"documents_out"}),
        "attachments_out": frozenset({"documents_out"}),
    }

    async def sync_products(self, ctx: ConnectorContext, since=None) -> SyncResult:
        raise NotImplementedError("Inbound peer synchronization is not supported")

    async def sync_orders(self, ctx: ConnectorContext, since=None) -> SyncResult:
        raise NotImplementedError("Inbound peer synchronization is not supported")

    async def before_outbound(self, ctx: ConnectorContext, now=None) -> None:
        from celerp.connectors.celerp_key import check_key
        await check_key(ctx, now=now)

    async def _send_outbound(self, ctx: ConnectorContext, kind: str, reader) -> SyncResult:
        """Shared incremental delivery; entity-specific data never bypasses validation."""
        from celerp.connectors.sync_runner import _last_success_watermark

        from celerp.services import peer_accounts, peer_journals
        from celerp.services.peer_access import key_ids

        if (spec := _KINDS.get(kind)) is None:
            raise ValueError("Unsupported peer entity")
        result = SyncResult(entity=spec.entity, direction=SyncDirection.OUTBOUND,
                            errors=[], attention=[])
        target = _target_url(ctx, kind)
        if not isinstance(ctx.access_token, str) or not ctx.access_token.startswith("cp1."):
            raise ValueError("No scoped receiving credential is connected")
        company_id = uuid.UUID(ctx.company_id)
        firm_company, grant_id = key_ids(ctx.access_token)
        since = await _last_success_watermark(ctx.company_id, self.name, kind + "_out")
        fields = _FIELDS if kind == "contacts" else _PRODUCT_FIELDS if kind == "products" else None
        label = spec.label

        async def records():
            """Each record with its last delivery and the firm's lock date, in reading
            order, except that a journal reversing one of these books' own journals goes
            after it: the firm takes a reversal only of a journal it already has."""
            after, later = "", []
            while True:
                page = await reader(company_id, after, since)
                if not page:
                    break
                marks, firm_lock = await peer_journals.sent_marks(
                    company_id, grant_id, kind, [entity_id for entity_id, _, _ in page])
                for record in page:
                    item = (record, marks.get(record[0]), firm_lock)
                    if kind == "journals" and (record[2].get("reverses") or {}).get("journal"):
                        later.append(item)
                    else:
                        yield item
                after = (str(page[-1][2]["source_closed_event_id"]) if kind == "inventory"
                         else page[-1][0])
                if len(page) < _PAGE:
                    break
            for item in _reversed_last(later):
                yield item

        async with RateLimitedClient(public_only=True) as client:
            async for (entity_id, revision, state), sent, firm_lock in records():
                if sent is not None and sent[0] == revision:
                    if sent[1] is None:
                        result.skipped += 1  # the firm has this revision
                        continue
                    if peer_journals.refused_by_lock(sent[1], firm_lock):
                        result.errors.append(label + " delivery incomplete")
                        result.attention.append({
                            "id": entity_id, "label": attention_label(state, entity_id),
                            "reason": sent[1]})
                        continue
                reason = None
                if type(revision) is not int or revision < 1:
                    reason = "Contact version unavailable" if kind == "contacts" else "Product state needs review"
                elif kind == "contacts" and state.get("is_self"):
                    result.skipped += 1
                    continue
                elif state.get("deleted") or state.get("merged_into"):
                    reason = "Deleted contact needs review" if kind == "contacts" else "Product state needs review"
                if kind == "journals":
                    data = state
                elif kind == "inventory":
                    data = state
                    if (not data.get("location_id") or not data.get("location_name")
                            or not data.get("location_type") or not data.get("counts")):
                        reason = "Stock count location or entries need review"
                elif kind == "documents":
                    from celerp.services.peer_documents import _FIELDS as doc_fields
                    data = {key: state[key] for key in doc_fields if key in state}
                    if state.get("accounting_sync_excluded"):
                        result.skipped += 1
                        continue
                    data.setdefault("status", "draft")
                else:
                    data = {key: state.get(key) for key in fields}
                    if kind == "products" and state.get("received_on"):
                        data["received_on"] = state["received_on"]
                if reason is None and kind in ("contacts", "products") and (not isinstance(data.get("name"), str) or not data["name"].strip()):
                    reason = "Contact name unavailable" if kind == "contacts" else "Product name missing"
                if reason is not None:
                    result.errors.append(reason)
                    result.attention.append({
                        "id": entity_id, "label": attention_label(state, entity_id),
                        "reason": reason,
                    })
                    continue
                if kind == "products" and not data.get("sell_by"):
                    data["sell_by"] = "piece"
                if kind in ("journals", "documents"):
                    data = {**data, "accounts": await peer_accounts.outgoing_for(
                        company_id, firm_company, _account_codes(data))}
                try:
                    response = await client.post(target, headers=peer_headers(ctx), json={
                        "records": [{
                            "entity_id": entity_id, "event_type": spec.event,
                            "data": data, "source": "celerp",
                            "idempotency_key": f"peer:{company_id}:{entity_id}:{revision}",
                            "source_revision": revision,
                        }]})
                    if response.status_code == 403 and kind == "journals":
                        return result  # the firm's key does not open journals
                    if response.status_code != 200:
                        raise ValueError("Receiving installation refused update")
                    body = response.json()
                    refused = body.get("errors") or []
                    status = next((s for s in ("created", "updated", "skipped")
                                   if body.get(s) == 1), None)
                    if refused or status is None:
                        raise _Refused(_refusal(refused))
                    await peer_journals.note_delivery(company_id, grant_id, kind, entity_id,
                                                      revision, None, "")
                    result.record(status)
                except Exception as exc:
                    if isinstance(exc, _Refused):
                        await peer_journals.note_delivery(
                            company_id, grant_id, kind, entity_id, revision, str(exc),
                            f"{label} {attention_label(state, entity_id)}")
                    result.errors.append(label + " delivery incomplete")
                    result.attention.append({
                        "id": entity_id, "label": attention_label(state, entity_id),
                        "reason": (str(exc) if isinstance(exc, _Refused)
                                   else "Receiving installation needs attention"),
                    })
        return result

    async def _fetch(self, client, ctx: ConnectorContext, entity: str, params: dict | None = None,
                     closed: tuple[int, ...] = (403,)):
        """The firm's answer to a fetch, or None when it answers with a ``closed`` status:
        by default, when its key does not open journals."""
        response = await client.get(_target_url(ctx, entity), headers=peer_headers(ctx),
                                    params=params or {})
        if response.status_code in closed:
            return None
        if response.status_code == 401:
            raise ValueError("Accountant's key was revoked or has expired")
        if response.status_code != 200:
            raise ValueError("Receiving installation needs attention")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("firm"), str):
            raise ValueError("Receiving installation needs attention")
        return body

    async def sync_journals(self, ctx: ConnectorContext, since=None) -> SyncResult:
        """Post the accountant's journal entries in these books, each once; decline one
        these books cannot take and tell the accountant why."""
        from celerp.services import peer_journals
        from celerp.services.peer_access import key_ids

        journals = _KINDS["journals"]
        result = SyncResult(entity=journals.entity, direction=SyncDirection.INBOUND,
                            errors=[], attention=[])
        company_id = uuid.UUID(ctx.company_id)
        firm_company, grant_id = key_ids(ctx.access_token)
        after = ""
        async with RateLimitedClient(public_only=True) as client:
            while True:
                body = await self._fetch(client, ctx, "journals_in", {"after": after})
                if body is None:
                    return result
                items = body.get("items")
                if not isinstance(items, list):
                    raise ValueError("Receiving installation needs attention")
                for item in items:
                    label = attention_label(item if isinstance(item, dict) else {}, journals.label)
                    try:
                        fetched = await peer_journals.apply_firm_journal(
                            company_id, grant_id, body["firm"], firm_company, item)
                        if fetched.answer is not None:
                            response = await client.post(
                                _target_url(ctx, "journals"), headers=peer_headers(ctx), json={
                                    "records": [{
                                        "entity_id": item["id"], "event_type": journals.event,
                                        "data": fetched.answer, "source": "celerp",
                                        "idempotency_key": f"peer:{company_id}:answer:{item['id']}:"
                                                           f"{uuid.uuid5(uuid.NAMESPACE_URL, str(fetched.declined))}",
                                        "source_revision": item["revision"]}]})
                            answer = response.json() if response.status_code == 200 else {}
                            if response.status_code != 200 or answer.get("errors"):
                                raise _Refused(_refusal(answer.get("errors") or []))
                            await peer_journals.answered(company_id, grant_id, item, fetched.declined)
                        if fetched.declined is not None:
                            result.skipped += 1
                            result.attention.append({"id": str(item.get("id")), "label": label,
                                                     "reason": fetched.declined, "locked": fetched.locked})
                        else:
                            result.record(fetched.status)
                    except Exception as exc:
                        result.errors.append("Journal entry fetch incomplete")
                        result.attention.append({
                            "id": str(item.get("id")) if isinstance(item, dict) else "", "label": label,
                            "reason": (str(exc) if isinstance(exc, (_Refused, ValueError))
                                       else "Receiving installation needs attention")})
                after = body.get("next")
                if not isinstance(after, str) or not after:
                    return result

    async def sync_period_lock(self, ctx: ConnectorContext, since=None) -> SyncResult:
        """Keep the accountant's lock date beside these books' own, moving theirs forward
        to it once. A key the firm no longer accepts (revoked or expired) or that no longer
        opens journals ends its lock date until the key answers again."""
        from celerp.services import peer_journals
        from celerp.services.peer_access import key_ids

        result = SyncResult(entity=SyncEntity.PERIOD_LOCK, direction=SyncDirection.INBOUND,
                            errors=[], attention=[])
        async with RateLimitedClient(public_only=True) as client:
            body = await self._fetch(client, ctx, "period_lock", closed=(401, 403))
        changed = await peer_journals.apply_firm_lock(uuid.UUID(ctx.company_id),
                                                      key_ids(ctx.access_token)[1], body)
        result.record("updated" if changed else "noop")
        return result

    async def sync_journals_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "journals", _read_journals)

    async def sync_contacts_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "contacts", _read_page)

    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "products", _read_products)

    async def sync_documents_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "documents", _read_documents)

    async def sync_inventory_out(self, ctx: ConnectorContext) -> SyncResult:
        excluded = await _excluded_stock_document(uuid.UUID(ctx.company_id))
        if excluded is not None:
            excluded_id, excluded_label = excluded
            return SyncResult(
                entity=_KINDS["inventory"].entity, direction=SyncDirection.OUTBOUND,
                errors=["Inventory needs a complete source-document reconciliation"],
                attention=[{"id": excluded_id, "label": excluded_label,
                            "reason": "A stock-linked document was excluded from accounting sync"}],
            )
        return await self._send_outbound(ctx, "inventory", _read_audits)

    async def sync_attachments_out(self, ctx: ConnectorContext) -> SyncResult:
        from celerp.services.peer_attachment_sync import sync_document_attachments
        return await sync_document_attachments(self, ctx)
