# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A production run names only real inventory items of its own company.

Every item a new run names (its components, and the product it makes) must be an
item in the same company when the run is created, whichever door creates it: the
New run form, the Build button, Make on the To-Make board, a finalized order, or a
manufacturing import. A document, contact or List ID is not a component just because
a record exists under it. A refused run leaves nothing behind. Runs already in the
ledger keep rebuilding as they are, and issuing still checks the component as it is
now.
"""

from __future__ import annotations

import ast
import inspect
import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from test_helpers import perm_setup



async def _company_id(session):
    from celerp.models.company import Company
    return (await session.execute(select(Company.id).order_by(Company.created_at.desc()).limit(1))).scalar_one()


async def _runs(session, company_id) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "mfg_order"))).scalar_one()


async def _other_kind(client, h, kind: str) -> str:
    if kind == "doc":
        r = await client.post("/docs", json={"doc_type": "invoice"}, headers=h)
    elif kind == "contact":
        r = await client.post("/crm/contacts", json={"name": "Buyer"}, headers=h)
    else:
        r = await client.post("/lists", json={"list_type": "quotation"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _order(item_id: str, **extra) -> dict:
    return {"description": "Run", "inputs": [{"item_id": item_id, "quantity": 1}], **extra}


@pytest.mark.asyncio
async def test_new_run_with_a_real_component_is_created(client, session):
    s = await perm_setup(client, session)
    r = await client.post("/manufacturing", json=_order(s["item_id"]), headers=s["admin_h"])
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("kind", ["unknown", "doc", "contact", "list"])
@pytest.mark.asyncio
async def test_new_run_naming_a_non_item_component_is_refused(client, session, kind):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    ref = "item:nope" if kind == "unknown" else await _other_kind(client, s["admin_h"], kind)
    before = await _runs(session, company_id)

    r = await client.post("/manufacturing", json={
        "description": "Run",
        "inputs": [{"item_id": s["item_id"], "quantity": 1}, {"item_id": ref, "quantity": 1}],
    }, headers=s["admin_h"])
    assert r.status_code == 422, r.text
    assert ref in r.json()["detail"]
    assert await _runs(session, company_id) == before


@pytest.mark.asyncio
async def test_new_run_making_a_non_item_product_is_refused(client, session):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    doc_id = await _other_kind(client, s["admin_h"], "doc")
    r = await client.post("/manufacturing/import/batch", json={"records": [{
        "entity_id": "mfg:out", "event_type": "mfg.order.created",
        "data": {**_order(s["item_id"]), "output_item_id": doc_id},
        "source": "import", "idempotency_key": "out-1",
    }]}, headers=s["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 0 and doc_id in r.json()["errors"][0]
    assert await _runs(session, company_id) == 0


@pytest.mark.asyncio
async def test_new_run_naming_another_companys_item_is_refused(client, session):
    from celerp.events.engine import emit_event
    from celerp.models.company import Company

    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    other = uuid.uuid4()
    session.add(Company(id=other, name="Other", slug=f"other-{other.hex[:8]}", settings={}))
    await session.flush()
    await emit_event(session, company_id=other, entity_id="item:theirs", entity_type="item",
                     event_type="item.created", data={"sku": "THEIRS", "name": "Theirs", "quantity": 5,
                                                      "sell_by": "piece"},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
    await session.flush()

    r = await client.post("/manufacturing", json=_order("item:theirs"), headers=s["admin_h"])
    assert r.status_code == 422, r.text
    assert await _runs(session, company_id) == 0
    assert await _runs(session, other) == 0


@pytest.mark.parametrize("kind", ["unknown", "doc", "contact"])
@pytest.mark.asyncio
async def test_imported_run_naming_a_non_item_component_is_refused(client, session, kind):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    ref = "item:nope" if kind == "unknown" else await _other_kind(client, s["admin_h"], kind)

    r = await client.post("/manufacturing/import/batch", json={"records": [
        {"entity_id": "mfg:bad", "event_type": "mfg.order.created", "data": _order(ref),
         "source": "import", "idempotency_key": "bad-1"},
        {"entity_id": "mfg:good", "event_type": "mfg.order.created", "data": _order(s["item_id"]),
         "source": "import", "idempotency_key": "good-1"},
    ]}, headers=s["admin_h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1, body
    assert len(body["errors"]) == 1 and ref in body["errors"][0] and body["errors"][0].startswith("mfg:bad")
    assert await session.get(Projection, {"company_id": company_id, "entity_id": "mfg:bad"}) is None
    assert (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == "mfg:bad"))).scalar_one() == 0
    assert await session.get(Projection, {"company_id": company_id, "entity_id": "mfg:good"}) is not None


@pytest.mark.parametrize("data", [
    {"description": "Run", "inputs": "item:x"},
    {"description": "Run", "inputs": [{"quantity": 1}]},
    {"description": "Run", "inputs": [{"item_id": 7, "quantity": 1}]},
    {"description": "Run", "inputs": [], "output_item_id": 7},
])
@pytest.mark.asyncio
async def test_imported_run_with_malformed_references_is_refused(client, session, data):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    r = await client.post("/manufacturing/import/batch", json={"records": [{
        "entity_id": "mfg:shape", "event_type": "mfg.order.created", "data": data,
        "source": "import", "idempotency_key": "shape-1",
    }]}, headers=s["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 0 and len(r.json()["errors"]) == 1
    assert await _runs(session, company_id) == 0


@pytest.mark.asyncio
async def test_historical_run_with_a_dangling_component_still_rebuilds(client, session):
    """A run created before this rule may name an ID that is not an item; it replays as it was."""
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    session.add(LedgerEntry(
        company_id=company_id, entity_id="mfg:old", entity_type="mfg_order", event_type="mfg.order.created",
        data=_order("item:gone"), actor_id=None, location_id=None, source="legacy",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    ))
    await session.flush()
    await ProjectionEngine.rebuild(session, company_id)
    await session.flush()
    row = await session.get(Projection, {"company_id": company_id, "entity_id": "mfg:old"}, populate_existing=True)
    assert row.entity_type == "mfg_order"
    assert row.state["inputs"][0]["item_id"] == "item:gone"


@pytest.mark.asyncio
async def test_issuing_a_component_deleted_after_the_run_was_created_is_refused(client, session):
    s = await perm_setup(client, session)
    company_id = await _company_id(session)
    r = await client.post("/manufacturing", json=_order(s["item_id"]), headers=s["admin_h"])
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    r = await client.post("/items/bulk/delete", json={"entity_ids": [s["item_id"]]}, headers=s["admin_h"])
    assert r.status_code == 200, r.text

    r = await client.post(f"/manufacturing/{run_id}/issue", json={}, headers=s["admin_h"])
    assert r.status_code == 404, r.text
    assert (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == s["item_id"]))).scalar_one() == 0


def test_every_run_creation_goes_through_the_one_check():
    """No writer of a new run bypasses the shared reference check: the only emit of a run's
    creation is inside it, and the one emit with a variable event type (the bulk run actions)
    is only ever handed lifecycle events."""
    from celerp_manufacturing import routes

    tree = ast.parse(inspect.getsource(routes))
    creators, variable = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "emit_event":
                event = {kw.arg: kw.value for kw in node.keywords}.get("event_type")
                if isinstance(event, ast.Constant):
                    if event.value == "mfg.order.created":
                        creators.append(fn.name)
                elif fn.name not in variable:
                    variable.append(fn.name)
    assert creators == ["_emit_order_created"], creators
    assert sorted(variable) == ["_emit", "bulk_run_action"], variable

    bulk = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "bulk_run_action")
    handed = [c.args[1] for c in ast.walk(bulk)
              if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_emit"]
    assert handed and all(isinstance(a, ast.Constant) and a.value.startswith("mfg.order.")
                          and a.value != "mfg.order.created" for a in handed)


def test_no_other_module_writes_a_new_run():
    """Outside the manufacturing routes, a run's creation event is only named by its schema,
    its type constant and the projection that applies it."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    allowed = {"celerp/events/schemas.py", "celerp/events/types.py",
               "default_modules/celerp-manufacturing/celerp_manufacturing/projection_handler.py",
               "default_modules/celerp-manufacturing/celerp_manufacturing/routes.py"}
    found = {str(p.relative_to(root)) for base in ("celerp", "default_modules") for p in (root / base).rglob("*.py")
             if "/tests/" not in str(p) and "mfg.order.created" in p.read_text(encoding="utf-8")}
    assert found <= allowed, found - allowed


