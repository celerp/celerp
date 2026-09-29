# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Overdue is one definition: the Overdue card on /docs/summary counts exactly the documents
the overdue list filter returns, per document type. A document is overdue when its due date is
before today and it still represents an unresolved obligation: an unpaid balance on an invoice
or bill, live goods out on a memo or in on a consignment."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.projections import Projection

from test_helpers import make_authed_token

_PAST, _TODAY, _FUTURE = -3, 0, 3

# (doc_type, status, amount_outstanding, due offset in days, overdue?); a None balance is a
# document with no recorded balance, which still owes its total.
_CASES = [
    ("invoice", "final", 50, _PAST, True),
    ("invoice", "sent", 50, _PAST, True),
    ("invoice", "awaiting_payment", 50, _PAST, True),
    ("invoice", "partial", 20, _PAST, True),
    ("invoice", "paid", 0, _PAST, False),
    ("invoice", "final", 0, _PAST, False),
    ("invoice", "partial", 0.004, _PAST, False),
    ("invoice", "final", None, _PAST, True),
    ("invoice", "draft", 50, _PAST, False),
    ("invoice", "void", 50, _PAST, False),
    ("invoice", "final", 50, _TODAY, False),
    ("invoice", "final", 50, _FUTURE, False),
    ("bill", "awaiting_payment", 50, _PAST, True),
    ("bill", "partial", 20, _PAST, True),
    ("bill", "final", 50, _PAST, True),
    ("bill", "received", 50, _PAST, True),
    ("bill", "partially_received", 50, _PAST, True),
    ("bill", "paid", 0, _PAST, False),
    ("bill", "awaiting_payment", 0, _PAST, False),
    ("bill", "void", 50, _PAST, False),
    ("bill", "awaiting_payment", 50, _TODAY, False),
    ("memo", "sent", 50, _PAST, True),
    ("memo", "final", 50, _PAST, True),
    ("memo", "partial_returned", 50, _PAST, True),
    ("memo", "closed", 50, _PAST, False),
    ("memo", "converted", 50, _PAST, False),
    ("memo", "returned", 50, _PAST, False),
    ("memo", "void", 50, _PAST, False),
    ("memo", "draft", 50, _PAST, False),
    ("memo", "sent", 50, _FUTURE, False),
    ("consignment_in", "final", 50, _PAST, True),
    ("consignment_in", "received", 50, _PAST, True),
    ("consignment_in", "partially_received", 50, _PAST, True),
    ("consignment_in", "partial_returned", 50, _PAST, True),
    ("consignment_in", "returned", 50, _PAST, False),
    ("consignment_in", "closed", 50, _PAST, False),
    ("consignment_in", "converted", 50, _PAST, False),
    ("consignment_in", "void", 50, _PAST, False),
    ("consignment_in", "draft", 50, _PAST, False),
    ("consignment_in", "received", 50, _TODAY, False),
    ("quotation", "sent", 50, _PAST, False),
    ("purchase_order", "final", 50, _PAST, False),
]


async def _seed(session) -> tuple[dict, dict[str, set[str]]]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    session.add(Company(id=company_id, name="OverdueCo", slug=f"od-{company_id.hex[:8]}"))
    session.add(User(id=user_id, email=f"admin-{uuid.uuid4().hex[:8]}", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    expected: dict[str, set[str]] = {}
    today = date.today()
    for i, (doc_type, status, outstanding, due, overdue) in enumerate(_CASES):
        entity_id = f"doc:od-{i:02d}"
        session.add(Projection(
            company_id=company_id, entity_id=entity_id, entity_type="doc", version=1,
            updated_at=datetime.now(timezone.utc),
            state={
                "doc_type": doc_type, "status": status, "doc_number": f"OD-{i:02d}", "currency": "USD",
                "total": 50, "due_date": (today + timedelta(days=due)).isoformat(), "line_items": [],
            } | ({} if outstanding is None else {"amount_outstanding": outstanding, "amount_paid": 50 - outstanding}),
        ))
        expected.setdefault(doc_type, set())
        if overdue:
            expected[doc_type].add(entity_id)
    await session.commit()
    token = await make_authed_token(session, str(user_id), str(company_id), "admin")
    return {"Authorization": f"Bearer {token}"}, expected


@pytest.mark.asyncio
async def test_overdue_card_and_overdue_list_agree_per_doc_type(client, session):
    headers, expected = await _seed(session)
    for doc_type, want in expected.items():
        listed = await client.get(f"/docs?doc_type={doc_type}&overdue_only=1", headers=headers)
        assert listed.status_code == 200, listed.text
        got = {x["id"] for x in listed.json()["items"]}
        assert got == want, f"{doc_type}: overdue list {sorted(got)} != expected {sorted(want)}"
        summary = await client.get(f"/docs/summary?doc_type={doc_type}", headers=headers)
        assert summary.status_code == 200, summary.text
        assert summary.json()["overdue_count"] == len(want), f"{doc_type}: card {summary.json()['overdue_count']} != {len(want)}"


@pytest.mark.asyncio
async def test_overdue_card_counts_what_the_unfiltered_overdue_list_shows(client, session):
    headers, expected = await _seed(session)
    want = set().union(*expected.values())
    listed = await client.get("/docs?overdue_only=1", headers=headers)
    assert {x["id"] for x in listed.json()["items"]} == want
    assert (await client.get("/docs/summary", headers=headers)).json()["overdue_count"] == len(want)
