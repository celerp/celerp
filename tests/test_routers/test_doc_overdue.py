# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Overdue is one definition everywhere: the Overdue card on /docs/summary counts exactly the
documents the overdue list filter returns, per document type, and the dashboard, the aging
reports and the overdue reminder read the same documents at the same balances. A document is
overdue when its due date is before today and it still represents an unresolved obligation: an
unpaid balance on an invoice or bill, live goods out on a memo or in on a consignment.

A document with no recorded balance owes its total; a recorded 0 stays 0. Imported documents
carry their due date, total and balance under older keys, which count the same."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.projections import Projection

from test_helpers import make_authed_token

_PAST, _TODAY, _FUTURE = -3, 0, 3
_TOTAL = 50
_NULL = object()   # the balance key present with an explicit null
_LEGACY = True     # stored under payment_due_date / total_amount / outstanding_balance

# Statuses in which an invoice, bill or purchase order still awaits payment.
_AWAITING = {
    "invoice": {"final", "sent", "awaiting_payment", "partial"},
    "bill": {"final", "awaiting_payment", "partial", "received", "partially_received"},
    "purchase_order": {"sent", "final", "awaiting_payment", "partial", "received", "partially_received"},
}

# (doc_type, status, amount_outstanding, due offset in days, overdue?[, legacy keys]); a None
# balance is a document with no recorded balance, which still owes its total.
_CASES = [
    ("invoice", "final", 50, _PAST, True),
    ("invoice", "sent", 50, _PAST, True),
    ("invoice", "awaiting_payment", 50, _PAST, True),
    ("invoice", "partial", 20, _PAST, True),
    ("invoice", "paid", 0, _PAST, False),
    ("invoice", "final", 0, _PAST, False),
    ("invoice", "partial", 0.004, _PAST, False),
    ("invoice", "final", None, _PAST, True),
    ("invoice", "final", _NULL, _PAST, True),
    ("invoice", "final", 30, _PAST, True, _LEGACY),
    ("invoice", "final", 0, _PAST, False, _LEGACY),
    ("invoice", "sent", None, _PAST, True, _LEGACY),
    ("invoice", "final", 50, _FUTURE, False, _LEGACY),
    ("invoice", "draft", 50, _PAST, False),
    ("invoice", "void", 50, _PAST, False),
    ("invoice", "final", 50, _TODAY, False),
    ("invoice", "final", 50, _FUTURE, False),
    ("bill", "awaiting_payment", 50, _PAST, True),
    ("bill", "partial", 20, _PAST, True),
    ("bill", "final", 50, _PAST, True),
    ("bill", "received", 50, _PAST, True),
    ("bill", "partially_received", 50, _PAST, True),
    ("bill", "partially_received", 0, _PAST, False),
    ("bill", "awaiting_payment", 40, _PAST, True, _LEGACY),
    ("bill", "paid", 0, _PAST, False),
    ("bill", "awaiting_payment", 0, _PAST, False),
    ("bill", "void", 50, _PAST, False),
    ("bill", "awaiting_payment", 50, _TODAY, False),
    ("memo", "sent", 50, _PAST, True),
    ("memo", "sent", 50, _PAST, True, _LEGACY),
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
    ("purchase_order", "received", 45, _PAST, False),
    ("purchase_order", "converted", 50, _PAST, False),
]


def _balance(outstanding) -> float:
    return _TOTAL if outstanding is None or outstanding is _NULL else outstanding


def _state(i: int, doc_type: str, status: str, outstanding, due: int, legacy: bool) -> dict:
    due_key, total_key, balance_key = (
        ("payment_due_date", "total_amount", "outstanding_balance") if legacy else ("due_date", "total", "amount_outstanding"))
    state = {
        "doc_type": doc_type, "status": status, "doc_number": f"OD-{i:02d}", "currency": "USD",
        "contact_id": "contact:od", "line_items": [],
        total_key: _TOTAL, due_key: (date.today() + timedelta(days=due)).isoformat(),
    }
    if outstanding is not None:
        state[balance_key] = None if outstanding is _NULL else outstanding
    return state


