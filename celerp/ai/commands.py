# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI command parsing and execution - structured bill creation from LLM output.

Two-phase flow:
  1. parse_bill_commands() - validate LLM JSON, return structured data
  2. create_bills() - execute against event store (only after user confirms)
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid

from pydantic import BaseModel, field_validator
from sqlalchemy import cast, select, String
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


# -- Pydantic models -------------------------------------------------------

class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("quantity must be > 0")
        return v

    @field_validator("unit_price")
    @classmethod
    def price_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("unit_price must be >= 0")
        return v


class DraftBill(BaseModel):
    vendor_name: str
    date: str
    total: float
    source_file_id: str | None = None
    line_items: list[LineItem]


# -- Phase 1: Parse + validate ---------------------------------------------

def parse_bill_commands(raw_json: dict) -> list[DraftBill]:
    """Validate LLM JSON output into structured bill data.

    Raises ValueError with details on validation failure.
    """
    bills_data = raw_json.get("create_draft_bills", [])
    if not bills_data:
        return []
    bills: list[DraftBill] = []
    for i, entry in enumerate(bills_data):
        try:
            bills.append(DraftBill.model_validate(entry))
        except Exception as exc:
            raise ValueError(f"Bill #{i + 1} validation failed: {exc}") from exc
    return bills


# -- Phase 2: Execute (after user confirmation) ----------------------------

async def create_bills(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    bills: list[DraftBill],
) -> str:
    """Create draft bills and contacts in the event store. Returns feedback text."""
    from celerp.events.engine import emit_event
    from celerp.models.company import Company
    from celerp.models.projections import Projection

    company = await session.get(Company, company_id)
    if not company:
        return ""

    feedback_lines: list[str] = []
    currency = company.settings.get("currency", "USD")

    for bill in bills:
        # Find vendor in projection
        vendor_row = (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "contact",
                cast(Projection.state["name"], String).ilike(f"%{bill.vendor_name}%"),
            )
        )).scalars().first()

        if vendor_row:
            contact_id = str(vendor_row.entity_id)
        else:
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", bill.vendor_name.lower()) or "vendor"
            ref_id = f"{slug}-{str(uuid.uuid4())[:4]}"
            contact_id = f"contact:{ref_id}"
            idem_key = _idempotency_key(company_id, "contact", bill.vendor_name, bill.date)
            await emit_event(
                session,
                company_id=company_id,
                entity_id=contact_id,
                entity_type="contact",
                event_type="contact.created",
                data={
                    "name": bill.vendor_name,
                    "contact_type": "vendor",
                    "status": "draft",
                    "ref_id": ref_id,
                    "currency": currency,
                },
                actor_id=user_id,
                location_id=None,
                source="ai",
                idempotency_key=idem_key,
                metadata_={},
            )

        try:
            from celerp_docs.sequences import next_doc_ref
            bill_ref = next_doc_ref(company, "bill")
        except ImportError:
            bill_ref = f"BIL-{str(uuid.uuid4())[:6].upper()}"

        entity_id = f"doc:{bill_ref}"
        line_items = [
            {
                "description": li.description,
                "quantity": li.quantity,
                "unit_price": li.unit_price,
                "line_total": li.quantity * li.unit_price,
            }
            for li in bill.line_items
        ]
        idem_key = _idempotency_key(company_id, "bill", bill.vendor_name, bill.date, str(bill.total))

        await emit_event(
            session,
            company_id=company_id,
            entity_id=entity_id,
            entity_type="doc",
            event_type="doc.created",
            data={
                "doc_type": "bill",
                "status": "draft",
                "ref_id": bill_ref,
                "date": bill.date,
                "contact_id": contact_id,
                "location_id": "loc:default",
                "total": bill.total,
                "subtotal": bill.total,
                "currency": currency,
                "amount_outstanding": bill.total,
                "line_items": line_items,
            },
            actor_id=user_id,
            location_id=None,
            source="ai",
            idempotency_key=idem_key,
            metadata_={"ai_source_file_id": bill.source_file_id} if bill.source_file_id else {},
        )
        feedback_lines.append(
            f"Created Draft Bill {bill_ref} for {bill.vendor_name} ({currency} {bill.total:.2f})"
        )

    return "\n".join(feedback_lines)


def _idempotency_key(company_id: uuid.UUID, entity: str, *parts: str) -> str:
    """Deterministic idempotency key from company + entity + parts."""
    raw = f"{company_id}:{entity}:" + ":".join(parts)
    return f"ai_{entity}_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
