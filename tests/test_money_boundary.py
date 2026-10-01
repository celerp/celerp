# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Inventory cost keeps its precision; it becomes money only at a monetary boundary.

A lot's basis is internal state and is never cut to cents. An automatic journal
entry rounds each line to the company currency and is refused if it does not
balance, and every multi-line producer builds its lines so that it does.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.projections import Projection
from celerp.services import auto_je
from test_cost_restatement import _cogs_adjustments, _sell, _set_cost, _state


async def _company(session, currency: str) -> dict:
    cid, uid = uuid.uuid4(), uuid.uuid4()
    session.add(Company(id=cid, name="MoneyCo", slug=f"money-{cid.hex[:8]}", settings={"currency": currency}))
    session.add(User(id=uid, email=f"admin-{cid.hex[:8]}@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    from test_helpers import make_authed_token, provision_company_books
    await provision_company_books(session, cid)
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": uid}


async def _je(session, auth, je_id: str) -> list[tuple[str, float, float]]:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})
    assert row is not None, je_id
    return [(e["account"], e["debit"], e["credit"]) for e in row.state["entries"]]


def _balanced(lines) -> bool:
    return round(sum(d for _, d, _ in lines), 10) == round(sum(c for _, _, c in lines), 10)


async def _item(client, auth, cost_total: float, qty: float) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"MB-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": qty, "sell_by": "piece",
        "status": "available", "cost_total": cost_total,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


# -- The journal-entry boundary ------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("currency, raw, stored", [("KWD", 1.23456, 1.235), ("CLF", 1.23456, 1.2346), ("USD", 1.23456, 1.23)])
async def test_auto_je_lines_round_to_the_company_currency(session, currency, raw, stored):
    auth = await _company(session, currency)
    await auto_je._emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id="je:auto:t:1",
        idem_create=f"t:{uuid.uuid4()}", idem_posted=f"t:{uuid.uuid4()}", memo="Test",
        entries=[{"account": "1130-P", "debit": raw, "credit": 0.0},
                 {"account": "2110", "debit": 0.0, "credit": raw}],
        metadata_={},
    )
    await session.commit()
    assert await _je(session, auth, "je:auto:t:1") == [("1130-P", stored, 0.0), ("2110", 0.0, stored)]


@pytest.mark.asyncio
async def test_auto_je_unbalanced_after_rounding_is_refused(session):
    auth = await _company(session, "USD")
    with pytest.raises(auto_je.UnbalancedJournalEntry):
        await auto_je._emit_auto_posted_je(
            session, company_id=auth["company_id"], user_id=auth["user_id"], je_id="je:auto:t:2",
            idem_create=f"t:{uuid.uuid4()}", idem_posted=f"t:{uuid.uuid4()}", memo="Test",
            entries=[{"account": "1130-P", "debit": 0.005, "credit": 0.0},
                     {"account": "1130-FRT", "debit": 0.0, "credit": 0.0025},
                     {"account": "1130-DTY", "debit": 0.0, "credit": 0.0025}],
            metadata_={},
        )


@pytest.mark.asyncio
async def test_half_cent_landed_capitalisation_stays_balanced(session):
    auth = await _company(session, "USD")
    await auto_je.create_for_landed_capitalisation(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:bill-1",
        landed_by_kind={"freight": 0.005, "duty": 0.005}, landed_by_account={"1130-P": 0.01},
        receive_suffix="r1",
    )
    await session.commit()
    lines = await _je(session, auth, "je:auto:doc:bill-1:landed-cap:r1")
    assert lines == [("1130-P", 0.02, 0.0), ("1130-FRT", 0.0, 0.01), ("1130-DTY", 0.0, 0.01)]


@pytest.mark.asyncio
async def test_half_cent_manufacturing_completion_stays_balanced(session):
    auth = await _company(session, "USD")
    await auto_je.create_for_mfg_completed(
        session, company_id=auth["company_id"], user_id=auth["user_id"], order_id="mo:1",
        inputs={"1130-P": 0.015}, waste_cost=0.005, outputs={"1130-P": 1.0},
    )
    await session.commit()
    lines = await _je(session, auth, "je:auto:mo:1:mfg")
    assert lines == [("1130-P", 0.01, 0.0), ("5100", 0.01, 0.0), ("1130-P", 0.0, 0.02)]
    assert _balanced(lines)


