# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""History review is always finishable, whatever status the agreement holds.

The 2.8.0 demo company (test_subscriptions_upgrade_v280) moves ``paused_resumed`` to this
release with two documents waiting for history review. Generated over every agreement status
(billing.AGREEMENT_STATUSES, each reached through the door a person uses for it) and every
history review door (status_census.review_doors) in every document state that door acts on:
each door answers as the rule says (on a cancelled or void agreement nothing new is placed in a
period, and the refusal names Set aside), and from every state some door empties the review, so
the agreement leaves the review list and its page stops asking for review.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "default_modules" / "celerp-subscriptions" / "tests"))
import status_census  # noqa: E402
from celerp_subscriptions.billing import AGREEMENT_STATUSES  # noqa: E402
from test_subscriptions_upgrade_v280 import _page, _state, _upgrade, _url  # noqa: E402
from ui.i18n import t  # noqa: E402


NAME = "paused_resumed"
# The document state each review door acts on (a door the census finds that is not named here
# fails test_every_review_door_is_named).
ACTS_ON = {"confirm": "in_review", "set-aside": "in_review", "withdraw": ("placed", "set_aside")}


def _door(path: str) -> str:
    return path.rsplit("/", 1)[-1]


async def _to(client, old, status: str) -> None:
    """Bring the agreement to ``status`` through the door a person uses for it."""
    eid, hd = old["runs"][NAME], old["headers"]
    if status == "active":
        return
    if status == "void":
        r = await client.post(f"/docs/{eid}/void", headers=hd,
                              json={"reason": "Entered by mistake", "idempotency_key": str(uuid.uuid4())})
    elif status == "draft":
        r = await client.post(f"/docs/{eid}/revert-to-draft", headers=hd, json={})
    else:
        r = await client.post(_url(old, NAME, {"paused": "/pause", "cancelled": "/cancel"}[status]), headers=hd, json={})
    assert r.status_code == 200, (status, r.text)


async def _review(client, old) -> list[dict]:
    return (await client.get(_url(old, NAME, "/periods"), headers=old["headers"])).json().get("review") or []


async def _in_review_list(client, old) -> bool:
    r = await client.get("/subscriptions", headers=old["headers"], params={"review": "1"})
    assert r.status_code == 200, r.text
    body = r.json()
    items = body.get("items", body) if isinstance(body, dict) else body
    return any(old["runs"][NAME] in (i.get("entity_id"), i.get("id")) for i in items)


def test_every_review_door_is_named():
    assert {_door(a.path) for a in status_census.review_doors()} == set(ACTS_ON)


# The statuses an agreement in history review can hold: draft is left out only if its door
# refuses (asserted in test_a_draft_cannot_be_reached_from_history_review).
STATUSES = sorted(AGREEMENT_STATUSES - {"draft"})


@pytest.mark.parametrize("status", STATUSES)
async def test_every_review_door_answers_by_the_rule_and_review_always_finishes(client, session, monkeypatch, status):
    from celerp_subscriptions.billing import ENDED

    old = await _upgrade(session, monkeypatch, "2026-11-04")
    hd, eid = old["headers"], old["runs"][NAME]
    review = await _review(client, old)
    assert len(review) == 2 and await _in_review_list(client, old), review
    (placed, k), (aside, _) = ((r["doc_id"], r["candidates"][0]["period_index"]) for r in review)

    def door(name, doc, **body):
        return client.post(f"/subscriptions/{eid}/history/{doc}/{name}", headers=hd, json=body or None)

    # The documents' states the doors act on, made while the agreement still runs.
    assert (await door("confirm", placed, period_index=k)).status_code == 200
    assert (await door("set-aside", aside)).status_code == 200
    assert not await _review(client, old) and not await _in_review_list(client, old)
    await _to(client, old, status)
    ref = (await _state(session, old, NAME))["ref_id"]
    # Withdraw from both states is open on every status: it moves no money and claims nothing.
    for doc in (placed, aside):
        r = await door("withdraw", doc)
        assert r.status_code == 200, (status, doc, r.text)
    assert {r["doc_id"] for r in await _review(client, old)} == {placed, aside}
    assert await _in_review_list(client, old)
    # Placing claims a period: refused on an ended agreement, naming the way out.
    r = await door("confirm", placed, period_index=k)
    if status in ENDED:
        assert r.status_code == 409, r.text
        key = f"subscriptions.err_ended_place_{status}"
        assert r.json()["detail"]["message_key"] == key
        assert r.json()["detail"]["message"] == t(key, "en", ref=ref)
        assert t("subscriptions.history_set_aside", "en") in r.json()["detail"]["message"]
    else:
        assert r.status_code == 200, r.text
    # The way out: Set aside ends review for every document still waiting, on every status.
    for row in await _review(client, old):
        r = await door("set-aside", row["doc_id"])
        assert r.status_code == 200, (status, r.text)
    assert not await _review(client, old)
    assert not await _in_review_list(client, old)
    assert (await _state(session, old, NAME)).get("history_review") in (None, [])
    assert t("subscriptions.history_set_aside", "en") not in await _page(monkeypatch, old, NAME)


async def test_an_agreement_in_review_can_be_cancelled_and_still_finishes(client, session, monkeypatch):
    """Cancel during review stays allowed; both documents are set aside after it."""
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    hd = old["headers"]
    await _to(client, old, "cancelled")
    for row in await _review(client, old):
        r = await client.post(_url(old, NAME, f"/history/{row['doc_id']}/set-aside"), headers=hd)
        assert r.status_code == 200, r.text
    assert not await _review(client, old) and not await _in_review_list(client, old)


async def test_a_draft_cannot_be_reached_from_history_review(client, session, monkeypatch):
    """The one status left out of the generated guard: no door takes a moved agreement back to
    draft, so no draft holds history review."""
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    r = await client.post(f"/docs/{old['runs'][NAME]}/revert-to-draft", headers=old["headers"], json={})
    assert r.status_code >= 400, r.text
    assert (await _state(session, old, NAME))["status"] == "active"
