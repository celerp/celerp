# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A shipment Celerp cannot place on a line of the invoice is refused, and the refusal names
the way out only where the app takes it. One rule says why an invoice cannot go back to draft
(celerp_docs.routes.revert_check): Revert to Draft obeys it, the document page offers the
button from it, and the refusal's advice reads it, as the invoice stands once the refusal has
undone the action. Nothing refused -> Revert to Draft, then Issue Invoice; payments the only
reason -> void them first; anything else -> report it."""
from __future__ import annotations

from pathlib import Path

import pytest
from fasthtml.common import to_xml

import ui.i18n
from ui.i18n import t
from test_cost_follows_goods import _doc_number
from test_credit_note_settlement_owned import _cn
from test_invoice_unshipped_books import _ok
from test_reissued_invoice_cogs import _invoice, _line_ids, _ship, _state, _stones, drop_lines_as_before
from test_set_aside_goods_follow_the_lot import _per_doc
from ui.routes.documents import _doc_detail

pytestmark = pytest.mark.asyncio

_REVERT = "/action/revert_to_draft"


async def _drop_returned_line(client, session, auth, n: int, out: int = 0, before_drop=None):
    """An issued invoice of ``n`` stones whose last line shipped, came back (Set as available)
    and was then removed from the issued invoice by an earlier release's save, with ``out``
    lines before it still shipped.
    ``before_drop`` runs on the invoice just before the line is removed."""
    stones = (await _stones(client, auth))[:n]
    doc = await _invoice(client, auth, stones)
    ids = await _line_ids(client, auth, doc)
    r = await _ship(client, auth, doc, ids[n - 1 - out:])
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": ids[n - 1:]})
    if before_drop is not None:
        await before_drop(doc)
    await drop_lines_as_before(client, session, auth, doc, n - 1)
    return doc, stones, ids


async def _pay(client, auth, doc: str, amount: float) -> None:
    await _ok(client, auth, f"/docs/{doc}/payment", {"amount": amount, "payment_date": "2026-03-02",
                                                     "bank_account": "1111"})


async def _refused_ship(client, session, auth, doc: str, line_ids: list[str], stones, key: str) -> None:
    before = await _per_doc(session, auth)
    r = await _ship(client, auth, doc, line_ids)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    params = {"doc": await _doc_number(session, auth, doc), "items": stones[-1][1]}
    assert (detail["message_key"], detail["params"]) == (key, params)
    assert detail["message"] == t(key, **params)
    assert await _per_doc(session, auth) == before


async def _revert_refused(client, auth, doc: str) -> list[str]:
    return (await _state(client, auth, doc))["revert_refused"]


async def test_part_paid_invoice_names_voiding_the_payments_and_that_way_out_works(client, session, auth):
    doc, stones, ids = await _drop_returned_line(client, session, auth, 2)
    await _pay(client, auth, doc, 1)
    await _refused_ship(client, session, auth, doc, ids[:1], stones, "error.cogs_line_unknown_paid")
    assert await _revert_refused(client, auth, doc) == ["payments"]
    for lang in [p.stem for p in (Path(ui.i18n.__file__).parent / "locales").glob("*.json")]:
        for label in ("documents.void_this_payment", "doc.revert_to_draft", "documents.finalize_label_invoice"):
            assert t(label, lang) in t("error.cogs_line_unknown_paid", lang, doc="X", items="Y"), (lang, label)
    payment = (await _state(client, auth, doc))["payments"][0]["index"]
    await _ok(client, auth, f"/docs/{doc}/void-payment", {"payment_index": payment})
    assert await _revert_refused(client, auth, doc) == []
    await _ok(client, auth, f"/docs/{doc}/revert-to-draft")
    await _ok(client, auth, f"/docs/{doc}/finalize")
    r = await _ship(client, auth, doc, await _line_ids(client, auth, doc))
    assert r.status_code == 200, r.text


async def test_paid_invoice_names_voiding_the_payments(client, session, auth):
    doc, stones, ids = await _drop_returned_line(client, session, auth, 2)
    await _pay(client, auth, doc, float((await _state(client, auth, doc))["amount_outstanding"]))
    assert (await _state(client, auth, doc))["status"] == "paid"
    await _refused_ship(client, session, auth, doc, ids[:1], stones, "error.cogs_line_unknown_paid")
    assert await _revert_refused(client, auth, doc) == ["payments"]


async def test_credited_invoice_asks_for_a_report(client, session, auth):
    async def credit(doc):
        await _cn(client, auth, doc, 10.0)

    doc, stones, ids = await _drop_returned_line(client, session, auth, 2, before_drop=credit)
    await _refused_ship(client, session, auth, doc, ids[:1], stones, "error.cogs_line_unknown_out")
    assert await _revert_refused(client, auth, doc) == ["credit_note"]


async def test_another_line_still_out_asks_for_a_report_on_every_step(client, session, auth):
    """Line 2 is still out, so Revert to Draft is refused and Set as available on line 2 meets
    the same refusal: no step the app takes clears it, and every answer says to report it,
    Revert to Draft's too, rather than sending the user to Set as available."""
    doc, stones, ids = await _drop_returned_line(client, session, auth, 3, out=1)
    await _refused_ship(client, session, auth, doc, ids[:1], stones, "error.cogs_line_unknown_out")
    assert await _revert_refused(client, auth, doc) == ["out"]
    r = await client.post(f"/docs/{doc}/set-available", headers=auth["headers"], json={"line_ids": ids[1:2]})
    assert (r.status_code, r.json()["detail"]["message_key"]) == (409, "error.cogs_line_unknown_out"), r.text
    r = await client.post(f"/docs/{doc}/revert-to-draft", headers=auth["headers"], json={})
    assert (r.status_code, r.json()["detail"]["message_key"]) == (409, "error.cogs_line_unknown_out"), r.text


