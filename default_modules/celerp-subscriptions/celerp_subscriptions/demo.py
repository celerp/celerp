# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Sample agreements for a demo company of a business type that bills by agreement: a
consulting firm's monthly and quarterly retainers, a nonprofit's annual and monthly
memberships, a SaaS company's seat subscriptions (one still in its trial), so the Retainers,
Members and Subscribers pages have something to show.

Seeded once per agreement, only while the company keeps no books of its own, and kept in the
books' currency until then. Each is an ordinary agreement created as a demo record and
activated as Activate does; it bills nothing until someone bills it.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import NamedTuple

from celerp.services.money import books_currency

class _Sample(NamedTuple):
    key: str
    kind: str
    customer: str
    name: str
    price: str
    frequency: str
    grace_days: int | None = None
    quantity: int = 1
    trial_days: int | None = None  # its trial ends this many days after seeding


_AGREEMENTS: dict[str, tuple[_Sample, ...]] = {
    "consulting": (
        _Sample("advisory", "retainer", "[DEMO] Harbor Logistics", "Monthly advisory retainer", "2500", "monthly"),
        _Sample("strategy", "retainer", "[DEMO] Northwind Foods", "Quarterly strategy review", "6000", "quarterly"),
    ),
    "nonprofit": (
        _Sample("individual", "membership", "[DEMO] Alex Morgan", "Annual membership - Individual", "60", "annually", 30),
        _Sample("family", "membership", "[DEMO] Sam Rivera", "Annual membership - Family", "120", "annually", 30),
        _Sample("supporter", "membership", "[DEMO] Jordan Lee", "Monthly supporter membership", "10", "monthly", 14),
    ),
    "saas": (
        _Sample("starter", "subscription", "[DEMO] Brightline Studio", "Starter plan - monthly", "29", "monthly",
                quantity=5),
        _Sample("professional", "subscription", "[DEMO] Copperfield Analytics", "Professional plan - annual", "990",
                "annually", quantity=12),
        _Sample("api", "subscription", "[DEMO] Lumen Health", "API add-on - monthly", "49", "monthly",
                trial_days=14),
    ),
}


def _key(vertical: str, key: str) -> str:
    return f"subs:demo:{vertical}:{key}"


async def seed_demo_agreements(session, company_id, user_id) -> None:
    """Add the business type's sample agreements, activated, unless the company keeps books of
    its own; one already seeded is only kept in the books' currency. Runs inside the caller's
    transaction."""
    from celerp.events.engine import emit_event, find_event_by_idempotency
    from celerp.services.business_time import business_today
    from celerp.services.company_lock import locked_company
    from celerp.services.demo import DEMO_SOURCE, follow_books_currency, has_own_books
    from celerp_docs.doc_money import document_money
    from celerp_docs.sequences import claim_number, draft_counter
    from celerp_subscriptions.routes import activate_agreement

    if await has_own_books(session, company_id):
        return
    company = await locked_company(session, company_id)
    settings = company.settings or {}
    vertical = settings.get("vertical") or ""
    currency = books_currency(settings)
    today = business_today(settings.get("timezone"))
    start = today.replace(day=1)
    actor = SimpleNamespace(id=user_id)
    seeded = []
    for sample in _AGREEMENTS.get(vertical, ()):
        idem = _key(vertical, sample.key)
        if await find_event_by_idempotency(session, company_id, idem) is not None:
            seeded.append(idem)
            continue
        line = {"line_id": str(uuid.uuid4()), "name": sample.name, "description": sample.name,
                "quantity": sample.quantity, "unit_price": float(sample.price)}
        money = document_money({}, [line], currency, keep_unrated_tax=True)
        ref = await claim_number(session, company, draft_counter("subscription_invoice"))
        doc_id = f"doc:{ref}"
        data = {"doc_type": "subscription_invoice", "ref_id": ref, "contact_name": sample.customer,
                "line_items": [line], "currency": currency, **money, "amount_paid": 0.0,
                "amount_outstanding": money["total"], "issue_date": start.isoformat(),
                "start_date": start.isoformat(), "frequency": sample.frequency, "kind": sample.kind}
        if sample.grace_days is not None:
            data["grace_days"] = sample.grace_days
        if sample.trial_days is not None:
            data["trial_end"] = (today + timedelta(days=sample.trial_days)).isoformat()
        await emit_event(session, company_id=company_id, entity_id=doc_id, entity_type="doc",
                         event_type="doc.created", data=data, actor_id=user_id, location_id=None,
                         source=DEMO_SOURCE, idempotency_key=idem)
        await session.flush()
        await activate_agreement(session, company_id, doc_id, actor)
        await session.flush()
    await follow_books_currency(session, company_id, seeded, currency)


async def seed_demo_agreements_hook(*, session, company_id, user_id) -> None:
    """The demo_records slot: called whenever a company's demo data is seeded."""
    await seed_demo_agreements(session, company_id, user_id)
