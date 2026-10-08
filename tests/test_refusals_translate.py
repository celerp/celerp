# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Receiving, returning, closing and taking back refuse in the user's language.

Each refusal carries a message key and its params, so the page shows it translated and
in plain words, with no internal status names. A take-back that fails for several lines
gives each reason translated too, not only the sentence around them."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from line_actions_support import doc, h, line, line_ids, lot  # noqa: F401
from test_helpers import default_location_id
from ui import i18n

pytestmark = pytest.mark.asyncio


def _in_spanish(r, status: int) -> str:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key"), detail
    i18n.set_lang("es")
    try:
        return i18n.refusal_text(detail)
    finally:
        i18n.set_lang("en")


async def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


@pytest_asyncio.fixture
async def owner(client) -> dict:
    """Headers for the owner of a newly registered company, which has a stock location."""
    r = await client.post("/auth/register", json={
        "company_name": "Refusals Co", "email": f"a-{uuid.uuid4().hex[:8]}@refusals.test", "name": "Admin",
        "password": "pwvalid1"})
    return {"Authorization": f"Bearer {(await _ok(r))['access_token']}"}


async def _bill(client, h, sku: str, qty: float) -> str:
    bill = (await _ok(await client.post("/docs", headers=h, json={"doc_type": "bill", "line_items": [
        {"sku": sku, "name": sku, "quantity": qty, "unit_price": 10, "line_total": 10 * qty,
         "receive_as": "stock", "sell_by": "piece"}], "subtotal": 10 * qty, "tax": 0, "total": 10 * qty})))["id"]
    await _ok(await client.post(f"/docs/{bill}/finalize", headers=h))
    return bill


async def _receive(client, h, bill: str, sku: str, qty: float):
    return await client.post(f"/docs/{bill}/receive", headers=h, json={
        "location_id": await default_location_id(client, h), "received_items": [
            {"po_line_index": 0, "sku": sku, "name": sku, "quantity_received": qty, "receive_as": "stock"}]})


async def test_receiving_part_of_a_piece_is_refused_in_the_users_language(client, owner):
    h = owner
    bill = await _bill(client, h, "RT-DEC", 2)
    text = _in_spanish(await _receive(client, h, bill, "RT-DEC", 1.5), 422)
    assert text == "RT-DEC: 1.5 es más preciso de lo que permite esta unidad (máximo 0 decimales)."


async def test_receiving_more_than_the_line_is_refused_in_the_users_language(client, owner):
    h = owner
    bill = await _bill(client, h, "RT-OVER", 10)
    await _ok(await _receive(client, h, bill, "RT-OVER", 6))
    text = _in_spanish(await _receive(client, h, bill, "RT-OVER", 5), 422)
    assert text == ("RT-OVER: esta línea es de 10 y ya se han recibido 6, así que se pueden recibir "
                    "como máximo 4 más. Cambie la línea primero para recibir más.")


async def test_returning_on_a_void_bill_is_refused_in_plain_words(client, owner):
    h = owner
    bill = await _bill(client, h, "RT-VOID", 1)
    await _ok(await client.post(f"/docs/{bill}/void", headers=h, json={"reason": "wrong supplier"}))
    r = await client.post(f"/docs/{bill}/return-items", headers=h, json={"items": [
        {"item_id": "item:none", "quantity_returned": 1}]})
    assert _in_spanish(r, 409) == (
        "No se puede devolver nada en este documento porque está Anulado. Los productos vuelven al "
        "proveedor solo después de recibirlos, desde un documento que sigue abierto.")


async def test_closing_a_memo_with_goods_out_is_refused_in_the_users_language(client, h):
    a = await lot(client, h, "RT-MEMO", 1)
    memo = await doc(client, h, [line(a, 1, sku="RT-MEMO")], doc_type="memo")
    (l0,) = await line_ids(client, h, memo)
    await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [l0]}))
    text = _in_spanish(await client.post(f"/docs/{memo}/close", headers=h, json={}), 409)
    assert text == ("Este memo no se puede cerrar mientras 1 producto(s) sigan fuera o reservados en él. "
                    "Véndalos o devuélvalos primero.")


async def test_taking_back_more_than_went_out_is_refused_in_the_users_language(client, h):
    a = await lot(client, h, "RT-BACK", 1)
    memo = await doc(client, h, [line(a, 1, sku="RT-BACK")], doc_type="memo")
    (l0,) = await line_ids(client, h, memo)
    await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [l0]}))
    r = await client.post(f"/docs/{memo}/set-available", headers=h,
                          json={"line_ids": [l0], "quantities": {l0: 2}})
    assert r.json()["detail"]["message_key"] == "lines.cannot_set_available"
    assert _in_spanish(r, 422) == ("No se puede marcar como disponible: "
                                   "RT-BACK: no se pueden devolver 2 de 1 que salieron")


async def test_each_take_back_reason_is_translated(client, h):
    a = await lot(client, h, "RT-TWO-A", 1)
    b = await lot(client, h, "RT-TWO-B", 1)
    memo = await doc(client, h, [line(a, 1, sku="RT-TWO-A"), line(b, 1, sku="RT-TWO-B")], doc_type="memo")
    l0, l1 = await line_ids(client, h, memo)
    await _ok(await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_ids": [l0]}))
    r = await client.post(f"/docs/{memo}/set-available", headers=h,
                          json={"line_ids": [l0, l1], "quantities": {l0: 0}})
    assert _in_spanish(r, 422) == ("No se puede marcar como disponible: "
                                   "RT-TWO-B: no hay nada reservado ni fuera en esta línea; "
                                   "RT-TWO-A: la cantidad devuelta debe ser mayor que cero")
