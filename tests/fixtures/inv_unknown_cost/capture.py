"""Capture a company backup from a release before unknown goods costs were kept (run from a checkout of one).

    FIXTURE_OUT=<dir> pytest tests/fixtures/inv_unknown_cost/capture.py

The company holds a cleared goods cost on a lot carrying freight, a merge of a known and an
unknown source, a merge of a lot carrying a bill's freight, a priced receipt into a lot, a
split and a sold-out lot. Writes the backup and every item's state, keyed by SKU."""
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from company_backup_support import download
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_company_backup import _bk_local, _bk_setup
from test_helpers import merge_items, provision_company_books
from test_landed_cost_pools import _po_into, _sell
from test_landed_cost_removals import _split

pytestmark = pytest.mark.asyncio
OUT = Path(os.environ["FIXTURE_OUT"])


async def _item(client, auth, sku: str, qty: float, **fields) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": sku.title(), "quantity": qty, "sell_by": "piece", "category": "Beads",
        "status": "available", **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, auth, ids: list[str]) -> str:
    r = await merge_items(client, headers=auth["headers"],
                          json={"source_entity_ids": ids, "target_sku_from": ids[0]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_capture(real_engine, real_client, tmp_path, monkeypatch):
    from celerp.models.projections import Projection

    _bk_local(monkeypatch, tmp_path)
    _user, cid, tok = await _bk_setup(real_engine, "Unknown Cost Co", "unknown-cost")
    async with maker(real_engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
    auth = {"headers": {"Authorization": f"Bearer {tok}"}, "company_id": cid}
    c = real_client

    cleared = await _item(c, auth, "cleared", 1, cost_total=10.0, retail_price=20.0)
    await _po_into(c, auth, cleared, 9, 10.0, shipping=30.0)
    r = await c.patch(f"/items/{cleared}", headers=auth["headers"],
                      json={"fields_changed": {"cost_total": {"new": None}}})
    assert r.status_code == 200, r.text

    known = await _item(c, auth, "known", 2, cost_total=20.0, retail_price=15.0)
    unknown = await _item(c, auth, "unknown", 3, retail_price=25.0)
    await _merge(c, auth, [known, unknown])

    freighted = await _item(c, auth, "freighted", 0)
    await _po_into(c, auth, freighted, 10, 4.0, shipping=30.0)
    plain = await _item(c, auth, "plain", 5, cost_total=25.0)
    await _merge(c, auth, [freighted, plain])

    topped = await _item(c, auth, "topped", 4, cost_total=8.0)
    await _po_into(c, auth, topped, 6, 3.0)

    whole = await _item(c, auth, "whole", 6, cost_total=36.0, wholesale_price=9.0)
    await _split(c, auth, whole, 2)

    sold = await _item(c, auth, "sold", 3, cost_total=15.0)
    await _sell(c, auth, sold, 3)

    data = await download(c, tok)
    async with maker(real_engine)() as s:
        items = (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "item"))).scalars().all()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "company.celerp-company").write_bytes(data)
    (OUT / "items.json").write_text(json.dumps(
        {"release": __import__("celerp").__version__,
         "items": {p.entity_id: p.state for p in items}}, indent=1, sort_keys=True, default=str))
