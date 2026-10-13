# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Mirror source document payments through the canonical accounting lifecycle."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from celerp.events.engine import find_event_by_idempotency
from celerp.models.projections import Projection
from celerp.services.money import round_money, to_decimal


def _payment_key(source_company, doc_id: str, index: int) -> str:
    raw = f"{source_company}:{doc_id}:{index}"
    return "peer:payment:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _same_payment(target: dict, source: dict, currency: str) -> bool:
    try:
        money_equal = round_money(target.get("amount"), currency) == round_money(source.get("amount"), currency)
    except (ValueError, ArithmeticError, TypeError):
        return False
    return (money_equal and target.get("status", "active") == "active"
            and all((target.get(k) or None) == (source.get(k) or None)
                    for k in ("payment_date", "bank_account", "reference", "method", "currency"))
            and to_decimal(target.get("refunded") or 0) == 0)


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
        if payment.get("status", "active") != "active" or refunded != 0:
            raise HTTPException(409, "Reversed source payments require reconciliation")
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
    if source_status in ("paid", "partial"):
        if "amount_paid" not in data:
            raise HTTPException(409, "Source paid balance unavailable")
        total = sum((to_decimal(p["amount"]) for _, p in out), Decimal(0))
        if round_money(total, currency) != round_money(data["amount_paid"], currency):
            raise HTTPException(409, "Source paid balance requires reconciliation")
    return out


async def apply_source_payments(
    session, *, grant, source_entity_id: str, target_doc_id: str,
    payments: list[tuple[int, dict]], actor_id, source_status: str,
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
    if source_status in ("partial", "paid"):
        row = await session.get(
            Projection, (grant.company_id, target_doc_id), populate_existing=True)
        if row is None or row.state.get("status") != source_status:
            raise HTTPException(409, "Source payment balance requires reconciliation")
