"""Capture old-format manufacturing state from this (pre-change) release into a frozen fixture."""
import json, os, uuid
from datetime import datetime
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

    shortage = await _shortage(client, session)
    shape = await _shape(client, session)
    mixed = await _mixed(client, session)
    with open(OUT, "w") as f:
        json.dump({"release": __import__("celerp").__version__,
                   "companies": {"main": main, "generic": generic_co, "shortage": shortage, "shape": shape,
                                 "mixed": mixed}},
                  f, indent=1, sort_keys=True, default=str)


async def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


async def _generic(client, h, name, inputs, outputs, issue=None):
    """A run made through the generic API, then issued (``issue``: the items, or [] for all)."""
    oid = (await _ok(await client.post("/manufacturing", headers=h, json={
        "description": name, "inputs": inputs, "expected_outputs": outputs})))["id"]
    if issue is not None:
        await _ok(await client.post(f"/manufacturing/{oid}/issue", headers=h, json={"items": issue} if issue else {}))
    return oid


async def _import(client, h, name, inputs, outputs, output_item_id):
    oid = f"mfg:{uuid.uuid4()}"
    r = await _ok(await client.post("/manufacturing/import/batch", headers=h, json={"records": [{
        "entity_id": oid, "event_type": "mfg.order.created", "source": "import", "idempotency_key": f"imp-{oid}",
        "data": {"description": name, "order_type": "assembly", "inputs": inputs, "expected_outputs": outputs,
                 "output_item_id": output_item_id}}]}))
    assert r["created"] == 1, r
    return oid


def _line(qty, sku="GEN-1"):
    return {"sku": sku, "name": "Generic", "quantity": qty}


async def _shortage(client, session):
    """Issues this release recorded in full although less (or nothing, or an undeclared item) left stock."""
    auth = await _company(session)
    h = auth["headers"]
    d = await _item(client, auth, 8.0, qty=2, sku="COMP-D")      # 2 on hand at 4
    e = await _item(client, auth, 0.0, qty=0, sku="COMP-E")      # none on hand
    f = await _item(client, auth, 30.0, qty=10, sku="COMP-F")    # 10 at 3
    c = await _item(client, auth, 12.0, qty=6, sku="COMP-C")     # 6 at 2
    a = await _item(client, auth, 10.0, qty=10, sku="COMP-A")    # 10 at 1
    await _balance_sheet(client, auth)
    out = [_line(1)]
    runs = {
        "short": await _generic(client, h, "short", [{"item_id": d, "quantity": 5}], out, issue=[]),
        "none_on_hand": await _generic(client, h, "none_on_hand", [{"item_id": e, "quantity": 5}], out, issue=[]),
        "undeclared": await _generic(client, h, "undeclared", [{"item_id": a, "quantity": 1}], out,
                                     issue=[{"item_id": a, "quantity": 1}, {"item_id": c, "quantity": 2}]),
    }
    oid = await _generic(client, h, "twice", [{"item_id": f, "quantity": 5}], out, issue=[{"item_id": f, "quantity": 2}])
    await _ok(await client.post(f"/manufacturing/{oid}/issue", headers=h, json={"items": [{"item_id": f, "quantity": 3}]}))
    runs["twice"] = oid
    return await _dump(session, auth, {"A": a, "C": c, "D": d, "E": e, "F": f}, runs)


