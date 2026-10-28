# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A tax refusal on a document past draft names only a way back to a draft that the document allows.

Only a draft's taxes can be changed, so a refusal of a tax still to be chosen, or of one that can't
be read, tells the user how to get back to one. Revert to Draft refuses while payments, received
goods or lines out on memo or sold remain, never takes a purchase order that received goods, and
takes a bill made from an order back to the order. Each case below builds the document through the
API, reads the advice the refusal gives (draft_route), then follows that advice step by step through
the API: every step must succeed and leave a draft whose tax can be chosen, or a void document
when the advice is to make a new one.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.projections import Projection
from celerp_docs.doc_money import draft_route, refuse_unset_line_tax, unreadable_tax_refusal
from test_receipt_accounting import _doc, _finalize, _receive, _return
from test_supplier_return_settles_bill import _restated_bill
from ui.i18n import t

DATE = "2026-01-15"


async def _state(session, auth, doc_id: str) -> dict:
    session.expire_all()
    return dict((await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc_id})).state)


async def _post(client, auth, doc_id: str, action: str, body: dict | None = None):
    return await client.post(f"/docs/{doc_id}/{action}", headers=auth["headers"], json=body or {})


def _goods(sku: str, qty: float = 10) -> dict:
    return {"sku": sku, "name": "Beads", "quantity": qty, "unit_price": 12.0}


def _sku() -> str:
    return f"DR-{uuid.uuid4().hex[:6]}"


async def _sent_order(client, session, auth) -> str:
    doc = await _doc(client, auth, "purchase_order", [_goods(_sku())])
    await _finalize(client, auth, doc)
    return doc


async def _received_order(client, session, auth) -> str:
    sku = _sku()
    doc = await _doc(client, auth, "purchase_order", [_goods(sku)])
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 4})
    assert r.status_code == 200, r.text
    return doc


async def _received_consignment(client, session, auth) -> str:
    sku = _sku()
    doc = await _doc(client, auth, "consignment_in", [_goods(sku)])
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 10})
    assert r.status_code == 200, r.text
    return doc


async def _received_bill(client, session, auth) -> str:
    return (await _restated_bill(client, session, auth, from_order=False, cost=120.0))[0]


async def _bill_from_order(client, session, auth) -> str:
    return (await _restated_bill(client, session, auth, from_order=True, cost=120.0))[0]


async def _bill_received_after_its_order(client, session, auth) -> str:
    sku = _sku()
    doc = await _doc(client, auth, "purchase_order", [_goods(sku)])
    line = {"po_line_index": 0, "sku": sku, "name": "Beads"}
    assert (await _receive(client, auth, doc, {**line, "quantity_received": 4})).status_code == 200
    await _finalize(client, auth, doc)
    assert (await _receive(client, auth, doc, {**line, "quantity_received": 6})).status_code == 200
    return doc


async def _invoice(client, auth, lines: list[dict], doc_type: str = "invoice") -> str:
    doc = await _doc(client, auth, doc_type, lines, contact_id="customer:1")
    await _finalize(client, auth, doc)
    return doc


async def _paid_invoice(client, session, auth) -> str:
    doc = await _invoice(client, auth, [{"name": "Setting", "quantity": 1, "unit_price": 50.0}])
    r = await _post(client, auth, doc, "payment", {"amount": 50.0, "payment_date": DATE, "bank_account": "1111"})
    assert r.status_code == 200, r.text
    return doc


async def _delivered_invoice(client, session, auth, doc_type: str = "invoice") -> str:
    sku = _sku()
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Ring", "quantity": 1, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    doc = await _invoice(client, auth, [{"item_id": r.json()["id"], "sku": sku, "name": "Ring", "quantity": 1,
                                         "unit_price": 50.0, "sell_by": "piece"}], doc_type)
    lines = [li["line_id"] for li in (await _state(session, auth, doc))["line_items"]]
    r = await _post(client, auth, doc, "fulfill-lines", {"line_ids": lines})
    assert r.status_code == 200, r.text
    return doc


async def _memo_out(client, session, auth) -> str:
    return await _delivered_invoice(client, session, auth, "memo")


async def _void_invoice(client, session, auth) -> str:
    doc = await _invoice(client, auth, [{"name": "Setting", "quantity": 1, "unit_price": 50.0}])
    assert (await _post(client, auth, doc, "void")).status_code == 200
    return doc


_R = "documents.tax_route_"
CASES = {
    "sent_order": (_sent_order, [_R + "revert"]),
    "received_order": (_received_order, [_R + "return", _R + "void"]),
    "received_consignment": (_received_consignment, [_R + "return", _R + "revert"]),
    "received_bill": (_received_bill, [_R + "return", _R + "revert"]),
    "bill_from_order": (_bill_from_order, [_R + "return", _R + "void_order"]),
    "bill_received_after_its_order": (_bill_received_after_its_order, [_R + "return", _R + "void_order"]),
    "paid_invoice": (_paid_invoice, [_R + "payments", _R + "revert"]),
    "delivered_invoice": (_delivered_invoice, [_R + "available", _R + "revert"]),
    "memo_out": (_memo_out, [_R + "available", _R + "revert"]),
    "void_invoice": (_void_invoice, [_R + "new"]),
}


