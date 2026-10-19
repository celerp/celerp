# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Mirror source document payments through the canonical accounting lifecycle."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from celerp.events.engine import find_event_by_idempotency
from celerp.models.projections import Projection
from celerp.services.money import round_money, to_decimal

# What a peer copy leaves to the person keeping the receiving company's books.
BY_HAND = "{} is not copied automatically. Enter it in the accountant's copy by hand."


def _payment_key(source_company, doc_id: str, index: int) -> str:
    raw = f"{source_company}:{doc_id}:{index}"
    return "peer:payment:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _same_payment(target: dict, source: dict, currency: str) -> bool:
    try:
        money_equal = round_money(target.get("amount"), currency) == round_money(source.get("amount"), currency)
        source_rate = source.get("conversion_rate")
        target_rate = target.get("conversion_rate")
        rate_equal = (source_rate is None and target_rate is None) or (
            source_rate is not None and target_rate is not None
            and to_decimal(source_rate) == to_decimal(target_rate)
        )
    except (ValueError, ArithmeticError, TypeError):
        return False
    return (money_equal and rate_equal and target.get("status", "active") == "active"
            and all((target.get(k) or None) == (source.get(k) or None)
                    for k in ("payment_date", "bank_account", "reference", "method", "currency"))
            and to_decimal(target.get("refunded") or 0) >= 0)


def validate_source_payments(data: dict) -> list[tuple[int, dict]]:
    """Validate before any importer writes, including source/status consistency."""
    from celerp_docs.routes import DocPaymentBody

    source_status = data.get("status", "draft")
    incoming = data.get("payments", [])
    if not isinstance(incoming, list) or len(incoming) > 1000:
        raise HTTPException(422, "Invalid source payments")
    currency = str(data.get("currency") or "USD").upper()
    out = []
    seen: set[int] = set()
    for payment in incoming:
        if not isinstance(payment, dict):
            raise HTTPException(422, "Invalid source payment")
        idx = payment.get("index")
        if type(idx) is not int or idx < 0 or idx in seen:
            raise HTTPException(422, "Invalid source payment identity")
        seen.add(idx)
        try:
            refunded = round_money(payment.get("refunded") or 0, currency)
        except (ValueError, ArithmeticError, TypeError) as exc:
            raise HTTPException(422, "Invalid source refund") from exc
        if payment.get("status", "active") != "active":
            raise HTTPException(409, "Reversed source payments require reconciliation")
        if refunded < 0 or refunded > round_money(payment.get("amount") or 0, currency):
            raise HTTPException(409, "Source payment refund is inconsistent")
        if refunded and not data.get("payment_events"):
            raise HTTPException(409, "Refund history required for booked payments")
        if payment.get("source_doc_id") or payment.get("target_doc_id"):
            raise HTTPException(409, BY_HAND.format("A credit note applied to a document"))
        if not payment.get("bank_account"):
            raise HTTPException(409, "Source payment has no deposit account")
        fields = ("amount", "payment_date", "currency", "method", "reference",
                  "bank_account", "conversion_rate")
        selected = {k: payment[k] for k in fields if k in payment and payment[k] is not None}
        selected.setdefault("currency", currency)
        try:
            parsed = DocPaymentBody.model_validate(selected)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid source payment") from exc
        if not parsed.payment_date or parsed.amount <= 0 or parsed.currency.upper() != currency:
            raise HTTPException(409, "Source payment cannot be reconciled")
        out.append((idx, parsed.model_dump(exclude_none=True)))
    if source_status in ("draft", "void") and out:
        raise HTTPException(409, "Unissued document has source payments")
    if source_status in ("paid", "partial") and not out:
        raise HTTPException(409, "Settled source document has no transferable payments")
    if "amount_paid" not in data:
        if out or source_status in ("paid", "partial"):
            raise HTTPException(409, "Source paid balance unavailable")
    else:
        try:
            source_paid = round_money(data["amount_paid"], currency)
        except (ValueError, ArithmeticError, TypeError) as exc:
            raise HTTPException(422, "Invalid source paid balance") from exc
        refunded_by_index = {
            p["index"]: round_money(p.get("refunded") or 0, currency)
            for p in incoming
        }
        total = sum((to_decimal(p["amount"]) - refunded_by_index[i]
                     for i, p in out), Decimal(0))
        if round_money(total, currency) != source_paid:
            raise HTTPException(409, "Source paid balance requires reconciliation")
        # A fully refunded receipt may remain in history after the paper returns
        # to an unpaid state. A positive settled balance may not.
        if source_status not in ("paid", "partial") and total != 0:
            raise HTTPException(409, "Source status conflicts with booked receipts")
        if source_status in ("paid", "partial") and source_paid <= 0:
            raise HTTPException(409, "Settled status has no outstanding receipt")
    return out