async def _shape(client, session):
    """Runs whose declared output this release accepted although no run can make it."""
    auth = await _company(session)
    h = auth["headers"]
    g = await _item(client, auth, 100.0, qty=100, sku="COMP-G")  # 100 at 1
    fg = await _item(client, auth, 0.0, qty=0, sku="FG-2")
    await _balance_sheet(client, auth)
    one = [{"item_id": g, "quantity": 2}]
    runs = {}
    for name, outputs in (("out_empty", []), ("out_zero", [_line(0)]), ("out_negative", [_line(-1)]),
                          ("out_multi", [_line(1), _line(2, "GEN-2")])):
        runs[name] = await _generic(client, h, name, one, outputs, issue=[])
    oid = await _generic(client, h, "out_multi_received", one, [_line(2), _line(3, "GEN-2")], issue=[])
    await _ok(await client.post(f"/manufacturing/{oid}/receive", headers=h, json={"quantity": 1}))
    runs["out_multi_received"] = oid
    for name, outputs in (("imp_multi", [_line(1, "FG-2"), _line(2, "GEN-2")]), ("imp_empty", []),
                          ("imp_zero", [_line(0, "FG-2")])):
        oid = await _import(client, h, name, one, outputs, fg)
        await _ok(await client.post(f"/manufacturing/{oid}/issue", headers=h, json={}))
        runs[name] = oid
    return await _dump(session, auth, {"G": g, "FG": fg}, runs)


async def _mixed(client, session):
    """One company holding every older shape at once: BOM history from the release before
    recipes, a short issue, an undeclared item, several outputs and a receipt with no lot,
    beside a run this release could settle."""
    auth = await _company(session)
    h = auth["headers"]
    cid = auth["company_id"]
    boms = json.load(open(os.environ["BOM_HISTORY"]))
    last = {}
    for e in boms["ledger"]:
        entry = LedgerEntry(company_id=cid, entity_id=e["entity_id"], entity_type=e["entity_type"],
                                event_type=e["event_type"], data=e["data"], actor_id=auth["user_id"],
                                location_id=None, source=e["source"], idempotency_key=e["idempotency_key"],
                                metadata_=e["metadata"], ts=datetime.fromisoformat(e["ts"]))
        session.add(entry)
        await session.flush()
        last[e["entity_id"]] = entry.id  # a projection's version is its last event's ledger id
    for p in boms["projections"]:
        session.add(Projection(company_id=cid, entity_id=p["entity_id"], entity_type=p["entity_type"],
                               state=p["state"], version=last[p["entity_id"]], location_id=None,
                               created_at=datetime.fromisoformat(p["created_at"]),
                               updated_at=datetime.fromisoformat(p["updated_at"]),
                               # flags added after that release take their migration default
                               **{k: p.get(k, False) for k in ("is_available", "is_on_memo", "is_on_marketplace",
                                                               "is_sync_to_shopify", "is_in_production",
                                                               "is_expired")}))
    await session.commit()
    hh = await _item(client, auth, 8.0, qty=2, sku="COMP-H")    # 2 on hand at 4
    j = await _item(client, auth, 15.0, qty=5, sku="COMP-J")    # 5 at 3
    k = await _item(client, auth, 40.0, qty=20, sku="COMP-K")   # 20 at 2
    fg = await _item(client, auth, 0.0, qty=0, sku="FG-3")
    await _ok(await client.put(f"/manufacturing/items/{fg}/recipe", headers=h, json={
        "output_qty": 1, "components": [{"item_id": k, "quantity": 2}], "labor": [], "overhead": []}))
    await _balance_sheet(client, auth)
    runs = {"tangle": await _generic(client, h, "tangle", [{"item_id": hh, "quantity": 5}],
                                     [_line(2), _line(1, "GEN-2")],
                                     issue=[{"item_id": hh, "quantity": 5}, {"item_id": j, "quantity": 1}])}
    await _ok(await client.post(f"/manufacturing/{runs['tangle']}/receive", headers=h, json={"quantity": 1}))
    runs["recipe"] = (await _ok(await client.post(f"/manufacturing/items/{fg}/build", headers=h,
                                                  json={"quantity": 1})))["id"]
    await _ok(await client.post(f"/manufacturing/{runs['recipe']}/issue", headers=h, json={}))
    return await _dump(session, auth, {"H": hh, "J": j, "K": k, "FG": fg, "BOM_KEPT": boms["boms"]["kept"],
                                       "BOM_DROPPED": boms["boms"]["dropped"]}, runs)