async def _return_all(client, session, auth, doc: str) -> None:
    for parcel in (await _state(session, auth, doc)).get("received_item_ids") or []:
        qty = (await _state(session, auth, parcel)).get("quantity") or 0
        if qty:
            r = await _return(client, auth, doc, parcel, qty)
            assert r.status_code == 200, r.text


async def _follow(client, session, auth, doc: str, step: str) -> None:
    """Take one step of the advice the way a user would, asserting it is allowed."""
    state = await _state(session, auth, doc)
    if step == _R + "payments":
        r = await _post(client, auth, doc, "void-payment", {"payment_index": 0, "refund_date": DATE})
    elif step == _R + "return":
        return await _return_all(client, session, auth, doc)
    elif step == _R + "available":
        r = await _post(client, auth, doc, "set-available", {"line_ids": [li["line_id"] for li in state["line_items"]]})
    elif step == _R + "revert":
        r = await _post(client, auth, doc, "revert-to-draft")
        assert r.status_code == 200, r.text
        assert (await _state(session, auth, doc))["status"] == "draft"
        return None
    elif step == _R + "void":
        r = await _post(client, auth, doc, "void")
    elif step == _R + "void_order":
        r = await _post(client, auth, doc, "void")
        if r.status_code == 409 and r.json()["detail"]["message_key"] == "docs.void_order_receipt":
            r = await _post(client, auth, doc, "revert-to-draft")
            assert r.status_code == 200, r.text
            assert (await _state(session, auth, doc))["doc_type"] == "purchase_order"
            r = await _post(client, auth, doc, "void")
    else:
        assert step == _R + "new", step
        r = await _post(client, auth, doc, "revert-to-draft")
        assert r.status_code == 409, r.text
        return None
    assert r.status_code == 200, (step, r.text)


@pytest.mark.parametrize("case", list(CASES))
async def test_the_refusal_advice_leads_back_to_a_draft_the_document_allows(client, session, auth, case):
    build, expected = CASES[case]
    doc = await build(client, session, auth)
    state = await _state(session, auth, doc)
    assert state["status"] != "draft"
    assert draft_route(state) == expected

    for step in expected:
        await _follow(client, session, auth, doc, step)

    if expected[-1] != _R + "new":
        assert (await _state(session, auth, doc))["status"] == ("draft" if expected[-1] == _R + "revert" else "void")


def _advice(state: dict) -> str:
    return " ".join(t(key) for key in draft_route(state))


def route(*steps: str, locale: str | None = None) -> str:
    """The advice text of these draft_route steps, named without their common prefix."""
    return " ".join(t(_R + step, locale) for step in steps)


@pytest.mark.parametrize("case", ["received_order", "paid_invoice"])
async def test_both_tax_refusals_past_draft_end_with_that_advice(client, session, auth, case):
    doc = await CASES[case][0](client, session, auth)
    state = await _state(session, auth, doc)
    lines = [{**state["line_items"][0], "taxes": None}]

    with pytest.raises(HTTPException) as unset:
        refuse_unset_line_tax(lines, state)
    unreadable = unreadable_tax_refusal("VAT ?", state)

    assert unset.value.detail == t("documents.err_line_tax_unset_issued", line=1, route=_advice(state))
    assert unreadable.detail == t("documents.err_tax_unreadable_issued", tax="VAT ?", route=_advice(state))


async def test_a_stranded_order_refusing_more_goods_names_return_then_void(client, session, auth):
    """A purchase order that received goods while a line's tax was still to be chosen (before the
    receipt checked it) refuses the next receipt with the way out it actually has."""
    doc = await _received_order(client, session, auth)
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})
    row.state["line_items"][0]["taxes"] = None
    flag_modified(row, "state")
    await session.commit()
    state = await _state(session, auth, doc)
    sku = state["line_items"][0]["sku"]

    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 1})

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("documents.err_line_tax_unset_issued", line=1, route=_advice(state))
    for step in draft_route(state):
        await _follow(client, session, auth, doc, step)
    assert (await _state(session, auth, doc))["status"] == "void"


def test_only_the_selector_words_a_tax_refusal_past_draft():
    """Every past-draft tax refusal goes through doc_money (draft_route), so no caller words its
    own way back to a draft that the document might refuse."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    keys = ('"documents.err_line_tax_unset_issued"', '"documents.err_tax_unreadable_issued"', '"documents.tax_route_')
    users = sorted(str(p.relative_to(root)) for top in ("celerp", "default_modules", "ui")
                   for p in (root / top).rglob("*.py") if any(k in p.read_text() for k in keys))
    assert users == ["default_modules/celerp-docs/celerp_docs/doc_money.py"], users
