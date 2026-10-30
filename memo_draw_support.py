# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared setup for the memo draw tests: a company, memos issued with their lots out, and
the draw, Back to memo and rate correction calls as a client sends them."""
from __future__ import annotations

import uuid

from celerp.services.company_lock import locked_company
from test_helpers import company_auth


async def auth(session) -> dict:
    """Request headers for the admin of a fresh company with its books."""
    return (await company_auth(session, uuid.uuid4(), uuid.uuid4()))["headers"]


async def auth_full(session) -> dict:
    """A fresh company's admin: its ids and request headers."""
    return await company_auth(session, uuid.uuid4(), uuid.uuid4())


async def revoke(session, company_id, permission: str) -> None:
    """Grant ``permission`` to no role, so the company's own admin lacks it."""
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "role_grants": {permission: []}}
    await session.commit()


async def ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


def key_of(r) -> str | None:
    """The message key of a refusal, None when the response carries none."""
    detail = r.json().get("detail")
    return detail.get("message_key") if isinstance(detail, dict) else None


def also_keys(r) -> list[str | None]:
    detail = r.json().get("detail")
    return [p.get("message_key") for p in (detail.get("also") or [])] if isinstance(detail, dict) else []


async def lot(client, h, sku: str, qty: float, *, cost: float = 100.0, weight: float | None = None,
              price: float | None = None) -> str:
    data = {"sku": sku, "name": sku, "quantity": qty, "sell_by": "piece", "status": "available",
            "cost_total": cost * qty}
    if price is not None:
        data["retail_price"] = price
    if weight is not None:
        data["weight"] = weight
    return (await ok(await client.post("/items", headers=h, json=data)))["id"]


async def item(client, h, item_id: str) -> dict:
    return await ok(await client.get(f"/items/{item_id}", headers=h))


async def owner(client, h, item_id: str) -> tuple:
    s = await item(client, h, item_id)
    return s.get("status"), s.get("status_doc_id")


async def state(client, h, doc_id: str) -> dict:
    return await ok(await client.get(f"/docs/{doc_id}", headers=h))


async def contact(client, h, name: str = "Store A", currency: str | None = None) -> str:
    body = {"name": name, "contact_type": "customer"}
    if currency:
        body["currency"] = currency
    return (await ok(await client.post("/crm/contacts", headers=h, json=body)))["id"]


async def memo_out(client, h, lines: list[tuple[int, float]] | None = None, *, lots_per_line: int = 1,
                   contact_id: str | None = None, line_extra: dict | None = None, **header) -> tuple[str, list[str], list[list[str]]]:
    """An issued memo with every lot out on it. ``lines`` gives each line's (units, unit price);
    a line's units sit in ``lots_per_line`` lots of one SKU. Returns the memo id, its line ids and
    each line's lot ids."""
    lines = lines or [(10, 10.0)]
    line_items, lots = [], []
    for units, price in lines:
        sku = f"MD-{uuid.uuid4().hex[:6]}"
        per = units / lots_per_line
        # The sell price on the default list, so choosing a customer prices the line as given.
        ids = [await lot(client, h, sku, per, price=price) for _ in range(lots_per_line)]
        li = {"item_id": ids[0], "sku": sku, "description": sku, "quantity": units,
              "unit_price": price, "sell_by": "piece"}
        li.update(line_extra or {})
        line_items.append(li)
        lots.append(ids)
    body = {"doc_type": "memo", "line_items": line_items, **header}
    if contact_id:
        body["contact_id"] = contact_id
    memo = (await ok(await client.post("/docs", headers=h, json=body)))["id"]
    await ok(await client.post(f"/docs/{memo}/finalize", headers=h))
    lids = [li["line_id"] for li in (await state(client, h, memo))["line_items"]]
    await ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": lids}))
    return memo, lids, lots


async def draft(client, h, *, contact_id: str | None = None, lines: list[dict] | None = None, **header) -> str:
    """A draft invoice, optionally with ordinary (service) lines."""
    body = {"doc_type": "invoice", "line_items": lines or [], **header}
    if contact_id:
        body["contact_id"] = contact_id
    return (await ok(await client.post("/docs", headers=h, json=body)))["id"]