async def test_nothing_refused_names_revert_and_reissue(client, session, auth):
    doc, stones, ids = await _drop_returned_line(client, session, auth, 2)
    await _refused_ship(client, session, auth, doc, ids[:1], stones, "error.cogs_line_unknown")
    assert await _revert_refused(client, auth, doc) == []


async def _states(client, auth) -> dict[str, tuple[str, list[str]]]:
    """One document per state, with the reasons the rule gives for it."""
    states = {}
    stones = await _stones(client, auth)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"item_id": stones[0][0], "sku": stones[0][1], "quantity": 1,
                                               "unit_price": 100.0, "sell_by": "piece"}], "total": 100.0})
    states["draft"] = (r.json()["id"], ["status"])
    states["issued"] = (await _invoice(client, auth, stones[1:2]), [])
    part = await _invoice(client, auth, stones[2:3])
    await _pay(client, auth, part, 1)
    states["part_paid"] = (part, ["payments"])
    paid = await _invoice(client, auth, stones[3:4])
    await _pay(client, auth, paid, 100)
    states["paid"] = (paid, ["payments"])
    credited = await _invoice(client, auth, stones[4:5])
    await _cn(client, auth, credited, 10.0)
    states["credited"] = (credited, ["credit_note"])
    out = await _invoice(client, auth, stones[5:6])
    assert (await _ship(client, auth, out, await _line_ids(client, auth, out))).status_code == 200
    states["out"] = (out, ["out"])
    return states


async def test_the_button_and_the_api_agree_on_every_state(client, session, auth):
    for name, (doc, reasons) in (await _states(client, auth)).items():
        state = await _state(client, auth, doc)
        assert state["revert_refused"] == reasons, name
        offered = _REVERT in to_xml(_doc_detail({**state, "entity_id": doc}))
        r = await client.post(f"/docs/{doc}/revert-to-draft", headers=auth["headers"], json={})
        assert (r.status_code == 200) == (reasons == []), (name, r.text)
        assert offered or r.status_code != 200, name
        if not offered:
            assert r.status_code == 409, name

