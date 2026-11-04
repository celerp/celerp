# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A period is billed by its document only while that document is issued, and a document an
agreement period has let go never bills again: unvoid, finalize and send after Reopen are
refused, naming the bill the period has now. A draft or void document never reads as billed,
whether the answer is fresh or a replay."""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import agreement, billed_docs, freeze_day, generate_due, register, template
from ui.i18n import t

pytestmark = pytest.mark.asyncio


def _key() -> dict:
    return {"idempotency_key": str(uuid.uuid4())}


async def _live(session, eid: str, k: int = 0) -> list[str]:
    """The period's issued documents: the ones its customer owes on."""
    return [d["ref_id"] for d in await billed_docs(session, eid)
            if d.get("period_index") == k and d.get("status") not in ("draft", "void")]


async def _row(client, hd, eid: str, k: int = 0) -> dict:
    rows = (await client.get(f"/subscriptions/{eid}/periods", headers=hd)).json()["rows"]
    return next(r for r in rows if r["period_index"] == k)


def _refusal(r) -> tuple[str, dict]:
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    return detail["message_key"], detail["params"]


async def _void(client, hd, doc_id: str) -> None:
    assert (await client.post(f"/docs/{doc_id}/void", headers=hd, json={"reason": "x", **_key()})).status_code == 200


async def _revert(client, hd, doc_id: str) -> None:
    assert (await client.post(f"/docs/{doc_id}/revert-to-draft", headers=hd, json=_key())).status_code == 200


async def _reopen(client, hd, eid: str, k: int = 0):
    return await client.post(f"/subscriptions/{eid}/periods/{k}/reopen", headers=hd)


async def _billed_then_let_go(client, hd, eid: str, let_go) -> tuple[dict, dict]:
    """Bill period 0, take its document out of billing (``let_go``), Reopen, bill it again."""
    first = (await generate_due(client, hd, eid)).json()
    await let_go(client, hd, first["doc_id"])
    assert (await _reopen(client, hd, eid)).status_code == 200
    second = (await generate_due(client, hd, eid)).json()
    assert second["ref_id"] != first["ref_id"], second
    return first, second


# What brings a let-go document back into billing, from the state each leaves it in.
_RETURNS = {
    "unvoid": (_void, lambda c, hd, d: c.post(f"/docs/{d}/unvoid", headers=hd, json=_key())),
    "finalize": (_revert, lambda c, hd, d: c.post(f"/docs/{d}/finalize", headers=hd)),
    "send": (_revert, lambda c, hd, d: c.post(f"/docs/{d}/send", headers=hd, json={**_key(), "sent_via": "manual"})),
}


@pytest.mark.parametrize("route", sorted(_RETURNS))
async def test_a_let_go_document_never_bills_a_period_billed_again(client, session, monkeypatch, route):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    let_go, bring_back = _RETURNS[route]
    first, second = await _billed_then_let_go(client, hd, eid, let_go)
    key, params = _refusal(await bring_back(client, hd, first["doc_id"]))
    assert key == "subscriptions.err_bill_rebilled"
    assert (params["ref"], params["newer"], params["start"]) == (first["ref_id"], second["ref_id"], "2026-03-01")
    assert await _live(session, eid) == [second["ref_id"]]


@pytest.mark.parametrize("route", sorted(_RETURNS))
async def test_a_let_go_document_never_bills_a_reopened_period(client, session, monkeypatch, route):
    """Reopen without a new bill yet: the document is still refused, and the period stays open
    for Generate due, which then bills it once."""
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    let_go, bring_back = _RETURNS[route]
    first = (await generate_due(client, hd, eid)).json()
    await let_go(client, hd, first["doc_id"])
    assert (await _reopen(client, hd, eid)).status_code == 200
    key, params = _refusal(await bring_back(client, hd, first["doc_id"]))
    assert (key, params["ref"]) == ("subscriptions.err_bill_reopened", first["ref_id"])
    assert (await _row(client, hd, eid))["status"] == "due"
    second = (await generate_due(client, hd, eid)).json()
    assert await _live(session, eid) == [second["ref_id"]]


async def test_a_document_its_period_still_holds_comes_back(client, session, monkeypatch):
    """Void then unvoid, or revert then finalize, without Reopen: the period still names the
    document, so it bills again and the period reads billed."""
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    first = (await generate_due(client, hd, eid)).json()
    await _void(client, hd, first["doc_id"])
    assert (await client.post(f"/docs/{first['doc_id']}/unvoid", headers=hd, json=_key())).status_code == 200
    await _revert(client, hd, first["doc_id"])
    assert (await client.post(f"/docs/{first['doc_id']}/finalize", headers=hd)).status_code == 200
    assert (await _row(client, hd, eid))["status"] == "billed"
    assert await _live(session, eid) == [first["ref_id"]]


async def test_a_bill_back_in_draft_reads_draft_and_never_answers_billed(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    first = (await generate_due(client, hd, eid)).json()
    await _revert(client, hd, first["doc_id"])
    assert (await _row(client, hd, eid))["status"] == "draft"
    assert _refusal(await generate_due(client, hd, eid)) == ("subscriptions.err_period_draft", {"ref": first["ref_id"]})
    r = await client.post("/subscriptions/generate-due", headers=hd, json={"items": [
        {"entity_id": eid, "period_index": 0, "request_key": "stale"}]})
    row = r.json()["results"][0]
    assert not row["ok"] and row["error"] == t("subscriptions.err_period_draft", ref=first["ref_id"])
    # Nothing is billed, so the period can be opened again and billed with a new document.
    assert (await _reopen(client, hd, eid)).status_code == 200
    second = (await generate_due(client, hd, eid)).json()
    assert await _live(session, eid) == [second["ref_id"]]


async def test_a_replayed_request_never_answers_a_document_that_stopped_billing(client, session, monkeypatch):
    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    first = (await generate_due(client, hd, eid, key="k-1")).json()
    await _revert(client, hd, first["doc_id"])
    assert _refusal(await generate_due(client, hd, eid, key="k-1")) == (
        "subscriptions.err_period_draft", {"ref": first["ref_id"]})
    await _void(client, hd, first["doc_id"])
    assert _refusal(await generate_due(client, hd, eid, key="k-1")) == (
        "subscriptions.err_period_void", {"ref": first["ref_id"]})
    assert (await template(client, hd, first["doc_id"]))["status"] == "void"
