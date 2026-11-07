"""Capture the subscription templates a 2.8.0 company holds into a frozen fixture.

Run from a checkout of release 2.8.0, with this file copied into its tests/ directory:
``FIXTURE_OUT=.../templates.json pytest tests/capture.py``. The company is the 2.8.0 demo
(its own scripts/seed_demo.py), and every template is made through 2.8.0's own API on the
business day each step names, in the states an upgrade meets."""
import importlib.util
import json
import os
import random
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp_accounting.models import Account

pytestmark = pytest.mark.asyncio
OUT = os.environ["FIXTURE_OUT"]


def _on(day: str) -> None:
    """2.8.0's subscription routes see ``day`` as the company's business day."""
    import celerp_subscriptions.routes as r
    d = date.fromisoformat(day)

    async def _company_today(*a, **k):
        return d
    r.business_today = lambda *a, **k: d
    r.company_today = _company_today


async def _ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _dump(session, company_id, user_id, templates):
    session.expire_all()
    company = await session.get(Company, company_id)
    ledger = (await session.execute(select(LedgerEntry).where(LedgerEntry.company_id == company_id)
                                    .order_by(LedgerEntry.id))).scalars().all()
    projs = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
    accts = (await session.execute(select(Account).where(Account.company_id == company_id))).scalars().all()

    def ts(v):
        return v.isoformat() if v is not None else None

    return {
        "company_id": str(company_id), "user_id": str(user_id),
        "company": {"name": company.name, "settings": company.settings},
        "items": {}, "runs": templates,
        "accounts": [{"code": x.code, "name": x.name, "account_type": x.account_type,
                      "parent_code": x.parent_code} for x in accts],
        "ledger": [{"entity_id": e.entity_id, "entity_type": e.entity_type, "event_type": e.event_type,
                    "data": e.data, "actor": e.actor_id is not None, "source": e.source,
                    "idempotency_key": e.idempotency_key, "metadata": e.metadata_, "ts": ts(e.ts)}
                   for e in ledger],
        "projections": [{"entity_id": p.entity_id, "entity_type": p.entity_type, "state": p.state,
                         "version": p.version, "created_at": ts(p.created_at), "updated_at": ts(p.updated_at),
                         **{k: getattr(p, k) for k in ("is_available", "is_on_memo", "is_on_marketplace",
                                                        "is_sync_to_shopify", "is_in_production", "is_expired")},
                         "expires_at": ts(p.expires_at)} for p in projs],
    }


async def test_capture(client, session):
    from celerp.models.accounting import UserCompany

    random.seed(414)
    tok = (await _ok(await client.post("/auth/register", json={
        "company_name": "Demo Upgrade Co", "email": "admin@demo.test", "name": "Admin",
        "password": "demo-password"})))["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    spec = importlib.util.spec_from_file_location("seed_demo", Path.cwd() / "scripts" / "seed_demo.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    await seed.seed_items(client, tok)
    contacts = await seed.seed_contacts(client, tok)
    await seed.seed_docs(client, tok, contacts, "invoice", 6)
    customer = next(c for c in contacts if c.get("contact_type") == "customer")
    vendor = next((c for c in contacts if c.get("contact_type") == "vendor"), customer)

    async def template(label, start, freq="monthly", doc_type="subscription_invoice", activate=True, **kw):
        who = vendor if doc_type == "subscription_po" else customer
        body = {"doc_type": doc_type, "contact_id": who.get("entity_id") or who.get("id"), "frequency": freq,
                "line_items": [{"description": label, "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
                **kw}
        if start:
            body["start_date"] = start
        eid = (await _ok(await client.post("/docs", json=body, headers=h)))["id"]
        if activate:
            _on(start)
            await _ok(await client.post(f"/subscriptions/{eid}/activate", headers=h))
        return eid

    async def act(eid, action, day):
        _on(day)
        await _ok(await client.post(f"/subscriptions/{eid}/{action}", headers=h))

    async def field(eid, name, value: str):
        # What 2.8.0's click-to-edit field sends (ui/api_client.patch_doc): the form value, as text.
        await _ok(await client.patch(f"/docs/{eid}", json={"fields_changed": {name: {"new": value}}},
                                      headers=h))

    t = {}
    t["clean_monthly"] = await template("clean monthly", "2026-06-30")
    for d in ("2026-07-30", "2026-08-30", "2026-09-30"):
        await act(t["clean_monthly"], "generate", d)
    t["never_generated"] = await template("never generated", "2026-10-15")
    t["weekly_late"] = await template("weekly late", "2026-09-01", "weekly")
    await act(t["weekly_late"], "generate", "2026-09-10")
    await act(t["weekly_late"], "generate", "2026-09-17")
    t["paused_now"] = await template("paused now", "2026-04-01")
    await act(t["paused_now"], "generate", "2026-05-01")
    await act(t["paused_now"], "pause", "2026-05-15")
    t["paused_resumed"] = await template("paused and resumed", "2026-04-01")
    await act(t["paused_resumed"], "generate", "2026-05-01")
    await act(t["paused_resumed"], "pause", "2026-05-15")
    await act(t["paused_resumed"], "resume", "2026-08-10")
    await act(t["paused_resumed"], "generate", "2026-09-10")
    t["custom_10"] = await template("custom 10 days", "2026-09-01", "custom", custom_interval_days=10)
    await act(t["custom_10"], "generate", "2026-09-11")
    # Custom with the interval left at "--": 2.8.0 billed every 30 days.
    t["custom_no_days"] = await template("custom without interval", "2026-09-01", "custom")
    await act(t["custom_no_days"], "generate", "2026-10-01")
    # 2.8.0 edits a template's fields only while it is a draft. An interval cleared there ("")
    # activates on 30 days; one typed there ("14") is stored as text, and 2.8.0's activation
    # fails on it, so that template stays a draft.
    t["custom_cleared"] = await template("custom cleared interval", "2026-10-01", "custom", activate=False,
                                         custom_interval_days=10)
    await field(t["custom_cleared"], "custom_interval_days", "")
    await act(t["custom_cleared"], "activate", "2026-10-01")
    t["custom_typed"] = await template("custom typed interval", "2026-10-01", "custom", activate=False)
    await field(t["custom_typed"], "custom_interval_days", "14")
    _on("2026-10-01")
    with pytest.raises(TypeError):
        await client.post(f"/subscriptions/{t['custom_typed']}/activate", headers=h)
    t["month_end"] = await template("month end", "2025-12-31")
    await act(t["month_end"], "generate", "2026-01-31")
    await act(t["month_end"], "generate", "2026-02-28")
    t["purchase"] = await template("purchasing", "2026-08-05", doc_type="subscription_po")
    await act(t["purchase"], "generate", "2026-09-05")
    t["cancelled"] = await template("cancelled", "2026-05-01")
    await act(t["cancelled"], "generate", "2026-06-01")
    await act(t["cancelled"], "cancel", "2026-06-20")
    t["draft"] = await template("draft", "2026-12-01", activate=False)

    user_company = (await session.execute(select(UserCompany))).scalars().first()
    with open(OUT, "w") as f:
        json.dump({"release": "2.8.0",
                   "companies": {"demo": await _dump(session, user_company.company_id, user_company.user_id, t)}},
                  f, indent=1, sort_keys=True, default=str)
