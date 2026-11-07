# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a billing request answers: the bill carries every money input the agreement holds,
an answer never offers a void document as billed, a count never counts one document twice,
and Bill early bills only a period whose bill date has not come."""
from __future__ import annotations

import pytest

from agreement_kit import agreement, bill_early, billed_docs, freeze_day, generate_due, register, template
from ui.i18n import sentence, t

pytestmark = pytest.mark.asyncio

MONEY = {"currency": "EUR", "conversion_rate": 1.1, "discount": 10, "discount_type": "flat",
         "shipping": 5, "tax_rate": 7}


async def test_bill_carries_every_money_input_the_agreement_holds(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01", **MONEY)
    held = await template(client, hd, eid)
    r = await generate_due(client, hd, eid)
    assert r.status_code == 200, r.text
    bill = await template(client, hd, r.json()["doc_id"])
    from celerp_docs.doc_money import HEADER_MONEY_FIELDS
    assert {f: bill.get(f) for f in HEADER_MONEY_FIELDS if f in held} == {
        f: held[f] for f in HEADER_MONEY_FIELDS if f in held}
    assert bill["status"] != "draft" and bill["conversion_rate"] == 1.1


async def test_header_money_fields_cover_what_document_money_reads():
    """The shared list is the whole set of header inputs: a new one document_money reads
    and the list lacks fails here, so an agreement's bill can never drop it."""
    from celerp_docs.doc_money import HEADER_MONEY_FIELDS, document_money

    class Reads(dict):
        seen: set[str] = set()

        def get(self, key, default=None):
            self.seen.add(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.seen.add(key)
            return super().__getitem__(key)

        def __contains__(self, key):
            self.seen.add(key)
            return super().__contains__(key)

    state = Reads(discount=1, discount_type="flat", shipping=2, tax_rate=7)
    document_money(state, [{"description": "Fee", "quantity": 1, "unit_price": 10, "line_total": 10}], "USD",
                   keep_unrated_tax=True)
    assert Reads.seen and Reads.seen <= set(HEADER_MONEY_FIELDS) | {"line_items"}
    assert "conversion_rate" in HEADER_MONEY_FIELDS


async def _void_first_bill(client, hd, eid) -> str:
    first = (await generate_due(client, hd, eid)).json()
    assert (await client.post(f"/docs/{first['doc_id']}/void", headers=hd,
                              json={"reason": "Wrong amount"})).status_code == 200
    return first["ref_id"]


async def test_generate_due_on_a_voided_period_says_to_reopen(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    ref = await _void_first_bill(client, hd, eid)
    r = await generate_due(client, hd, eid)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert (detail["message_key"], detail["params"]) == ("subscriptions.err_period_void", {"ref": ref})


async def test_generate_all_due_on_a_voided_period_says_to_reopen(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    ref = await _void_first_bill(client, hd, eid)
    # The period asked for by index, as a preview taken before the void would.
    r = await client.post("/subscriptions/generate-due", headers=hd, json={"items": [
        {"entity_id": eid, "period_index": 0, "request_key": "stale"}]})
    row = r.json()["results"][0]
    assert not row["ok"] and row["error"] == t("subscriptions.err_period_void", ref=ref)


async def test_generate_all_due_counts_one_document_once(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    items = [{"entity_id": eid, "period_index": 0, "request_key": f"k{n}"} for n in range(2)]
    r = (await client.post("/subscriptions/generate-due", headers=hd, json={"items": items})).json()
    assert len(await billed_docs(session, eid)) == 1
    assert (r["billed"], r["already_billed"], r["refused"]) == (1, 1, 0)
    assert [row["already_billed"] for row in r["results"]] == [False, True]
    again = (await client.post("/subscriptions/generate-due", headers=hd, json={
        "items": [{"entity_id": eid, "period_index": 0, "request_key": "fresh"}]})).json()
    assert (again["billed"], again["already_billed"]) == (0, 1)
    # The same request sent again is its own receipt: it billed the document.
    replay = (await client.post("/subscriptions/generate-due", headers=hd, json={"items": items[:1]})).json()
    assert (replay["billed"], replay["already_billed"]) == (1, 0)


async def test_due_results_row_says_already_billed():
    from ui.routes.subscriptions import _due_results

    result = {"billed": 0, "already_billed": 1, "refused": 0, "results": [
        {"entity_id": "doc:SUB-1", "period_index": 0, "ok": True, "already_billed": True,
         "doc_id": "doc:INV-1", "ref_id": "INV-1", "service_start": "2026-03-01", "service_end": "2026-03-31"}]}
    html = str(_due_results(result, {}, "key"))
    assert sentence("subscriptions.done_already_billed", ref="INV-1", start="2026-03-01", end="2026-03-31") in html
    assert t("subscriptions.due_already", n=1) in html


async def test_bill_early_refuses_while_a_period_is_due(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-04-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    r = await bill_early(client, hd, eid, "early-behind")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert (detail["message_key"], detail["params"]) == ("subscriptions.err_early_due", {"date": "2026-03-01"})
    assert await billed_docs(session, eid) == []
    # Once nothing is due, Bill early bills the next period ahead of its date.
    assert (await generate_due(client, hd, eid)).status_code == 200
    assert (await generate_due(client, hd, eid)).status_code == 200
    ahead = await bill_early(client, hd, eid, "early-ahead")
    assert ahead.status_code == 200, ahead.text
    assert ahead.json()["period_index"] == 2
