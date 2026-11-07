# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A cancelled or void agreement (billing.ENDED) bills no period again from its page: the page
says so in place of a bill date and lists its unclaimed periods as not billing, and no action
of a person changes what its periods claim (billing.claims), whichever door it comes through."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status_census  # noqa: E402
from agreement_kit import agreement, freeze_day, generate_due, template  # noqa: E402
from ui.i18n import t  # noqa: E402

ENDED = ("cancelled", "void")
# Every action route of the module's API on one agreement, read from the code (census).
DOORS = sorted(f"{a.verb.upper()} {a.path}" for a in status_census.api_actions() if "{entity_id}" in a.path)


async def _ended(api, monkeypatch, status: str) -> tuple[str, str]:
    """An agreement from 1 Jan, monthly, on 15 Mar: January billed and its bill voided (so it can
    be reopened), February skipped, March open; then ended as ``status`` through the door a
    person uses for it. (agreement id, the voided bill's id)"""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(api, {}, start="2026-01-01")
    bill = (await generate_due(api, {}, eid)).json()["doc_id"]
    assert (await api.post(f"/docs/{bill}/void", json={"reason": "Wrong amount"})).status_code == 200
    assert (await api.post(f"/subscriptions/{eid}/periods/1/skip", json={"reason": "Holiday"})).status_code == 200
    if status == "cancelled":
        assert (await api.post(f"/subscriptions/{eid}/cancel")).status_code == 200
    else:
        r = await api.post(f"/docs/{eid}/void", json={"reason": "Entered by mistake", "idempotency_key": str(uuid.uuid4())})
        assert r.status_code == 200, r.text
    return eid, bill


def test_the_census_finds_the_doors_that_write_an_agreement():
    assert {"POST /subscriptions/{entity_id}/periods/{k}/skip", "POST /subscriptions/{entity_id}/periods/{k}/unskip",
            "POST /subscriptions/{entity_id}/periods/{k}/reopen", "PATCH /subscriptions/{entity_id}/schedule",
            "POST /subscriptions/{entity_id}/history/{doc_id}/withdraw"} <= set(DOORS)


@pytest.mark.parametrize("status", ENDED)
@pytest.mark.parametrize("door", DOORS)
async def test_no_door_changes_what_an_ended_agreements_periods_claim(owner_ui, monkeypatch, status, door):
    """Generated over every action route on one agreement (census) and each ended status: every
    period holding a bill or a skip claims what it did, and so do the bills periods let go."""
    from celerp_subscriptions.billing import claims

    eid, bill = await _ended(owner_ui.api, monkeypatch, status)
    before = await template(owner_ui.api, {}, eid)
    method, path = door.split(" ", 1)
    k = {"skip": 2, "unskip": 1, "reopen": 0, "send": 0}.get(path.rsplit("/", 1)[-1], 2)
    body = ({"frequency": "weekly"} if method == "PATCH" else
            {"reason": "Closed", "request_key": str(uuid.uuid4()), "period_index": k, "attempt": 0})
    r = await owner_ui.api.request(method, path.format(entity_id=eid, k=k, doc_id=bill), json=body)
    assert claims(await template(owner_ui.api, {}, eid)) == claims(before), (r.status_code, r.text)


def test_a_send_record_is_no_claim_and_an_agreement_without_periods_claims_none():
    """Sending a period's bill again changes no claim, so it stays open on an ended agreement;
    any other change to a billed period does, and an agreement with no periods claims nothing."""
    from celerp_subscriptions.billing import claims

    billed = {"status": "void", "periods": {"0": {"state": "billed", "doc_id": "d1"}}}
    resent = {**billed, "periods": {"0": {**billed["periods"]["0"], "send": {"state": "sent"}}}}
    assert claims(resent) == claims(billed)
    assert claims({**billed, "periods": {"0": {"state": "skipped"}}}) != claims(billed)
    assert claims({"status": "void"}) == ({}, None)


