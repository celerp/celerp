# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Mirror independently recorded refunds through the existing document refund service."""
from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from celerp.events.engine import find_event_by_idempotency
from celerp.models.ledger import LedgerEntry
from celerp.services.money import round_money


def _proof(event: dict) -> str:
    return hashlib.sha256(json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


async def replay_source_refunds(
    session, *, grant, source_doc_id: str, target_doc_id: str,
    refunds: list[dict], actor_id,
) -> None:
    """Preserve source refund order, operation identity, account and actual date."""
    from celerp_docs.routes import _get_doc, apply_payment_refund, reverse_payment_refund, posted_books
    from celerp.services.peer_payments import _payment_key

    cid = grant.company_id
    history = (await session.scalars(select(LedgerEntry).where(
        LedgerEntry.company_id == cid,
        LedgerEntry.entity_id == target_doc_id,
        LedgerEntry.event_type.in_(("doc.payment.refunded", "doc.payment.refund_reversed")),
    ).order_by(LedgerEntry.id))).all()
    existing = {
        (entry.data.get("payment_index"), entry.data.get("refund_number")): entry
        for entry in history if entry.event_type == "doc.payment.refunded"
    }
    reversals = {
        (entry.data.get("payment_index"), entry.data.get("refund_number")): entry
        for entry in history if entry.event_type == "doc.payment.refund_reversed"
    }
    for source in refunds:
        source_index = source["payment_index"]
        receipt_key = _payment_key(grant.source_company_id, source_doc_id, source_index)
        receipt = await find_event_by_idempotency(session, cid, receipt_key)
        if receipt is not None:
            if receipt.event_type != "doc.payment.received" or receipt.entity_id != target_doc_id:
                raise HTTPException(409, "Source receipt identity is inconsistent")
            target_index = receipt.data["index"]
        else:
            target_index = source_index

        row = await _get_doc(session, cid, target_doc_id, for_update=True)
        payment = next((p for p in (row.state or {}).get("payments", [])
                        if p.get("index") == target_index), None)
        if payment is None or payment.get("status", "active") != "active":
            raise HTTPException(409, "Source refund has no active receiving payment")
        number = source["refund_number"]
        currency = row.state.get("currency") or "USD"
        amount = round_money(source["amount"], currency)
        raw = f"{grant.source_company_id}:{source_doc_id}:{source['event_id']}"
        key = "peer:refund:" + hashlib.sha256(raw.encode()).hexdigest()
        proof = _proof(source)
        prior = await find_event_by_idempotency(session, cid, key)
        reversed_event = source.get("kind", "refunded") == "reversed"
        expected_type = "doc.payment.refund_reversed" if reversed_event else "doc.payment.refunded"
        if prior is not None:
            if (prior.event_type != expected_type
                    or prior.entity_id != target_doc_id
                    or (prior.metadata_ or {}).get("request") != proof):
                raise HTTPException(409, "Source refund operation identity was reused")
            continue
        if reversed_event:
            earlier = existing.get((target_index, number))
            if (earlier is None
                    or round_money(earlier.data.get("amount"), currency) != amount):
                raise HTTPException(409, "Source refund reversal has no matching booked refund")
            prior_reversal = reversals.get((target_index, number))
            if prior_reversal is not None:
                if (round_money(prior_reversal.data.get("amount"), currency) != amount
                        or prior_reversal.data.get("reversal_date") != source["reversal_date"]):
                    raise HTTPException(409, "Restored refund reversal differs from source")
                continue
            if round_money(payment.get("refunded") or 0, currency) < amount:
                raise HTTPException(409, "Source refund reversal exceeds refunded balance")
            result = await reverse_payment_refund(
                session, cid, target_doc_id, row, payment,
                refund=earlier.data, reversal_date=source["reversal_date"],
                books=await posted_books(session, cid, target_doc_id, row, payment),
                actor_id=actor_id, source="connector", idempotency_key=key,
                metadata_={"request": proof},
            )
            reversals[(target_index, number)] = result
            continue

        count = int(payment.get("refund_count") or 0)
        if number < count:
            earlier = existing.get((target_index, number))
            if (earlier is None
                    or round_money(earlier.data.get("amount"), currency) != amount
                    or earlier.data.get("refund_date") != source["refund_date"]):
                raise HTTPException(409, "Restored refund history differs from source")
            continue
        if number != count:
            raise HTTPException(409, "Source refund events are not consecutive")
        result = await apply_payment_refund(
            session, cid, target_doc_id, row, payment,
            amount=amount, refund_date=source["refund_date"],
            books=await posted_books(session, cid, target_doc_id, row, payment),
            data={}, actor_id=actor_id, source="connector",
            idempotency_key=key, metadata_={"request": proof},
        )
        existing[(target_index, number)] = result