def _bill(total: float, lines: list[tuple[float, str]], **fields) -> dict:
    return {"doc_type": "bill", "total": total, **fields, "line_items": [
        {"description": "Line", "sku": "SKU" if receive_as == "stock" else "", "receive_as": receive_as,
         "quantity": 1, "unit_price": amount, "line_total": amount}
        for amount, receive_as in lines
    ]}


@pytest.mark.asyncio
async def test_discounted_bill_reduces_each_line_by_its_share(session):
    auth = await _company(session, "USD")
    await auto_je.create_for_bill_conversion(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b1",
        doc=_bill(140.0, [(100.0, "stock"), (50.0, "expense")], discount=10),
    )
    await session.commit()
    assert await _je(session, auth, "je:auto:doc:b1:bill") == [
        ("1130-P", 93.33, 0.0), ("6950", 46.67, 0.0), ("2110", 0.0, 140.0)]


@pytest.mark.asyncio
async def test_bill_parts_above_its_total_are_refused(session):
    auth = await _company(session, "USD")
    with pytest.raises(auto_je.UnbalancedJournalEntry):
        await auto_je.create_for_bill_conversion(
            session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b2",
            doc=_bill(120.0, [(100.0, "stock")]),
        )


@pytest.mark.asyncio
async def test_bill_total_above_its_lines_is_refused_with_a_message(client, session):
    auth = await _company(session, "USD")
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "bill", "total": 120,
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100, "receive_as": "expense"}],
    })
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 422, r.text
    assert "do not add up to its total of 120" in r.json()["detail"]


@pytest.mark.asyncio
async def test_foreign_bill_conversion_rounding_stays_balanced(session):
    auth = await _company(session, "USD")
    await auto_je.create_for_bill_conversion(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b3",
        doc=_bill(0.15, [(0.05, "expense"), (0.05, "expense"), (0.05, "expense")],
                  currency="EUR", conversion_rate=1.1),
    )
    await session.commit()
    lines = await _je(session, auth, "je:auto:doc:b3:bill")
    assert lines[-1] == ("2110", 0.0, 0.17)
    assert _balanced(lines)


@pytest.mark.asyncio
async def test_foreign_invoice_revenue_is_the_receivable_less_tax(session):
    auth = await _company(session, "USD")
    await auto_je.create_for_doc_finalized(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:i1",
        doc={"doc_type": "invoice", "currency": "EUR", "conversion_rate": 1.15, "total": 0.15, "tax": 0.05,
             "line_items": []},
    )
    await session.commit()
    assert await _je(session, auth, "je:auto:doc:i1:fin") == [
        ("1120", 0.17, 0.0), ("4100", 0.0, 0.11), ("2120", 0.0, 0.06)]


# -- Basis precision -------------------------------------------------------------

@pytest.mark.asyncio
async def test_kwd_basis_sale_and_restatement_keep_fils(client, session):
    auth = await _company(session, "KWD")
    item_id = await _item(client, auth, 10.1234, 1)
    assert (await _state(session, auth, item_id))["cost_total"] == 10.1234
    doc = await _sell(client, session, auth, item_id)
    fin = await _je(session, auth, f"je:auto:{doc}:fin")
    assert ("5100", 10.123, 0.0) in fin
    assert (await _set_cost(client, auth, item_id, 10.1274)).status_code == 200
    adjustments = (await _cogs_adjustments(session, auth, doc)).values()
    assert [[(e["account"], e["debit"], e["credit"]) for e in s["entries"]] for s in adjustments] == [
        [("5100", 0.004, 0.0), ("1130-P", 0.0, 0.004)]
    ]


@pytest.mark.asyncio
async def test_count_down_and_back_restores_the_exact_basis(client, session):
    auth = await _company(session, "USD")
    item_id = await _item(client, auth, 100.0, 7)
    for qty in (3, 7):
        r = await client.post(f"/items/{item_id}/adjust", headers=auth["headers"], json={"new_qty": qty})
        assert r.status_code == 200, r.text
    state = await _state(session, auth, item_id)
    assert (state["cost_base"], state["cost_total"]) == (100.0, 100.0)