_BILL = {"state": "billed", "doc_id": "bill"}
_PLACED = {"state": "billed", "doc_id": "earlier", "history": True}
_SKIP = {"state": "skipped", "reason": "Holiday"}
# (periods before, periods after, released bills after, the answer on an ENDED agreement)
_WRITES = [
    ({"0": _BILL}, {"0": _BILL}, None, None),  # nothing claimed changes (set aside, a send)
    ({"0": _BILL, "1": _PLACED}, {"0": _BILL}, None, "ended"),  # Withdraw placement: final like a bill
    ({"0": _BILL}, {"0": _BILL, "1": _PLACED}, None, "place"),  # placing beside a bill it keeps
    ({}, {"1": _PLACED}, None, "place"),
    ({"1": _PLACED}, {"1": {**_PLACED, "doc_id": "other"}}, None, "place"),  # another document placed
    ({"0": _BILL}, {"1": _PLACED}, None, "ended"),  # placing while the bill's period lets go
    ({"0": _BILL}, {"0": _BILL, "1": _SKIP}, None, "ended"),  # skip
    ({"0": _BILL, "1": _SKIP}, {"0": _BILL}, None, "ended"),  # un-skip
    ({"0": _BILL}, {}, {"bill": 0}, "ended"),  # Reopen
    ({"0": _BILL}, {"0": _BILL, "1": _PLACED}, {"bill": 0}, "ended"),
]


@pytest.mark.parametrize("status", ["active", "paused", "draft", *ENDED])
@pytest.mark.parametrize("before,after,released,answer", _WRITES)
def test_an_ended_agreement_keeps_its_claims_and_refuses_placing_naming_set_aside(
        status, before, after, released, answer):
    """ended_refusal over every kind of claim change: an agreement still running is never refused
    here; an ended one refuses placing a history document with the key naming Set aside, and any
    other claim change (a placement's withdrawal included) naming why it ended."""
    from celerp_subscriptions.billing import ENDED as KEYS, ENDED_PLACE, ended_refusal

    got = ended_refusal({"status": status, "periods": before},
                        {"status": status, "periods": after, "released_bills": released})
    expected = None if status not in KEYS or answer is None else (ENDED_PLACE if answer == "place" else KEYS)[status]
    assert got == expected


@pytest.mark.parametrize("status", ENDED)
async def test_skip_and_hand_back_on_an_ended_agreement_are_refused_naming_why(owner_ui, monkeypatch, status):
    """Skip, Un-skip and Reopen are refused like Cancel on a void one: 409 with the status's
    reason, keyed so it reads in the reader's language after a reload too."""
    from celerp_subscriptions.billing import ENDED as reasons

    eid, _ = await _ended(owner_ui.api, monkeypatch, status)
    for action, k in (("skip", 2), ("unskip", 1), ("reopen", 0)):
        r = await owner_ui.api.post(f"/subscriptions/{eid}/periods/{k}/{action}", json={"reason": "Closed"})
        assert r.status_code == 409, (action, r.text)
        assert r.json()["detail"]["message"] == t(reasons[status], "en", ref=(await template(owner_ui.api, {}, eid))["ref_id"])
        assert r.json()["detail"]["message_key"] == reasons[status]


@pytest.mark.parametrize("status", ENDED)
async def test_an_ended_agreements_page_says_it_will_not_bill(owner_ui, monkeypatch, status):
    """The open period reads "Will not bill", never Overdue; the voided and skipped ones read as
    they are; no next bill date is shown or listed, and the note in its place says why."""
    from celerp_subscriptions.billing import _UNBILLABLE

    eid, _ = await _ended(owner_ui.api, monkeypatch, status)
    listed = (await owner_ui.api.get(f"/subscriptions/{eid}/periods")).json()
    assert listed["next_bill_date"] is None and listed["oldest_due_row"] is None
    assert {r["period_index"]: r["status"] for r in listed["rows"]} == {
        3: "not_billing", 2: "not_billing", 1: "skipped", 0: "voided"}
    soup = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
    cells = {tds[1].get_text(strip=True): tds[2].get_text(" ", strip=True)
             for tds in (tr.find_all("td") for tr in soup.select("#sub-periods tr")) if len(tds) > 2}
    assert cells["2026-03-01"] == t("subscriptions.period_not_billing", "en"), cells
    assert t("subscriptions.period_overdue", "en") not in cells.values(), cells
    assert soup.select_one("#sub-bill-note").get_text(strip=True) == t(_UNBILLABLE[status], "en")
