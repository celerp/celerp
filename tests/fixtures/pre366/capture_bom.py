"""Capture BOM history from a release that still had BOMs (run from a checkout of one)."""
import json, os
import pytest
from sqlalchemy import select
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio
OUT = os.environ["FIXTURE_OUT"]


async def test_capture_boms(client, session):
    r = await client.post("/auth/register", json={"company_name": "BOM Co", "email": "admin@bom.test",
                                                  "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    comps = [{"sku": "COMP-A", "qty": 2}, {"sku": "COMP-B", "qty": 1}]
    r = await client.post("/manufacturing/boms", headers=h, json={"name": "Kept", "output_qty": 1, "components": comps})
    assert r.status_code == 200, r.text
    kept = r.json()["bom_id"]
    r = await client.put(f"/manufacturing/boms/{kept}", headers=h, json={"name": "Kept v2", "output_qty": 2})
    assert r.status_code == 200, r.text
    r = await client.post("/manufacturing/boms", headers=h, json={"name": "Dropped", "components": comps[:1]})
    assert r.status_code == 200, r.text
    dropped = r.json()["bom_id"]
    r = await client.delete(f"/manufacturing/boms/{dropped}", headers=h)
    assert r.status_code == 200, r.text
    session.expire_all()
    ledger = (await session.execute(select(LedgerEntry).where(LedgerEntry.entity_type == "bom")
                                    .order_by(LedgerEntry.id))).scalars().all()
    projs = (await session.execute(select(Projection).where(Projection.entity_type == "bom"))).scalars().all()
    ts = lambda v: v.isoformat() if v is not None else None
    with open(OUT, "w") as f:
        json.dump({"release": __import__("celerp").__version__, "boms": {"kept": kept, "dropped": dropped},
                   "ledger": [{"entity_id": e.entity_id, "entity_type": e.entity_type, "event_type": e.event_type,
                               "data": e.data, "source": e.source, "idempotency_key": e.idempotency_key,
                               "metadata": e.metadata_, "ts": ts(e.ts)} for e in ledger],
                   "projections": [{"entity_id": p.entity_id, "entity_type": p.entity_type, "state": p.state,
                                    "created_at": ts(p.created_at), "updated_at": ts(p.updated_at),
                                    **{k: getattr(p, k) for k in ("is_available", "is_on_memo", "is_on_marketplace",
                                                                   "is_sync_to_shopify", "is_in_production",
                                                                   "is_expired") if hasattr(p, k)}}
                                   for p in projs]},
                  f, indent=1, sort_keys=True, default=str)