class _Seeded:
    def __init__(self, headers: dict, company_id, rows: list[tuple[str, tuple]]):
        self.headers, self.company_id, self.rows = headers, company_id, rows

    def ids(self, doc_type: str | None = None, *, overdue: bool | None = None, awaiting: bool = False) -> set[str]:
        return {
            eid for eid, (dt, st, _o, _d, od, *_l) in self.rows
            if (doc_type is None or dt == doc_type)
            and (overdue is None or od == overdue)
            and (not awaiting or st in _AWAITING.get(dt, ()))
        }

    def balance(self, ids: set[str], *, owed_only: bool = False) -> float:
        amounts = [_balance(case[2]) for eid, case in self.rows if eid in ids]
        return sum(a for a in amounts if not owed_only or a > 0.005)


async def _seed(session) -> _Seeded:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    session.add(Company(id=company_id, name="OverdueCo", slug=f"od-{company_id.hex[:8]}"))
    session.add(User(id=user_id, email=f"admin-{uuid.uuid4().hex[:8]}", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    rows = []
    for i, case in enumerate(_CASES):
        doc_type, status, outstanding, due, _overdue, *legacy = case
        entity_id = f"doc:od-{i:02d}"
        session.add(Projection(
            company_id=company_id, entity_id=entity_id, entity_type="doc", version=1,
            updated_at=datetime.now(timezone.utc),
            state=_state(i, doc_type, status, outstanding, due, bool(legacy and legacy[0])),
        ))
        rows.append((entity_id, case))
    await session.commit()
    token = await make_authed_token(session, str(user_id), str(company_id), "admin")
    return _Seeded({"Authorization": f"Bearer {token}"}, company_id, rows)


_DOC_TYPES = ("invoice", "bill", "memo", "consignment_in", "quotation", "purchase_order")


@pytest.mark.asyncio
async def test_overdue_card_and_overdue_list_agree_per_doc_type(client, session):
    seeded = await _seed(session)
    for doc_type in _DOC_TYPES:
        want = seeded.ids(doc_type, overdue=True)
        listed = await client.get(f"/docs?doc_type={doc_type}&overdue_only=1", headers=seeded.headers)
        assert listed.status_code == 200, listed.text
        got = {x["id"] for x in listed.json()["items"]}
        assert got == want, f"{doc_type}: overdue list {sorted(got)} != expected {sorted(want)}"
        summary = await client.get(f"/docs/summary?doc_type={doc_type}", headers=seeded.headers)
        assert summary.status_code == 200, summary.text
        assert summary.json()["overdue_count"] == len(want), f"{doc_type}: card {summary.json()['overdue_count']} != {len(want)}"


@pytest.mark.asyncio
async def test_overdue_card_counts_what_the_unfiltered_overdue_list_shows(client, session):
    seeded = await _seed(session)
    want = seeded.ids(overdue=True)
    listed = await client.get("/docs?overdue_only=1", headers=seeded.headers)
    assert {x["id"] for x in listed.json()["items"]} == want
    assert (await client.get("/docs/summary", headers=seeded.headers)).json()["overdue_count"] == len(want)


@pytest.mark.asyncio
async def test_overdue_total_is_the_balance_the_overdue_invoices_owe(client, session):
    """An invoice with no recorded balance owes its total, in the card total as in the count."""
    seeded = await _seed(session)
    summary = (await client.get("/docs/summary?doc_type=invoice", headers=seeded.headers)).json()
    assert summary["overdue_total"] == pytest.approx(seeded.balance(seeded.ids("invoice", overdue=True)))


@pytest.mark.asyncio
async def test_awaiting_card_counts_what_its_list_link_shows(client, session):
    """The Awaiting Payment card counts, per type, the documents its status_in link lists, and
    every overdue document of the type is among them."""
    seeded = await _seed(session)
    for doc_type in ("invoice", "bill"):
        want = seeded.ids(doc_type, awaiting=True)
        summary = (await client.get(f"/docs/summary?doc_type={doc_type}", headers=seeded.headers)).json()
        assert summary["awaiting_payment_count"] == len(want), doc_type
        link = ",".join(sorted(_AWAITING[doc_type]))
        listed = await client.get(f"/docs?doc_type={doc_type}&status_in={link}", headers=seeded.headers)
        assert {x["id"] for x in listed.json()["items"]} == want, doc_type
        assert seeded.ids(doc_type, overdue=True) <= want, doc_type


@pytest.mark.asyncio
async def test_dashboard_receivables_match_the_invoice_cards(client, session):
    seeded = await _seed(session)
    sales = (await client.get("/dashboard/kpis", headers=seeded.headers)).json()["sales"]
    overdue = seeded.ids("invoice", overdue=True)
    awaiting = seeded.ids("invoice", awaiting=True)
    assert sales["ar_overdue"] == pytest.approx(seeded.balance(overdue))
    assert sales["invoices_overdue"] == len(overdue)
    assert sales["ar_outstanding"] == pytest.approx(seeded.balance(awaiting))
    assert sales["invoices_outstanding"] == len(awaiting)
    purchasing = (await client.get("/dashboard/kpis", headers=seeded.headers)).json()["purchasing"]
    assert purchasing["ap_outstanding"] == pytest.approx(seeded.balance(seeded.ids("purchase_order", awaiting=True)))


@pytest.mark.asyncio
async def test_aging_reports_age_what_is_still_owed(client, session):
    """Aging buckets hold the balance each awaiting-payment document still owes: a settled 0 is
    never aged at its total, and converted or closed documents are not aged at all."""
    seeded = await _seed(session)
    ar = (await client.get("/reports/ar-aging", headers=seeded.headers)).json()
    assert sum(line["total"] for line in ar["lines"]) == pytest.approx(
        seeded.balance(seeded.ids("invoice", awaiting=True), owed_only=True))
    ap = (await client.get("/reports/ap-aging", headers=seeded.headers)).json()
    payables = seeded.ids("bill", awaiting=True) | seeded.ids("purchase_order", awaiting=True)
    assert sum(line["total"] for line in ap["lines"]) == pytest.approx(seeded.balance(payables, owed_only=True))


@pytest.mark.asyncio
async def test_overdue_reminder_lists_the_overdue_invoices(client, session):
    from celerp.services.reorder import _detect_overdue

    seeded = await _seed(session)
    company = await session.get(Company, seeded.company_id)
    found = await _detect_overdue(session, company)
    assert {x["entity_id"] for x in found} == seeded.ids("invoice", overdue=True)
    by_id = {x["entity_id"]: x["balance"] for x in found}
    assert sum(by_id.values()) == pytest.approx(seeded.balance(seeded.ids("invoice", overdue=True)))


async def _register(client) -> tuple[dict, str]:
    email = f"bulk-{uuid.uuid4().hex[:8]}@test.example"
    r = await client.post("/auth/register", json={"company_name": "BulkCo", "email": email, "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = await client.get("/companies/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()["id"]


async def _seed_doc(session, company_id: str, entity_id: str, state: dict) -> None:
    session.add(Projection(
        company_id=uuid.UUID(company_id), entity_id=entity_id, entity_type="doc", version=1,
        updated_at=datetime.now(timezone.utc), state=state,
    ))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    pytest.param({"doc_type": "bill", "status": "received", "amount_outstanding": 50}, id="received-bill"),
    pytest.param({"doc_type": "bill", "status": "partially_received", "amount_outstanding": 50}, id="partially-received-bill"),
    pytest.param({"doc_type": "invoice", "status": "final", "amount_outstanding": None}, id="invoice-null-balance"),
])
async def test_bulk_payment_pays_documents_awaiting_payment(client, session, state):
    headers, company_id = await _register(client)
    doc_id = f"doc:bulk-{uuid.uuid4().hex[:8]}"
    await _seed_doc(session, company_id, doc_id, state | {
        "doc_number": "B-1", "currency": "USD", "contact_id": "contact:bulk", "total": 50.0,
        "amount_paid": 0.0, "line_items": [], "due_date": "2026-01-01",
    })
    r = await client.post("/docs/bulk-payment", headers=headers, json={
        "payment_date": "2026-01-15", "doc_ids": [doc_id], "amount": 50.0, "method": "transfer", "bank_account": "1111",
    })
    assert r.status_code == 200, r.text
    assert [a["doc_id"] for a in r.json()["allocations"]] == [doc_id]
    assert r.json()["allocations"][0]["amount"] == 50.0