def service_line(price: float = 20.0, **extra) -> dict:
    return {"description": "Setting fee", "quantity": 1, "unit_price": price, "sell_by": "piece", **extra}


async def draw(client, h, memo: str, quantities: dict[str, float] | None = None, *, line_ids: list[str] | None = None,
               target: str | None = None, key: str | None = None, dry_run: bool = False,
               expected_versions: dict | None = None):
    """POST the draw: ``quantities`` maps memo line id to units, ``line_ids`` draws whole lines."""
    body: dict = {"line_ids": list(line_ids or quantities or []), "idempotency_key": key or uuid.uuid4().hex}
    if quantities:
        body["quantities"] = quantities
    if target:
        body["target_doc_id"] = target
    if dry_run:
        body["dry_run"] = True
    if expected_versions is not None:
        body["expected_versions"] = expected_versions
    return await client.post(f"/docs/{memo}/invoice-lines", headers=h, json=body)


async def drawn(client, h, memo: str, quantities: dict[str, float] | None = None, **kw) -> str:
    """A draw that must succeed; returns the invoice id."""
    return (await ok(await draw(client, h, memo, quantities, **kw)))["target_doc_id"]


async def back_to_memo(client, h, invoice: str, line_ids: list[str], *, key: str | None = None):
    return await client.post(f"/docs/{invoice}/back-to-memo", headers=h,
                             json={"line_ids": line_ids, "idempotency_key": key or uuid.uuid4().hex})


def drawn_lines(doc: dict, memo: str | None = None) -> list[dict]:
    return [li for li in doc.get("line_items") or []
            if li.get("drawn_from") and (memo is None or li["drawn_from"].get("memo_id") == memo)]


async def pay(client, h, doc_id: str, amount: float, date: str = "2026-03-02"):
    return await client.post(f"/docs/{doc_id}/payment", headers=h, json={
        "amount": amount, "payment_date": date, "bank_account": "1111"})


async def lots_out_on(client, h, doc_id: str, lot_ids: list[str]) -> list[str]:
    return [i for i in lot_ids if (await owner(client, h, i))[1] == doc_id]


async def legacy_convert(client, session, a: dict, memo: str, lot_ids: list[str]) -> str:
    """A memo converted the way it was before partial invoicing: replays the old convert's own
    events (the invoice's ``doc.created``, the memo's ``doc.converted`` and each lot's stamp onto
    the invoice), so the ledger holds what a memo converted back then holds. Returns the invoice."""
    from celerp.events.engine import emit_event
    from celerp_docs.doc_constants import LIFECYCLE_OWNED_FIELDS

    h, cid = a["headers"], a["company_id"]
    memo_doc = await state(client, h, memo)
    inv, ref = str(uuid.uuid4()), f"PF-L{uuid.uuid4().hex[:5]}"
    data = {k: v for k, v in memo_doc.items()
            if k not in LIFECYCLE_OWNED_FIELDS and k not in ("id", "entity_id", "version", "memo_fixed_amounts")}
    data.update({"doc_type": "invoice", "ref_id": ref, "source_memo_id": memo, "status": "draft"})
    common = {"company_id": cid, "actor_id": a["user_id"], "location_id": None, "metadata_": {}}
    # The memo's entry first: the invoice's lines take the memo's lots only once the memo's
    # ledger names the invoice (converted_from_memo).
    await emit_event(session, entity_id=memo, entity_type="doc", event_type="doc.converted",
                     data={"target_doc_id": inv, "target_doc_type": "invoice",
                           "pre_convert_status": memo_doc.get("status")},
                     source="api", idempotency_key=uuid.uuid4().hex, **common)
    await emit_event(session, entity_id=inv, entity_type="doc", event_type="doc.created", data=data,
                     source="api", idempotency_key=uuid.uuid4().hex, **common)
    for lot_id in lot_ids:
        await emit_event(session, entity_id=lot_id, entity_type="item", event_type="item.status.set",
                         data={"new_status": "memo_out", "source_doc_id": inv, "doc_number": ref},
                         source="memo_convert", idempotency_key=uuid.uuid4().hex,
                         **{**common, "metadata_": {"doc_id": memo}})
    await session.commit()
    return inv
