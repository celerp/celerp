# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Sample asset bills for a demo company: a delivery van, three laptops, an office fit-out
still under construction and an older forklift that has been in service long enough to
sell, so the register has something to register, build and sell.

Seeded once, only while the company keeps no books of its own, and kept in the books'
currency until then. The bills are ordinary finalized bills marked as demo records, so they
never count as the company's own books.
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

from celerp.events.engine import find_event_by_idempotency
from celerp.services.business_time import business_today
from celerp.services.money import books_currency

# key, supplier, line name, quantity, unit price, months back, day of month, account code
_BILLS = (
    ("forklift", "Lift Equipment Co", "Forklift", 1, "24000", 18, 20, None),
    ("van", "City Motors", "Delivery van", 1, "32000", 2, 5, None),
    ("laptops", "Office IT Supplies", "Laptop", 3, "1450", 1, 12, None),
    ("fitout", "Shopfit Builders", "Office fit-out", 1, "18500", 0, 1, "1240"),
)


def _months_back(today: date, months: int, day: int) -> str:
    month = today.month - months
    year = today.year + (month - 1) // 12
    return date(year, (month - 1) % 12 + 1, day).isoformat()


async def seed_demo_assets(session, company_id, user_id) -> None:
    """Add the sample asset bills, finalized, unless the company keeps books of its own or
    already has them. Runs inside the caller's transaction."""
    from celerp.events.engine import emit_event
    from celerp.services.company_lock import locked_company
    from celerp.services.demo import DEMO_SOURCE, has_own_books
    from celerp_docs.doc_money import document_money
    from celerp_docs.routes import finalize_document
    from celerp_docs.sequences import next_draft_ref

    if await has_own_books(session, company_id):
        return
    company = await locked_company(session, company_id)
    currency = books_currency(company.settings or {})
    if await find_event_by_idempotency(session, company_id, "fa:demo:van") is not None:
        await _follow_books_currency(session, company_id, currency)
        return
    today = business_today(company.settings.get("timezone"))
    for key, supplier, name, quantity, price, months, day, account in _BILLS:
        line = {"line_id": str(uuid.uuid4()), "name": name, "quantity": quantity, "unit_price": float(price),
                "receive_as": "asset"}
        if account:
            line["account_code"] = account
        money = document_money({}, [line], currency, keep_unrated_tax=True)
        ref = next_draft_ref(company, "bill")
        doc_id = f"doc:{ref}"
        await emit_event(session, company_id=company_id, entity_id=doc_id, entity_type="doc",
                         event_type="doc.created", data={
                             "doc_type": "bill", "ref_id": ref, "contact_name": supplier, "line_items": [line],
                             "currency": currency, **money, "amount_outstanding": money["total"],
                             "issue_date": _months_back(today, months, day)},
                         actor_id=user_id, location_id=None, source=DEMO_SOURCE,
                         idempotency_key=f"fa:demo:{key}")
        await session.flush()
        await finalize_document(doc_id, company_id, SimpleNamespace(id=user_id), session, commit=False)
        await session.flush()


async def _follow_books_currency(session, company_id, currency: str) -> None:
    """Keep the sample bills in the books' currency. Their amounts are sample figures,
    like the sample stock's, so a currency set before the company keeps books of its own
    re-labels them; their journals already hold the same figures at a rate of one."""
    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection
    from celerp.services.demo import DEMO_SOURCE

    for key, *_ in _BILLS:
        created = await find_event_by_idempotency(session, company_id, f"fa:demo:{key}")
        if created is None:
            continue
        row = await session.get(Projection, {"company_id": company_id, "entity_id": created.entity_id})
        held = str(((row.state if row else None) or {}).get("currency") or currency).upper()
        if held == currency:
            continue
        await emit_event(session, company_id=company_id, entity_id=created.entity_id, entity_type="doc",
                         event_type="doc.updated",
                         data={"fields_changed": {"currency": {"old": held, "new": currency}}},
                         actor_id=None, location_id=None, source=DEMO_SOURCE,
                         idempotency_key=f"fa:demo:{key}:currency:{currency}:{uuid.uuid4().hex[:8]}")
    await session.flush()


async def seed_demo_assets_hook(*, session, company_id, user_id) -> None:
    """The demo_records slot: called whenever a company's demo data is seeded."""
    await seed_demo_assets(session, company_id, user_id)
