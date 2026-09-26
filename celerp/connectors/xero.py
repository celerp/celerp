# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Xero connector.

OAuth model: CelERP relay service holds one registered Xero app.
Paying customers authorize via relay, and the relay keeps the Xero
connection. API calls are sent to the relay, which forwards them to the
Xero Accounting API for the connected organisation.

Xero token model:
  - Access tokens expire after 30 minutes
  - Refresh tokens are long-lived (rotate on each refresh)
  - One token per (instance_id, tenant_id)

API version: Xero Accounting API v2 (https://api.xero.com/api.xro/2.0)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx

from celerp.connectors.http import RateLimitedClient
from celerp.connectors.util import money
from celerp.connectors.base import (
    ConnectorBase,
    ConnectorCategory,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
)
import celerp.connectors.upsert as _upsert

if TYPE_CHECKING:
    from celerp.connectors.outbound_queue import OutboundOperation

log = logging.getLogger(__name__)

_PAGE_SIZE = 100


def _api_base() -> str:
    from celerp.gateway.state import relay_http_url
    return f"{relay_http_url()}/connectors/xero/api"


# Longer than the relay's own 30s timeout for Xero, so the relay normally
# settles the outcome of a call before the desktop gives up on it.
_RELAY_TIMEOUT_S = 45.0


# Resending a create with the same Idempotency-Key is safe only while Xero still
# remembers the key; past this age Celerp looks the invoice up instead.
_KEY_WINDOW = timedelta(minutes=6)


def _invoice_request(inv: dict) -> dict:
    return {
        "Invoices": [{
            "Type": "ACCREC",
            "InvoiceNumber": inv["ref_id"],
            "Contact": {"ContactID": inv.get("customer_external_id") or inv.get("customer_name", "")},
            "LineItems": [
                {
                    "Description": line.get("description", ""),
                    "Quantity": float(line.get("quantity", 1)),
                    "UnitAmount": float(line.get("unit_price", 0)),
                    "LineAmount": float(line.get("total", 0)),
                }
                for line in (inv.get("line_items") or [])
            ],
            "Status": "AUTHORISED",
        }]
    }


def _new_attempt(state: dict) -> dict:
    return {
        **state,
        "idempotency_key": uuid.uuid4().hex,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }


def _line_amounts(invoice: dict) -> list[Decimal]:
    amounts = []
    for line in invoice.get("LineItems") or []:
        try:
            amounts.append(Decimal(str(line.get("LineAmount") or 0)).quantize(Decimal("0.01")))
        except InvalidOperation:
            amounts.append(Decimal("NaN"))
    return sorted(amounts)


def _is_sent_invoice(remote: dict, sent: dict) -> bool:
    """Whether a Xero invoice with the sent number is the one Celerp created."""
    return (
        remote.get("Type") == sent["Type"]
        and (remote.get("Contact") or {}).get("ContactID") == sent["Contact"]["ContactID"]
        and _line_amounts(remote) == _line_amounts(sent)
    )


def _where_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validation_message(resp: httpx.Response) -> str:
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    messages = [
        error.get("Message")
        for element in body.get("Elements") or []
        for error in element.get("ValidationErrors") or []
        if error.get("Message")
    ]
    return "Xero rejected it: " + ("; ".join(messages) or body.get("Message") or "invalid invoice")


def _headers() -> dict[str, str]:
    from celerp.gateway.state import relay_session_headers
    return {
        **relay_session_headers(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


class XeroConnector(ConnectorBase):
    name = "xero"
    display_name = "Xero"
    store_scoped_ids = False  # Xero ids are unique across all organisations
    supported_entities = [SyncEntity.PRODUCTS, SyncEntity.ORDERS, SyncEntity.CONTACTS]
    category = ConnectorCategory.ACCOUNTING
    direction = SyncDirection.BOTH
    conflict_strategy = {
        SyncEntity.PRODUCTS: "newest",
        SyncEntity.ORDERS: "platform",
        SyncEntity.CONTACTS: "merge",
    }

    # -- Internal helpers ------------------------------------------------------

    async def _paginate(
        self,
        ctx: ConnectorContext,
        path: str,
        key: str,
        since: datetime | None = None,
        paginated: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch a resource. Paginating endpoints (Invoices, Contacts) use Xero's
        page-based paging; endpoints that return the full set in one response
        (Items) pass paginated=False and are fetched in a single request."""
        results: list[dict[str, Any]] = []
        page = 1
        headers = _headers()
        if since:
            headers["If-Modified-Since"] = since.strftime("%a, %d %b %Y %H:%M:%S GMT")
        async with RateLimitedClient(timeout=_RELAY_TIMEOUT_S) as client:
            while True:
                params = {"page": page, "pageSize": _PAGE_SIZE} if paginated else None
                resp = await client.get(f"{_api_base()}{path}", headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                items = data.get(key, [])
                results.extend(items)
                if not paginated or len(items) < _PAGE_SIZE:
                    break
                page += 1
        return results

    # -- Products (Items in Xero) ----------------------------------------------

    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull Xero Items -> Celerp items.

        Mapping:
          Item.Code          -> item.sku
          Item.Name          -> item.name
          Item.Description   -> item.description
          Item.SalesDetails.UnitPrice -> item.sale_price
          Item.PurchaseDetails.UnitPrice -> item.cost_price
          Item.ItemID        -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.PRODUCTS)
        errors: list[str] = []

        try:
            items = await self._paginate(ctx, "/Items", "Items", since=since, paginated=False)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for xero_item in items:
            sku = (xero_item.get("Code") or "").strip()
            if not sku:
                result.skipped += 1
                continue

            idempotency_key = f"xero:item:{xero_item['ItemID']}"

            try:
                from celerp_inventory.routes import ItemCreate
                item = ItemCreate(
                    sku=sku,
                    name=xero_item.get("Name") or sku,
                    description=xero_item.get("Description") or "",
                    sell_by="piece",
                    sale_price=money((xero_item.get("SalesDetails") or {}).get("UnitPrice")),
                    cost_price=money((xero_item.get("PurchaseDetails") or {}).get("UnitPrice")),
                    idempotency_key=idempotency_key,
                )
                result.record(await _upsert.upsert_item(ctx.company_id, item))
            except Exception as exc:
                errors.append(f"SKU {sku}: {exc}")

        result.errors = errors or None
        log.info(
            "xero.sync_products company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # -- Invoices / Orders -----------------------------------------------------

    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """
        Pull Xero Invoices (ACCREC type) -> Celerp documents.

        Mapping:
          Invoice.InvoiceNumber  -> doc.ref_id
          Invoice.Contact.Name   -> customer_name
          Invoice.LineItems      -> doc line_items
          Invoice.Status         -> doc.status (PAID->paid, AUTHORISED->final, DRAFT->draft)
          Invoice.InvoiceID      -> idempotency_key
        """
        result = SyncResult(entity=SyncEntity.ORDERS)
        errors: list[str] = []

        try:
            invoices = await self._paginate(ctx, "/Invoices", "Invoices", since=since)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for inv in invoices:
            if inv.get("Type") != "ACCREC":
                result.skipped += 1
                continue
            try:
                result.record(await _upsert.upsert_invoice_from_xero(ctx.company_id, inv))
            except Exception as exc:
                errors.append(f"Invoice {inv.get('InvoiceNumber')}: {exc}")

        result.errors = errors or None
        log.info(
            "xero.sync_orders company=%s created=%d skipped=%d errors=%d",
            ctx.company_id, result.created, result.skipped, len(errors),
        )
        return result

    # -- Contacts --------------------------------------------------------------

    async def sync_contacts(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult:
        """Pull Xero Contacts -> Celerp CRM contacts."""
        result = SyncResult(entity=SyncEntity.CONTACTS)
        errors: list[str] = []

        try:
            contacts = await self._paginate(ctx, "/Contacts", "Contacts", since=since)
        except httpx.HTTPStatusError as exc:
            result.errors = [f"Xero API error: {exc}"]
            return result

        for contact in contacts:
            try:
                result.record(await _upsert.upsert_contact_from_xero(ctx.company_id, contact))
            except Exception as exc:
                errors.append(f"Contact {contact.get('ContactID')}: {exc}")

        result.errors = errors or None
        return result

    # -- Outbound: Invoices push -----------------------------------------------

    async def sync_invoices_out(self, ctx: ConnectorContext) -> SyncResult:
        """Push Celerp invoices -> Xero (outbound). Each invoice is queued and
        delivered through the outbound queue, so an interrupted push resumes as
        the same Xero create."""
        from celerp.connectors.outbound_queue import enqueue_outbound, process_outbound_identity

        result = SyncResult(entity=SyncEntity.INVOICES, direction=SyncDirection.OUTBOUND)
        errors: list[str] = []

        try:
            invoices = await _upsert.list_unsynced_invoices(ctx.company_id, platform="xero")
        except Exception as exc:
            result.errors = [f"Failed to load invoices: {exc}"]
            return result

        for inv in invoices:
            doc_id = str(inv["entity_id"])
            try:
                await enqueue_outbound(str(ctx.company_id), self.name, "invoice", doc_id)
                outcome = await process_outbound_identity(
                    str(ctx.company_id), self.name, "invoice", doc_id, ctx=ctx
                )
            except Exception as exc:
                errors.append(f"Invoice {inv.get('ref_id')}: {exc}")
                continue
            if outcome.error:
                errors.append(f"Invoice {inv.get('ref_id')}: {outcome.error}")
            elif outcome.result is not None:
                result.created += outcome.result.created
                result.skipped += outcome.result.skipped

        result.errors = errors or None
        log.info(
            "xero.sync_invoices_out company=%s created=%d errors=%d",
            ctx.company_id, result.created, len(errors),
        )
        return result

    async def sync_invoice_identity_out(
        self, ctx: ConnectorContext, operation: OutboundOperation
    ) -> SyncResult:
        """Create one Celerp invoice in Xero, exactly once.

        The request, its invoice number and an Idempotency-Key are committed
        before the first call. From then on the operation only ever re-sends
        that request: with the same key while Xero remembers it, and otherwise
        after looking the number up in Xero and finding nothing. Local edits
        made meanwhile do not change what is sent.
        """
        from celerp.connectors.outbound_queue import (
            OutboundNeedsReconciliation,
            OutboundRejected,
        )

        result = SyncResult(entity=SyncEntity.INVOICES, direction=SyncDirection.OUTBOUND)
        doc = await _upsert.invoice_for_push(ctx.company_id, operation.identity, "xero")
        if doc is None or doc["pushed_id"]:
            result.skipped = 1
            return result

        state = operation.state
        if not state:
            if doc["imported"]:
                result.skipped = 1
                return result
            if not doc["ref_id"]:
                raise OutboundRejected("It has no invoice number.")
            state = _new_attempt({
                "request": _invoice_request(doc),
                "invoice_number": doc["ref_id"],
            })
            await operation.save(state)

        sent = state["request"]["Invoices"][0]
        number = state["invoice_number"]
        async with RateLimitedClient(timeout=_RELAY_TIMEOUT_S) as client:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(state["attempted_at"])
            if state.get("held") or age >= _KEY_WINDOW:
                found = await client.get(
                    f"{_api_base()}/Invoices",
                    headers=_headers(),
                    params={
                        "where": f'Type=="ACCREC" AND InvoiceNumber=="{_where_literal(number)}"',
                        # Xero includes line items only in paged responses.
                        "page": 1,
                    },
                )
                found.raise_for_status()
                matches = (found.json() or {}).get("Invoices") or []
                if len(matches) == 1 and _is_sent_invoice(matches[0], sent):
                    await _upsert.mark_doc_pushed(
                        ctx.company_id, operation.identity, "xero", matches[0]["InvoiceID"]
                    )
                    result.created = 1
                    return result
                if matches:
                    await operation.save({**state, "held": True})
                    raise OutboundNeedsReconciliation(
                        f"Xero has an invoice numbered {number} that does not match the one "
                        "Celerp sent. Correct it in Xero, then sync again."
                    )
                if state.get("held"):
                    # Once a conflict was seen, whether Celerp's invoice reached
                    # Xero is unknown, so it is never sent again.
                    raise OutboundNeedsReconciliation(
                        f"Xero has no invoice numbered {number}. Create it in Xero to match "
                        "this invoice, then sync again."
                    )
                state = _new_attempt(state)
                await operation.save(state)

            resp = await client.put(
                f"{_api_base()}/Invoices",
                headers={**_headers(), "Idempotency-Key": state["idempotency_key"]},
                json=state["request"],
            )
        if resp.status_code == 400:
            raise OutboundRejected(_validation_message(resp))
        resp.raise_for_status()
        xero_id = ((resp.json() or {}).get("Invoices") or [{}])[0].get("InvoiceID")
        if not xero_id:
            raise RuntimeError("Xero did not confirm the invoice.")
        await _upsert.mark_doc_pushed(ctx.company_id, operation.identity, "xero", xero_id)
        result.created = 1
        return result
