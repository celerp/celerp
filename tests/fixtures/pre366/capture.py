"""Capture old-format manufacturing state from this (pre-change) release into a frozen fixture."""
import json, os, uuid
import pytest
from sqlalchemy import select
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.models.company import Company
from celerp_accounting.models import Account
from celerp.models.accounting import UserCompany
from celerp.models.company import User
from test_cost_restatement import TZ, _item

pytestmark = pytest.mark.asyncio
OUT = os.environ["FIXTURE_OUT"]


async def _company(session):
    from celerp_accounting.routes import seed_chart_of_accounts_hook
    from test_helpers import make_authed_token
    cid, uid = uuid.uuid4(), uuid.uuid4()
    session.add(Company(id=cid, name="CostCo", slug=f"costco-{cid.hex[:8]}",
                        settings={"currency": "USD", "timezone": TZ}))
    session.add(User(id=uid, email=f"admin-{cid.hex[:8]}@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await seed_chart_of_accounts_hook(session=session, company_id=cid)
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": uid}


async def _dump(session, auth, items, runs):
    cid = auth["company_id"]
    session.expire_all()
    company = await session.get(Company, cid)
    ledger = (await session.execute(select(LedgerEntry).where(LedgerEntry.company_id == cid)
                                    .order_by(LedgerEntry.id))).scalars().all()
    projs = (await session.execute(select(Projection).where(Projection.company_id == cid))).scalars().all()
    accts = (await session.execute(select(Account).where(Account.company_id == cid))).scalars().all()
    for p in projs:
        assert p.location_id is None, p.entity_id

    def ts(v):
        return v.isoformat() if v is not None else None

    return {
        "company_id": str(cid), "user_id": str(auth["user_id"]),
        "company": {"name": company.name, "settings": company.settings},
        "items": items, "runs": runs,
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


async def _balance_sheet(client, auth):
    # Viewing the balance sheet is how this release booked stock on hand (its opening entry).
    r = await client.get("/accounting/balance-sheet", headers=auth["headers"])
    assert r.status_code == 200, r.text


async def test_capture(client, session):
    auth = await _company(session)
    h = auth["headers"]
    a = await _item(client, auth, 100.0, qty=20, sku="COMP-A")
    b = await _item(client, auth, 60.0, qty=20, sku="COMP-B")
    fg = await _item(client, auth, 0.0, qty=0, sku="FG-1")
    r = await client.put(f"/manufacturing/items/{fg}/recipe", headers=h, json={
        "output_qty": 1, "components": [{"item_id": a, "quantity": 2}, {"item_id": b, "quantity": 1}],
        "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    await _balance_sheet(client, auth)
    runs = {}
    # B3 / B1: a run built from the recipe, all components issued, nothing received.
    r = await client.post(f"/manufacturing/items/{fg}/build", headers=h, json={"quantity": 1})
    assert r.status_code == 200, r.text
    runs["settle"] = r.json()["id"]
    r = await client.post(f"/manufacturing/{runs['settle']}/issue", headers=h, json={})
    assert r.status_code == 200, r.text

    out = [{"sku": "FG-1", "name": "Lot", "quantity": 1}]

    async def imported(name, inputs):
        oid = f"mfg:{uuid.uuid4()}"
        r = await client.post("/manufacturing/import/batch", headers=h, json={"records": [{
            "entity_id": oid, "event_type": "mfg.order.created", "source": "import",
            "idempotency_key": f"imp-{oid}",
            "data": {"description": name, "order_type": "assembly", "inputs": inputs,
                     "expected_outputs": out, "output_item_id": fg}}]})
        assert r.status_code == 200 and r.json()["created"] == 1, r.text
        runs[name] = oid
        return oid

    # B2: duplicate requirements, one left open and one issued by this release.
    await imported("dup", [{"item_id": a, "quantity": 1}, {"item_id": a, "quantity": 2}, {"item_id": b, "quantity": 1}])
    oid = await imported("dup_issued", [{"item_id": a, "quantity": 1}, {"item_id": a, "quantity": 2},
                                        {"item_id": b, "quantity": 1}])
    r = await client.post(f"/manufacturing/{oid}/issue", headers=h, json={})
    assert r.status_code == 200, r.text
    # B2: zero and negative requirements (the negative one issued: only the positive line moved).
    await imported("zero", [{"item_id": a, "quantity": 0}, {"item_id": b, "quantity": 1}])
    oid = await imported("negative", [{"item_id": a, "quantity": -1}, {"item_id": b, "quantity": 1}])
    r = await client.post(f"/manufacturing/{oid}/issue", headers=h, json={})
    assert r.status_code == 200, r.text
    main = await _dump(session, auth, {"A": a, "B": b, "FG": fg}, runs)

    # B4, a second company: generic runs with no output item. One issued and half received
    # (no lot is made), one issued only.
    auth = await _company(session)
    h = auth["headers"]
    c = await _item(client, auth, 20.0, qty=10, sku="COMP-C")
    await _balance_sheet(client, auth)
    runs = {}

    async def generic(name):
        r = await client.post("/manufacturing", headers=h, json={
            "description": name, "inputs": [{"item_id": c, "quantity": 2}],
            "expected_outputs": [{"sku": "GEN-1", "name": "Generic", "quantity": 2}]})
        assert r.status_code == 200, r.text
        oid = r.json()["id"]
        r = await client.post(f"/manufacturing/{oid}/issue", headers=h, json={})
        assert r.status_code == 200, r.text
        runs[name] = oid
        return oid

    oid = await generic("generic_received")
    r = await client.post(f"/manufacturing/{oid}/receive", headers=h, json={"quantity": 1})
    assert r.status_code == 200, r.text
    await generic("generic_open")
    generic_co = await _dump(session, auth, {"C": c}, runs)

    with open(OUT, "w") as f:
        json.dump({"release": __import__("celerp").__version__, "companies": {"main": main, "generic": generic_co}},
                  f, indent=1, sort_keys=True, default=str)
