# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An amount or quantity that is not a real number is refused with a clear error.

JSON lets a client send NaN or Infinity where a price, payment or quantity belongs.
Each is turned away as invalid input before anything is recorded.
"""
from __future__ import annotations

import uuid

import pytest

from test_operation_retries import DATE, _final


async def _item(client, auth) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"NF-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# Each case returns the path and a JSON body with the placeholder BAD where the number goes.
async def _doc_line(client, auth):
    return "/docs", '{"doc_type": "invoice", "line_items": [{"name": "Lot", "quantity": 1, "unit_price": BAD}]}'


async def _doc_total(client, auth):
    return "/docs", '{"doc_type": "invoice", "total": BAD}'


async def _payment(client, auth):
    inv = await _final(client, auth, "invoice")
    return f"/docs/{inv}/payment", f'{{"amount": BAD, "payment_date": "{DATE}", "bank_account": "1111"}}'


async def _bulk_payment(client, auth):
    inv = await _final(client, auth, "invoice")
    return "/docs/bulk-payment", f'{{"doc_ids": ["{inv}"], "amount": BAD, "payment_date": "{DATE}"}}'


async def _apply_credit(client, auth):
    inv = await _final(client, auth, "invoice")
    cn = await _final(client, auth, "credit_note", 30.0)
    return f"/docs/{cn}/apply-to-invoice", f'{{"target_doc_id": "{inv}", "amount": BAD, "date": "{DATE}"}}'


async def _new_item(client, auth):
    return "/items", f'{{"sku": "NF-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": BAD}}'


async def _adjust(client, auth):
    return f"/items/{await _item(client, auth)}/adjust", '{"new_qty": BAD}'


async def _transfer(client, auth):
    return "/accounting/transfers", \
        f'{{"from_bank_id": "a", "to_bank_id": "b", "amount": BAD, "date": "{DATE}"}}'


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("case", [_doc_line, _doc_total, _payment, _bulk_payment, _apply_credit,
                                  _new_item, _adjust, _transfer])
@pytest.mark.asyncio
async def test_a_number_that_is_not_finite_is_refused(client, auth, case, bad):
    path, body = await case(client, auth)
    r = await client.post(path, headers={**auth["headers"], "Content-Type": "application/json"},
                          content=body.replace("BAD", bad))
    assert r.status_code == 422, r.text