@pytest.mark.asyncio
async def test_finalizing_an_order_skips_and_reports_a_line_whose_recipe_names_a_deleted_item(client, session):
    """Automatic work orders follow the same rule: a product whose recipe names an item that has
    since been deleted gets no work order, the other lines still do, the order still finalizes,
    and the skipped line is reported."""
    s = await perm_setup(client, session)
    h = s["admin_h"]

    async def _item(sku: str, qty: int = 0) -> str:
        r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku,
                                                         "quantity": qty, "sell_by": "piece"})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    gold, silver = await _item("GOLDREF", 100), await _item("SILVREF", 100)
    ring, chain = await _item("RINGREF"), await _item("CHAINREF")
    for product, part in ((ring, gold), (chain, silver)):
        r = await client.put(f"/manufacturing/items/{product}/recipe", headers=h, json={
            "output_qty": 1, "components": [{"item_id": part, "quantity": 1}], "labor": [], "overhead": []})
        assert r.status_code == 200, r.text
    r = await client.patch("/companies/me", headers=h, json={"settings": {"manufacturing": {
        "auto_create_work_orders": True, "auto_complete_work_orders": False}}})
    assert r.status_code == 200, r.text
    r = await client.post("/items/bulk/delete", json={"entity_ids": [gold]}, headers=h)
    assert r.status_code == 200, r.text

    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "total": 0, "line_items": [
        {"item_id": ring, "sku": "RINGREF", "name": "RINGREF", "quantity": 2, "unit_price": 10},
        {"item_id": chain, "sku": "CHAINREF", "name": "CHAINREF", "quantity": 1, "unit_price": 10}]})
    assert r.status_code in (200, 201), r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=h)
    assert r.status_code == 200, r.text

    runs = (await client.get("/manufacturing", headers=h)).json()["items"]
    assert [o["output_item_id"] for o in runs] == [chain]
    notes = [n for n in (await client.get("/notifications", headers=h)).json()["items"]
             if n["title"] == "Work orders not created"]
    assert len(notes) == 1 and "RINGREF" in notes[0]["body"] and gold in notes[0]["body"]
