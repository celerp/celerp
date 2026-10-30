# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Records a connected company sends through the receiving company's own import routes
(peer_access.PEER_ROUTES). Each record is applied in its own transaction, so one record
that needs attention never holds back the others."""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import one_unit
from celerp.importers.results import failure_reason
from celerp.services import peer_access, peer_attention

log = logging.getLogger(__name__)

PEER_ONLY = "Only a connected company's integration key may send a source revision."
UNEXPECTED = "The receiving company could not apply this record. Its owner can see why in the server log."
# The field every import record model carries for a connected company's records.
SourceRevision = Annotated[int | None, Field(ge=1, le=2**63 - 1)]
class ContactData(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    billing_address: str | None = Field(default=None, max_length=4096)
    shipping_address: str | None = Field(default=None, max_length=4096)
    currency: str | None = Field(default=None, max_length=3)
    contact_type: str = Field(default="customer", min_length=1, max_length=32)


class ProductData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    sku: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=20000)
    sell_by: str = Field(default="piece", min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=255)
    barcode: str | None = Field(default=None, max_length=255)
    retail_price: FiniteFloat | None = Field(default=None, ge=0)
    wholesale_price: FiniteFloat | None = Field(default=None, ge=0)


class StockCountItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=512)
    counted_qty: FiniteFloat = Field(ge=0)


class StockCountData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: uuid.UUID
    location_name: str = Field(min_length=1, max_length=255)
    location_type: str = Field(min_length=1, max_length=64)
    source_closed_event_id: StrictInt = Field(ge=1, le=2**63 - 1)
    counts: list[StockCountItem] = Field(min_length=1, max_length=1000)


def refuse_source_revision(records) -> None:
    """A signed-in user's import is never treated as a connected company's."""
    if any(record.source_revision is not None for record in records):
        raise HTTPException(422, PEER_ONLY)


async def _contact(session, **kw) -> str:
    from celerp.services.peer_identity import receive_contact
    kw["data"] = ContactData.model_validate(kw["data"]).model_dump()
    return await receive_contact(session, **kw)


async def _product(session, **kw) -> str:
    """A parcel a receipt created (``received_on`` names its document) is first written by
    that document's receipt, which carries the parcel's current data; until then it is
    skipped here, so it is never created twice."""
    from celerp.services.peer_identity import peer_identity, receive_product
    data = dict(kw["data"]) if isinstance(kw["data"], dict) else kw["data"]
    received_on = data.pop("received_on", None) if isinstance(data, dict) else None
    grant = kw["grant"]
    if received_on is not None and await peer_identity(
            session, target_company=grant.company_id, source_company=grant.source_company_id,
            entity_type="item", source_entity_id=kw["source_entity_id"]) is None:
        return "skipped"
    kw["data"] = ProductData.model_validate(data).model_dump()
    return await receive_product(session, **kw)


async def _document(session, **kw) -> str:
    from celerp.services.peer_documents import receive_document
    return await receive_document(session, **kw)


async def _stock_count(session, **kw) -> str:
    from celerp.services.peer_inventory import receive_stock_audit
    kw["data"] = StockCountData.model_validate(kw["data"]).model_dump(mode="json")
    return await receive_stock_audit(session, **kw)


async def _journal(session, **kw) -> str:
    from celerp.services.peer_journals import receive_journal
    return await receive_journal(session, **kw)


RECEIVERS = {"contacts": _contact, "products": _product, "documents": _document,
             "inventory": _stock_count, "journals": _journal}


async def peer_batch(ctx, session: AsyncSession, records) -> dict | None:
    """An import route's records sent with a connected company's key, applied: the route's
    result, or None for a signed-in user's import, which then runs as usual."""
    if ctx.peer is None:
        refuse_source_revision(records)
        return None
    return await receive_records(session, ctx.peer, records)


async def receive_file(ctx, session: AsyncSession, source_doc_id: str, file,
                       **fields) -> dict | None:
    """A document file sent with a connected company's key, recorded once under the sender's
    document and file ids: ``{"status": ...}``, or None for a signed-in user's upload, which
    may not name those fields."""
    if ctx.peer is None:
        if any(value is not None for value in fields.values()):
            raise HTTPException(422, PEER_ONLY)
        return None
    from celerp.services.peer_attachments import receive_attachment
    if fields["source_file_id"] is None or fields["sha256_hex"] is None:
        raise HTTPException(422, "A connected company's file needs its file id and checksum.")
    grant = await _grant(session, ctx.peer)
    peer_access.mark_used(grant)
    status = await receive_attachment(
        session, grant=grant, source_doc_id=source_doc_id, file=file, **fields)
    return {"status": status}


async def _grant(session: AsyncSession, peer: peer_access.PeerCaller):
    return await peer_access.authenticate(
        session, peer.credential, source_company_id=peer.source_company_id, scope=peer.scope)


def _label(record) -> str:
    from celerp.connectors.celerp import attention_label
    return attention_label(record.data if isinstance(record.data, dict) else {}, record.entity_id)


async def receive_records(session: AsyncSession, peer: peer_access.PeerCaller, records) -> dict:
    """Apply each record, authenticating the key again in the record's own transaction.
    Answers the import routes' result: created, updated, skipped, and one error per record
    that was not applied, naming the sender's record and the reason."""
    receive = RECEIVERS[peer.scope]
    counts = {"created": 0, "updated": 0, "skipped": 0}
    errors: list[str] = []
    grant = await _grant(session, peer)
    peer_access.mark_used(grant)
    await session.commit()
    where = dict(company_id=grant.company_id, source_company_id=peer.source_company_id,
                 entity_type=peer.scope)
    for record in records:
        try:
            if record.source_revision is None:
                raise HTTPException(422, "A connected company's record needs its source revision.")
            grant = await _grant(session, peer)
            with one_unit(session):  # a record applies whole or not at all
                status = await receive(session, grant=grant, source_entity_id=record.entity_id,
                                       source_revision=record.source_revision, data=record.data)
                await peer_attention.clear(session, **where, source_entity_id=record.entity_id)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            if isinstance(exc, HTTPException) and exc.status_code == 401:
                raise  # the key stopped working mid-batch; nothing further is applied
            if isinstance(exc, (HTTPException, ValueError)):
                reason = failure_reason(exc)
            else:
                log.exception("connected company record %s failed", record.entity_id)
                reason = UNEXPECTED
            errors.append(f"{record.entity_id}: {reason}")
            # The rollback released the company lock: the key is judged again under it
            # before the note is written, and a key that stopped working stops the batch.
            await _grant(session, peer)
            await peer_attention.record(session, **where, source_entity_id=record.entity_id,
                                        label=_label(record), reason=reason,
                                        fix=getattr(exc, "fix", None))
            await session.commit()
            continue
        counts[status] += 1
    return {**counts, "errors": errors}
