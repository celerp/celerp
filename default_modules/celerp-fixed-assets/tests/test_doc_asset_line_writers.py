# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every way a document is written follows its asset lines.

A line that names an asset is checked wherever a document's lines, type or original
document are written: the document page, a raw import, a batch import and its upsert.
The check reads the lines before the write and after it. An issued document's asset lines
never change, a malformed or foreign asset id is refused, and a line taken off a draft that
an asset is waiting on is refused with the way to undo the sale instead (cancelling the sale
from the asset's page voids the draft). A refused write leaves no document, no journal and
no pending sale behind.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _run
from test_sales import _asset_line, _assert_clearing, _doc, _finalize, _patch, _sale, _sell
from test_transfer import _second_company

D = Decimal


def _record(eid: str, lines: list[dict], status: str = "draft", key: str | None = None, **data) -> dict:
    total = sum(D(str(li["unit_price"])) for li in lines)
    return {"entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": key or uuid.uuid4().hex,
            "data": {"doc_type": "invoice", "ref_id": eid[4:], "status": status, "total": float(total),
                     "amount_paid": 0.0, "amount_outstanding": float(total), "issue_date": "2026-05-20",
                     "contact_name": "Buyer Co", "line_items": lines, **data}}


def _line(name: str, asset_id=None, **kw) -> dict:
    return {"name": name, "quantity": 1, "unit_price": 100.0, **({"asset_id": asset_id} if asset_id is not None else {}),
            **kw}


def _eid() -> str:
    return f"doc:INV-IMP{uuid.uuid4().hex[:6]}"


async def _import(fa: FA, record: dict):
    r = await fa.client.post("/docs/import", headers=fa.h, json=record)
    if r.is_error:
        await fa.session.rollback()
    return r


async def _batch(fa: FA, records: list[dict], upsert: bool = False) -> dict:
    r = await fa.client.post("/docs/import/batch", headers=fa.h, json={"records": records, "upsert": upsert})
    assert r.status_code == 200, r.text
    return r.json()


async def _journals_of(fa: FA, doc_id: str) -> list[dict]:
    return [je for je in await fa.entities("journal_entry") if doc_id in je["entity_id"]]


async def _left_nothing(fa: FA, doc_id: str, *assets: str) -> None:
    """A refused write leaves no document, no journal and no pending sale."""
    assert await fa.state(doc_id) is None
    assert await _journals_of(fa, doc_id) == []
    for a in assets:
        assert (await fa.asset(a)).get("sale") in (None, {}), a


async def test_f03_raw_import_checks_asset_lines(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    number = (await fa.asset(car))["number"]
    other = await _second_company(fa)
    theirs = await _contributed(other)

    # A draft import naming an asset that is not being sold on it.
    eid = _eid()
    r = await _import(fa, _record(eid, [_line("Car", car)]))
    assert r.status_code == 409 and number in r.text, r.text
    await _left_nothing(fa, eid, car)

    # Forged, foreign-company, wrong-type and malformed asset ids.
    for bad in ("fa:asset:nope", theirs, "doc:INV-0001", 123, {"id": car}, ["x"]):
        eid = _eid()
        r = await _import(fa, _record(eid, [_line("Odd line", bad)]))
        assert r.status_code == 422 and "Odd line" in r.text, (bad, r.text)
        await _left_nothing(fa, eid, car)
    assert (await other.asset(theirs)).get("sale") in (None, {})

    # The same asset twice.
    eid = _eid()
    r = await _import(fa, _record(eid, [_line("Car", car), _line("Car again", car)]))
    assert r.status_code == 422 and "Car again" in r.text, r.text
    await _left_nothing(fa, eid, car)

    # Repeated with the same key, refused the same way each time.
    record = _record(_eid(), [_line("Car", car)])
    for _ in range(2):
        assert (await _import(fa, record)).status_code == 409
    await _left_nothing(fa, record["entity_id"], car)

    # The neighbour: a plain draft import is unchanged, and its replay is a hit.
    plain = _record(_eid(), [_line("Service")])
    assert (await _import(fa, plain)).status_code == 200
    again = await _import(fa, plain)
    assert again.status_code == 200 and again.json()["idempotency_hit"] is True


async def test_f03_issued_import_cannot_carry_an_asset_line(fa):
    """With ledger postings (the single import posts what an issued invoice books) and
    without (a batch import), and for an asset waiting on a draft of its own."""
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    await _run(fa, "2026-03")
    inv = await _sell(fa, van)
    for status in ("final", "paid"):
        eid = _eid()
        r = await _import(fa, _record(eid, [_line("Car", car)], status=status))
        assert r.status_code in (409, 422), r.text
        await _left_nothing(fa, eid, car)
        eid = _eid()
        r = await _import(fa, _record(eid, [_line("Van", van)], status=status))
        assert r.status_code in (409, 422), r.text
        assert await fa.state(eid) is None and await _journals_of(fa, eid) == []
    eid = _eid()
    out = await _batch(fa, [_record(eid, [_line("Car", car)], status="final")])
    assert out["created"] == 0 and len(out["errors"]) == 1, out
    await _left_nothing(fa, eid, car)
    # The van is still waiting on its own draft, nothing else.
    assert (await _sale(fa, van))["state"] == "pending" and (await _sale(fa, van))["invoice_id"] == inv


async def test_f03_import_upsert_checks_asset_lines(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    eid = _eid()
    key = uuid.uuid4().hex
    out = await _batch(fa, [_record(eid, [_line("Service")], key=key)])
    assert out["created"] == 1, out
    out = await _batch(fa, [_record(eid, [_line("Service"), _line("Car", car)], key=key)], upsert=True)
    assert out["updated"] == 0 and len(out["errors"]) == 1, out
    assert not [li for li in (await fa.state(eid))["line_items"] if li.get("asset_id")]
    assert (await fa.asset(car)).get("sale") in (None, {})
    assert await _journals_of(fa, eid) == []


async def test_f10_last_asset_line_off_a_draft_is_refused_until_the_sale_is_cancelled(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    await _run(fa, "2026-03")
    inv = await _sell(fa, car)
    line = await _asset_line(fa, inv)

    # Taking the line off: refused, naming the way back.
    r = await _patch(fa, inv, [{"name": "Delivery", "quantity": 1, "unit_price": 50.0}])
    assert r.status_code == 409 and "cancel the sale" in r.json()["detail"], r.text
    assert (await _sale(fa, car))["state"] == "pending"
    assert (await _asset_line(fa, inv))["asset_id"] == car
    # Swapping it for another asset, and changing the document's type: refused too.
    r = await _patch(fa, inv, [{**line, "asset_id": van}])
    assert r.status_code in (409, 422), r.text
    r = await _patch(fa, inv, [line], doc_type="quotation")
    assert r.status_code == 422, r.text
    assert (await fa.state(inv))["doc_type"] == "invoice" and (await fa.asset(van)).get("sale") in (None, {})
    assert await _journals_of(fa, inv) == []

    # The refused edits leave the sale whole through its document's lifecycle.
    await _finalize(fa, inv)
    assert (await _sale(fa, car))["state"] == "sold"
    await _assert_clearing(fa, car, "10000")
    await fa.ok(await _doc(fa, inv, "revert-to-draft"))
    assert (await _sale(fa, car))["state"] == "pending"
    r = await _patch(fa, inv, [{"name": "Delivery", "quantity": 1, "unit_price": 50.0}])
    assert r.status_code == 409 and "cancel the sale" in r.json()["detail"], r.text
    await _finalize(fa, inv)
    await fa.ok(await _doc(fa, inv, "void"))
    assert (await fa.asset(car))["status"] == "active"
    await fa.ok(await _doc(fa, inv, "unvoid"))
    assert (await _sale(fa, car))["state"] == "sold"
    await fa.fold_matches(car)


async def test_f10_cancelling_the_sale_frees_the_draft(fa):
    """The way back the refusal names: cancelling the sale voids the draft and frees the asset."""
    await _managed_from(fa)
    car = await _contributed(fa)
    inv = await _sell(fa, car)
    await fa.operate(f"/assets/{car}/sale/cancel", {}, key="cancel")
    assert (await fa.state(inv))["status"] == "void"
    got = await fa.asset(car)
    assert got["status"] == "active" and not got.get("sale")
