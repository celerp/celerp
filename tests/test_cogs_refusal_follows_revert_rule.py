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
from test_reissued_invoice_cogs import (_invoice, _line_ids, _rewrite_lines, _rewrite_state, _ship,
                                        _ship_in_two_batches, _state, _stones, drop_lines_as_before)
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



# ── Every refusal names a step the app takes in that state ─────────────────────────────────

async def _legacy(client, session, auth, name: str) -> dict:
    """A document in one of the states an earlier release's saves left behind, with the
    lines still out on it and the credit notes raised on it."""
    notes: list[str] = []

    async def credit(doc):
        notes.append(await _cn(client, auth, doc, 10.0))

    if name == "lot_out_off_lines":
        stones = (await _stones(client, auth))[:2]
        doc, first, _second = await _ship_in_two_batches(client, auth, stones)
        await _ok(client, auth, f"/docs/{doc}/set-available", {"line_ids": [first]})
        await _rewrite_state(session, auth, doc, lambda st: {"fulfilled_items": [], "fulfillment_status": "unfulfilled"})
        await _ok(client, auth, f"/docs/{doc}/revert-to-draft")
        await _rewrite_lines(session, auth, doc, lambda lines: lines[:1])
        await _ok(client, auth, f"/docs/{doc}/finalize")
        return {"doc": doc, "out": [], "notes": notes}
    out = 1 if name == "another_out" else 0
    doc, _stones_, ids = await _drop_returned_line(client, session, auth, 3 if out else 2, out=out,
                                                   before_drop=credit if name == "credited" else None)
    if name in ("part_paid", "paid"):
        await _pay(client, auth, doc, 1 if name == "part_paid"
                   else float((await _state(client, auth, doc))["amount_outstanding"]))
    return {"doc": doc, "out": ids[1:2] if out else [], "notes": notes}


async def _post_action(client, auth, path: str, body: dict):
    return await client.post(path, headers=auth["headers"], json=body)


async def _void_payments(client, auth, s: dict) -> bool:
    for payment in (await _state(client, auth, s["doc"])).get("payments") or []:
        r = await _post_action(client, auth, f"/docs/{s['doc']}/void-payment", {"payment_index": payment["index"]})
        if r.status_code != 200:
            return False
    return True


# The refused actions, each as the page sends it.
_ACTIONS = {
    "ship": lambda c, a, s, ids: _ship(c, a, s["doc"], ids[:1]),
    "set_available": lambda c, a, s, ids: _post_action(c, a, f"/docs/{s['doc']}/set-available", {"line_ids": s["out"]}),
    "revert": lambda c, a, s, ids: _post_action(c, a, f"/docs/{s['doc']}/revert-to-draft", {}),
    "void": lambda c, a, s, ids: _post_action(c, a, f"/docs/{s['doc']}/void", {"reason": "test"}),
}

# The step each refusal's advice names. A refusal missing here fails the guard until its
# advice is classified; REPORT is the report text, which names no step.
REPORT = "report"
_STEPS = {
    "error.cogs_line_unknown": "revert",
    "error.cogs_line_unknown_paid": "void_payments",
    "documents.err_revert_has_payments": "void_payments",
    "Cannot void a document with payments; void the payments first": "void_payments",
    "docs.revert_under_credit_note": "void_credit_notes",
    "docs.void_under_credit_note": "void_credit_notes",
    "docs.revert_lines_out": "set_available",
    "documents.err_void_delivered": "set_available",
    "error.cogs_line_unknown_out": REPORT,
}


async def _take(client, auth, step: str, s: dict, ids: list[str]) -> bool:
    if step == "void_payments":
        return await _void_payments(client, auth, s)
    if step == "void_credit_notes":
        return [(await _post_action(client, auth, f"/docs/{cn}/void", {"reason": "test"})).status_code
                for cn in s["notes"]] == [200] * len(s["notes"])
    return (await _ACTIONS[step](client, auth, s, ids)).status_code == 200


def _refusal_id(detail) -> str:
    return detail["message_key"] if isinstance(detail, dict) else detail


@pytest.mark.parametrize("name", ["nothing", "part_paid", "paid", "credited", "another_out", "lot_out_off_lines"])
async def test_every_refusal_names_a_step_accepted_in_that_state(client, session, auth, name):
    """Generated over the legacy states and every action the document page offers on them:
    each refusal's advice, followed one step, is accepted there, or the refusal asks for a
    report. A new refusal fails here until its advice is classified."""
    s = None
    for action in _ACTIONS:
        s = s or await _legacy(client, session, auth, name)
        if action == "set_available" and not s["out"]:
            continue
        ids = await _line_ids(client, auth, s["doc"])
        r = await _ACTIONS[action](client, auth, s, ids)
        if r.status_code == 200:
            s = None
            continue
        assert r.status_code == 409, (name, action, r.text)
        refused = _refusal_id(r.json()["detail"])
        assert refused in _STEPS, f"{name}/{action}: classify the advice of {refused!r}"
        step = _STEPS[refused]
        if step == REPORT:
            continue
        assert await _take(client, auth, step, s, ids), (name, action, refused, step)
        s = None
