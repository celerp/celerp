# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Send approved company contacts using the canonical connector sync runner."""
from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from sqlalchemy import select

from celerp.connectors.base import (
    ConnectorBase, ConnectorCategory, ConnectorContext,
    SyncDirection, SyncEntity, SyncResult,
)
from celerp.connectors.http import RateLimitedClient
from celerp.models.projections import Projection

_PAGE = 100
_FIELDS = frozenset({
    "name", "company_name", "email", "phone", "billing_address",
    "shipping_address", "currency", "contact_type",
})


def _target_url(ctx: ConnectorContext, entity: str = "contacts") -> str:
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
    if entity not in ("contacts", "products", "documents", "attachments"):
        raise ValueError("Unsupported peer entity")
    return raw.rstrip("/") + "/company-peers/" + entity


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
    return await _read_projection_page(company_id, after, since, "item")


async def _read_documents(company_id: uuid.UUID, after: str, since=None) -> list[tuple[str, int, dict]]:
    """Attach source refund entries, preserving their original order and accounting dates."""
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



class CelerpConnector(ConnectorBase):
    name = "celerp"
    display_name = "Celerp Accountant"
    direction = SyncDirection.OUTBOUND
    category = ConnectorCategory.ACCOUNTING
    supported_entities = []
    store_scoped_ids = False
    conflict_strategy = {SyncEntity.CONTACTS: "source"}

    async def sync_products(self, ctx: ConnectorContext, since=None) -> SyncResult:
        raise NotImplementedError("Inbound peer synchronization is not supported")

    async def sync_orders(self, ctx: ConnectorContext, since=None) -> SyncResult:
        raise NotImplementedError("Inbound peer synchronization is not supported")

    async def _send_outbound(self, ctx: ConnectorContext, kind: str, reader) -> SyncResult:
        """Shared incremental delivery; entity-specific data never bypasses validation."""
        from celerp.connectors.sync_runner import _last_success_watermark

        if kind not in ("contacts", "products", "documents"):
            raise ValueError("Unsupported peer entity")
        entity = {"contacts": SyncEntity.CONTACTS, "products": SyncEntity.PRODUCTS,
                  "documents": SyncEntity.DOCUMENTS}[kind]
        result = SyncResult(entity=entity, direction=SyncDirection.OUTBOUND,
                            errors=[], attention=[])
        target = _target_url(ctx, kind)
        if not isinstance(ctx.access_token, str) or not ctx.access_token.startswith("cp1."):
            raise ValueError("No scoped receiving credential is connected")
        company_id = uuid.UUID(ctx.company_id)
        since = await _last_success_watermark(ctx.company_id, self.name, kind + "_out")
        fields = (_FIELDS if kind == "contacts" else
                  ("name", "sku", "description", "sell_by", "category",
                   "barcode", "retail_price", "wholesale_price") if kind == "products"
                  else None)
        after = ""
        async with RateLimitedClient(public_only=True) as client:
            while True:
                page = await reader(company_id, after, since)
                if not page:
                    break
                for entity_id, revision, state in page:
                    label = {"contacts": "Contact", "products": "Product",
                             "documents": "Document"}[kind]
                    reason = None
                    if type(revision) is not int or revision < 1:
                        reason = "Contact version unavailable" if kind == "contacts" else "Product state needs review"
                    elif kind == "contacts" and state.get("is_self"):
                        result.skipped += 1
                        continue
                    elif state.get("deleted") or state.get("merged_into"):
                        reason = "Deleted contact needs review" if kind == "contacts" else "Product state needs review"
                    if kind == "documents":
                        from celerp.services.peer_documents import _FIELDS as doc_fields
                        data = {key: state[key] for key in doc_fields if key in state}
                        if state.get("accounting_sync_excluded"):
                            result.skipped += 1
                            continue
                        data.setdefault("status", "draft")
                        from celerp.services.peer_documents import requires_stock_movement
                        if state.get("status", "draft") != "draft" and requires_stock_movement(state):
                            reason = "Inventory-linked document requires full copy or shared access"
                    else:
                        data = {key: state.get(key) for key in fields}
                    if reason is None and kind != "documents" and (not isinstance(data.get("name"), str) or not data["name"].strip()):
                        reason = "Contact name unavailable" if kind == "contacts" else "Product name missing"
                    if reason is not None:
                        result.errors.append(reason)
                        result.attention.append({"id": entity_id, "label": entity_id, "reason": reason})
                        continue
                    if kind == "products" and not data.get("sell_by"):
                        data["sell_by"] = "piece"
                    try:
                        response = await client.post(target, headers={
                            "Authorization": "Peer " + ctx.access_token,
                        }, json={
                            "source_company_id": str(company_id),
                            "source_entity_id": entity_id,
                            "source_revision": revision, "data": data,
                        })
                        if response.status_code != 200:
                            raise ValueError("Receiving installation refused update")
                        status = response.json().get("status")
                        if status not in ("created", "updated", "skipped"):
                            raise ValueError("Receiving installation did not acknowledge update")
                        result.record(status)
                    except Exception:
                        result.errors.append(label + " delivery incomplete")
                        result.attention.append({
                            "id": entity_id, "label": entity_id,
                            "reason": "Receiving installation needs attention",
                        })
                after = page[-1][0]
                if len(page) < _PAGE:
                    break
        return result

    async def sync_contacts_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "contacts", _read_page)

    async def sync_products_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "products", _read_products)

    async def sync_documents_out(self, ctx: ConnectorContext) -> SyncResult:
        return await self._send_outbound(ctx, "documents", _read_documents)

    async def sync_attachments_out(self, ctx: ConnectorContext) -> SyncResult:
        from celerp.services.peer_attachment_sync import sync_document_attachments
        return await sync_document_attachments(self, ctx)