def validate_source_refunds(data: dict) -> list[dict]:
    """Validate a complete, ordered refund/reversal history before writing books."""
    events = data.get("payment_events") or []
    if not isinstance(events, list) or len(events) > 1000:
        raise HTTPException(422, "Invalid source refund events")
    previous_id = 0
    original: dict[tuple[int, int], Decimal] = {}
    reversed_events: set[tuple[int, int]] = set()
    result = []
    currency = data.get("currency") or "USD"
    for record in events:
        if not isinstance(record, dict):
            raise HTTPException(422, "Invalid source refund event")
        kind = record.get("kind", "refunded")
        common = {"event_id", "payment_index", "refund_number", "amount"}
        when_key = "reversal_date" if kind == "reversed" else "refund_date"
        if (kind not in ("refunded", "reversed")
                or set(record) != (common | {when_key} | ({"kind"} if "kind" in record else set()))):
            raise HTTPException(422, "Invalid source refund event")
        eid = record.get("event_id")
        index = record.get("payment_index")
        number = record.get("refund_number")
        if (type(eid) is not int or eid <= previous_id
                or type(index) is not int or index < 0
                or type(number) is not int or number < 0):
            raise HTTPException(422, "Invalid source refund identity")
        try:
            day = date.fromisoformat(record[when_key])
            amount = round_money(record["amount"], currency)
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise HTTPException(422, "Invalid source refund amount or date") from exc
        if amount <= 0 or day.isoformat() != record[when_key]:
            raise HTTPException(422, "Invalid source refund amount or date")
        identity = (index, number)
        if kind == "refunded":
            if identity in original:
                raise HTTPException(409, "Duplicate source refund identity")
            original[identity] = amount
        else:
            if (identity not in original or identity in reversed_events
                    or amount != original[identity]):
                raise HTTPException(409, "Source refund reversal is inconsistent")
            reversed_events.add(identity)
        previous_id = eid
        result.append(record)
    return result


async def apply_source_payments(
    session, *, grant, source_entity_id: str, target_doc_id: str,
    payments: list[tuple[int, dict]], actor_id, source_status: str,
    refunds: list[dict] | None = None,
    expected_refunded: dict[int, object] | None = None,
) -> None:
    """Record cash with normal document locks, accounts, rates, journals and replay keys."""
    from celerp_docs.routes import apply_doc_payment

    for source_index, payload in payments:
        key = _payment_key(grant.source_company_id, source_entity_id, source_index)
        digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prior = await find_event_by_idempotency(session, grant.company_id, key)
        if prior is not None:
            if (prior.entity_type != "doc" or prior.entity_id != target_doc_id
                    or prior.event_type != "doc.payment.received"
                    or (prior.metadata_ or {}).get("request") != digest):
                raise HTTPException(409, "Source payment replay conflicts with existing books")
            continue
        row = await session.get(
            Projection, (grant.company_id, target_doc_id), populate_existing=True)
        if row is None or row.entity_type != "doc":
            raise HTTPException(409, "Document missing for source payment")
        stored = next((p for p in (row.state or {}).get("payments", [])
                       if p.get("index") == source_index), None)
        if stored is not None:
            if not _same_payment(stored, payload, str(row.state.get("currency") or "USD")):
                raise HTTPException(409, "Source payment conflicts with an existing receipt")
            continue
        await apply_doc_payment(
            session, str(grant.company_id), target_doc_id, dict(payload),
            source="connector", actor_id=actor_id, idempotency_key=key,
            request=digest, commit=False)
    if refunds:
        from celerp.services.peer_refunds import replay_source_refunds
        await replay_source_refunds(
            session, grant=grant, source_doc_id=source_entity_id,
            target_doc_id=target_doc_id, refunds=refunds, actor_id=actor_id)
    if expected_refunded:
        for index, expected in expected_refunded.items():
            receipt_key = _payment_key(grant.source_company_id, source_entity_id, index)
            receipt = await find_event_by_idempotency(session, grant.company_id, receipt_key)
            dest_index = receipt.data["index"] if receipt is not None else index
            target = await session.get(
                Projection, (grant.company_id, target_doc_id), populate_existing=True)
            actual = next((p for p in (target.state or {}).get("payments", [])
                           if p.get("index") == dest_index), None) if target else None
            if actual is None or round_money(
                    actual.get("refunded") or 0, target.state.get("currency") or "USD"
            ) != round_money(expected, target.state.get("currency") or "USD"):
                raise HTTPException(409, "Source refund balance requires reconciliation")
    if source_status in ("partial", "paid"):
        row = await session.get(
            Projection, (grant.company_id, target_doc_id), populate_existing=True)
        if row is None or row.state.get("status") != source_status:
            raise HTTPException(409, "Source payment balance requires reconciliation")
