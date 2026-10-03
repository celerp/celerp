# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared steps for the production run accounting tests: components, a product with a
recipe, a run, the journal entries a movement wrote, and a snapshot of everything a
refused movement must leave exactly as it was."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from celerp.accounting_roles import ROLES_KEY, AccountRole
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from test_cost_restatement import _item, _state

WIP, PURCHASED, OPENING = (AccountRole.WORK_IN_PROGRESS.value, AccountRole.INVENTORY_PURCHASED.value,
                           AccountRole.INVENTORY_OPENING.value)
COGS, GAIN, SHRINKAGE = AccountRole.COGS.value, AccountRole.STOCK_GAIN.value, AccountRole.STOCK_SHRINKAGE.value


async def role(session, auth, name: str) -> str:
    session.expire_all()
    return ((await session.get(Company, auth["company_id"])).settings or {})[ROLES_KEY][name]


async def product(client, auth, components: list[tuple[str, float]], output_qty: float = 1) -> str:
    """A product made from ``components`` (item id, quantity per batch of ``output_qty``)."""
    item = await _item(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")
    r = await client.put(f"/manufacturing/items/{item}/recipe", headers=auth["headers"], json={
        "output_qty": output_qty, "components": [{"item_id": c, "quantity": q} for c, q in components],
        "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    return item


async def run(client, auth, item: str, qty: float) -> str:
    r = await client.post(f"/manufacturing/items/{item}/build", headers=auth["headers"], json={"quantity": qty})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def issue(client, auth, order: str, items: list[tuple[str, float]] | None = None, key: str | None = None):
    body: dict = {}
    if items is not None:
        body["items"] = [{"item_id": i, "quantity": q} for i, q in items]
    if key:
        body["idempotency_key"] = key
    return await client.post(f"/manufacturing/{order}/issue", headers=auth["headers"], json=body)


async def receive(client, auth, order: str, qty: float | None = None, key: str | None = None):
    body: dict = {}
    if qty is not None:
        body["quantity"] = qty
    if key:
        body["idempotency_key"] = key
    return await client.post(f"/manufacturing/{order}/receive", headers=auth["headers"], json=body)


async def complete(client, auth, order: str, key: str | None = None, **body):
    if key:
        body["idempotency_key"] = key
    return await client.post(f"/manufacturing/{order}/complete", headers=auth["headers"], json=body)


async def lines(session, auth, je_id: str) -> list[tuple]:
    """The posted lines of ``je_id`` as (account, roles, debit, credit), sorted."""
    state = await _state(session, auth, je_id)
    assert state.get("status") == "posted", (je_id, state)
    return sorted((e["account"], tuple(e.get("account_roles") or ()), float(e.get("debit") or 0),
                   float(e.get("credit") or 0)) for e in state["entries"])


def refusal(r, status: int, key: str) -> dict:
    """The refusal's detail, after checking its status and message key."""
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == f"mfg.{key}", detail
    assert detail.get("message"), detail
    return detail


async def snapshot(session, auth, *entity_ids: str) -> dict:
    """Everything a refused movement must not change: the named projections, every journal
    entry and every ledger event of the company."""
    session.expire_all()
    cid = auth["company_id"]
    states = {e: (await _state(session, auth, e)) for e in entity_ids}
    jes = {r.entity_id: r.state for r in (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "journal_entry"))).scalars()}
    items = sorted((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == cid, Projection.entity_type == "item"))).scalars())
    events = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid))).scalar_one()
    return {"states": states, "jes": jes, "items": items, "events": events}


async def set_settings(session, auth, **changes) -> None:
    company = await locked_company(session, auth["company_id"])
    company.settings = {**(company.settings or {}), **changes}
    await session.commit()


async def _act(client, auth, order: str, action: str, body: dict, key: str | None):
    if key:
        body["idempotency_key"] = key
    return await client.post(f"/manufacturing/{order}/{action}", headers=auth["headers"], json=body)


async def give_back(client, auth, order: str, items: list[tuple[str, float]] | None = None, key: str | None = None):
    """Return issued materials: the ``items`` given, or everything issued."""
    body: dict = {}
    if items is not None:
        body["items"] = [{"item_id": i, "quantity": q} for i, q in items]
    return await _act(client, auth, order, "return", body, key)


async def undo_receipt(client, auth, order: str, lot_id: str, key: str | None = None):
    return await _act(client, auth, order, "undo-receipt", {"lot_item_id": lot_id}, key)


async def reopen(client, auth, order: str, key: str | None = None):
    return await _act(client, auth, order, "reopen", {}, key)


async def cancel(client, auth, order: str, key: str | None = None):
    return await _act(client, auth, order, "cancel", {}, key)


async def balances(session, auth) -> dict[str, float]:
    """Every account's posted balance (debit - credit), accounts at zero left out."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars()
    net: dict[str, float] = {}
    for r in rows:
        if (r.state or {}).get("status") != "posted":
            continue
        for e in r.state.get("entries") or []:
            net[e["account"]] = round(net.get(e["account"], 0.0) + float(e.get("debit") or 0)
                                      - float(e.get("credit") or 0), 2)
    return {code: v for code, v in net.items() if v}
