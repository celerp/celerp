"""An amendment's credit against an invoice whose charge is in flight is held, not spent: the note
stays a draft until the charge can no longer take money, then it is issued and what is left of it
goes to the amendment's invoice. A 30.00 July charge in flight, an amendment crediting 16.45 and
billing 27.42: the customer pays 40.97 whatever the charge does, and nothing is kept unmatched."""
from __future__ import annotations

import pytest

from agreement_kit import tick
from test_collection import _attempt_outcome, cloud  # noqa: F401
from test_collection_settlement import _amend, _asked, _docs, _july, _unmatched

pytestmark = pytest.mark.asyncio


async def _held(client, session, monkeypatch, cloud, answer):
    from celerp.services import payments  # noqa: F401
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud, answer)
    r = await _amend(client, monkeypatch, hd, eid)
    assert r.status_code == 200, r.text
    new, note = r.json()["invoice_id"], r.json()["credit_note_id"]
    await tick(session, cid, "2026-07-15")
    docs = await _docs(session, eid)
    assert (docs[july]["amount_outstanding"], docs[new]["amount_outstanding"]) == (30.0, 27.42)
    assert docs[note]["status"] == "draft"
    assert cloud.sent("cancel") == [] and _asked(cloud, new) == []
    assert docs[new]["collection"]["held_by"] == docs[july]["ref_id"]
    return hd, cid, eid, july, new, note


async def _collect(session, cloud, cid, doc_id, ref, minor, day):
    [req] = [p for p in cloud.sent("attempt") if p["entity_id"] == doc_id and p["amount_minor"] == minor]
    assert await _attempt_outcome(cid, doc_id, req["attempt_number"], "succeeded") is True
    from celerp.services import payments
    assert await payments.receive_payment({
        "company_id": cid, "entity_id": doc_id, "reference": ref, "amount_minor": minor, "currency": "USD",
        "paid_at": f"{day}T13:00:00+00:00", "managed": True, "context": req["context"]})


@pytest.mark.parametrize("answer", ["submitted", "processing", "requires_action", "unknown"])
async def test_the_charge_succeeds_the_whole_credit_goes_to_the_amendment(client, session, monkeypatch, cloud, answer):
    hd, cid, eid, july, new, note = await _held(client, session, monkeypatch, cloud, answer)
    await _collect(session, cloud, cid, july, "pi_j", 3000, "2026-07-15")
    await tick(session, cid, "2026-07-16")
    docs = await _docs(session, eid)
    assert docs[note]["status"] != "draft"
    assert (_asked(cloud, july), _asked(cloud, new)) == ([3000], [1097])
    await _collect(session, cloud, cid, new, "pi_n", 1097, "2026-07-16")
    await tick(session, cid, "2026-07-17")
    docs = await _docs(session, eid)
    assert await _unmatched(session) == []
    assert (docs[july]["amount_outstanding"], docs[new]["amount_outstanding"]) == (0, 0)
    tb = (await client.get("/accounting/trial-balance", headers=hd)).json()
    assert tb.get("balanced") is True


async def test_the_charge_fails_the_credit_lowers_the_invoice_it_credits(client, session, monkeypatch, cloud):
    hd, cid, eid, july, new, note = await _held(client, session, monkeypatch, cloud, "submitted")
    assert await _attempt_outcome(cid, july, 1, "failed", retry_eligible_at="2026-07-15T00:00:00+00:00") is True
    await tick(session, cid, "2026-07-16")
    docs = await _docs(session, eid)
    assert docs[note]["status"] != "draft"
    assert (docs[july]["amount_outstanding"], docs[new]["amount_outstanding"]) == (13.55, 27.42)
    assert (_asked(cloud, july), _asked(cloud, new)) == ([3000, 1355], [2742])
    await _collect(session, cloud, cid, july, "pi_j2", 1355, "2026-07-16")
    await _collect(session, cloud, cid, new, "pi_n", 2742, "2026-07-16")
    await tick(session, cid, "2026-07-17")
    docs = await _docs(session, eid)
    assert await _unmatched(session) == []
    assert (docs[july]["amount_outstanding"], docs[new]["amount_outstanding"]) == (0, 0)
