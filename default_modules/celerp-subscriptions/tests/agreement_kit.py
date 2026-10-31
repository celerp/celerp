# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared steps for the agreement schedule tests: a frozen business day, a registered
company, agreements created and activated through the API, and what they billed."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

LINE = {"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}


def freeze_instant(monkeypatch, instant: datetime) -> None:
    """Every business-day decision (business_today, company_today) sees ``instant``."""
    from celerp.services import business_time

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(business_time, "datetime", _Frozen)


def freeze_day(monkeypatch, day: str) -> None:
    """Noon UTC on ``day``: the business day of a UTC company."""
    freeze_instant(monkeypatch, datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc))


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": f"Agreements-{uuid.uuid4().hex[:6]}", "email": f"ag-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return h(r.json()["access_token"])


async def member(client, session, admin: dict, role: str) -> tuple[dict, str]:
    """A member with ``role``: (headers, user id)."""
    from test_helpers import invite_user

    email = f"{role}-{uuid.uuid4().hex[:8]}@test.test"
    headers = h(await invite_user(client, session, admin, email, role))
    users = (await client.get("/companies/me/users", headers=admin)).json()["items"]
    return headers, next(str(u.get("id") or u.get("user_id")) for u in users if u["email"] == email)


async def agreement(client, headers: dict, *, start: str, doc_type: str = "subscription_invoice",
                    activate: bool = True, **fields) -> str:
    data = {"doc_type": doc_type, "contact_id": "contact:test-001", "frequency": "monthly",
            "start_date": start, "line_items": [dict(LINE)], **fields}
    r = await client.post("/docs", json=data, headers=headers)
    assert r.status_code in (200, 201), r.text
    eid = r.json()["id"]
    if activate:
        ra = await client.post(f"/subscriptions/{eid}/activate", headers=headers)
        assert ra.status_code == 200, ra.text
    return eid


async def template(client, headers: dict, eid: str) -> dict:
    r = await client.get(f"/docs/{eid}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def next_bill_date(client, headers: dict, eid: str) -> str | None:
    items = (await client.get("/subscriptions", headers=headers, params={"limit": 500})).json()["items"]
    return next(i for i in items if (i.get("entity_id") or i.get("id")) == eid).get("next_bill_date")


async def schedule(client, headers: dict, eid: str, **change):
    return await client.patch(f"/subscriptions/{eid}/schedule", json=change, headers=headers)


async def generate_due(client, headers: dict, eid: str, key: str | None = None):
    return await client.post(f"/subscriptions/{eid}/generate-due", headers=headers,
                             json={"request_key": key} if key else {})


async def bill_early(client, headers: dict, eid: str, key: str, **extra):
    return await client.post(f"/subscriptions/{eid}/bill-early", headers=headers,
                             json={"request_key": key, **extra})


async def billed_docs(session, eid: str) -> list[dict]:
    """Every document the agreement billed, oldest period first, void ones included."""
    rows = (await session.execute(select(Projection).where(
        Projection.entity_type == "doc",
        Projection.state["source_agreement"].as_string() == eid))).scalars().all()
    return sorted((dict(r.state, entity_id=r.entity_id) for r in rows),
                  key=lambda s: (s.get("period_index"), s.get("ref_id") or ""))


async def claim_events(session, eid: str) -> list[str]:
    keys = (await session.execute(select(LedgerEntry.idempotency_key).where(
        LedgerEntry.idempotency_key.like(f"sub:{eid}:p%")))).scalars().all()
    return sorted(keys)


def iso(d: date) -> str:
    return d.isoformat()


async def company_id(client, headers: dict) -> str:
    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def contact(client, headers: dict, email: str) -> str:
    r = await client.post("/crm/contacts", json={"name": "Member", "email": email}, headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def tick(session, cid: str, day: str):
    """One dispatcher pass for one company on ``day``, in the test's own transaction."""
    from contextlib import asynccontextmanager
    from celerp_docs import periodic

    @asynccontextmanager
    async def factory():
        yield session

    return await periodic.run_company(factory, uuid.UUID(cid), today=date.fromisoformat(day))


async def summary(client, headers: dict, **params) -> dict:
    r = await client.get("/subscriptions/summary", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def revoke(client, owner: dict, perm_key: str, role_key: str) -> None:
    r = await client.patch("/companies/me/role-permissions", headers=owner,
                           json={"perm_key": perm_key, "role_key": role_key, "granted": False})
    assert r.status_code == 200, r.text
