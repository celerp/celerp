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

Every refusal a status guard answers (guard_agreement_status, write_agreement, guard_bill_status) is
walked the same way: over every agreement status, with the module on or off, a door it names opens.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "default_modules" / "celerp-subscriptions" / "tests"))
import status_census  # noqa: E402
from agreement_kit import agreement, billed_docs, freeze_day, generate_due, register  # noqa: E402
from celerp.models.projections import Projection  # noqa: E402
from celerp_subscriptions.billing import AGREEMENT_STATUSES  # noqa: E402
from test_subscriptions_upgrade_v280 import _generate, _on, _page, _state, _upgrade, _url  # noqa: E402
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
    # Return of a set-aside is open on every status: it claims nothing. On an ended agreement a
    # placement is final like a bill: Withdraw placement is refused naming why it ended.
    for doc in (placed, aside):
        r = await door("withdraw", doc)
        if status in ENDED and doc == placed:
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["message_key"] == ENDED[status]
            assert r.json()["detail"]["message"] == t(ENDED[status], "en", ref=ref)
        else:
            assert r.status_code == 200, (status, doc, r.text)
    waiting = {aside} if status in ENDED else {placed, aside}
    assert {r["doc_id"] for r in await _review(client, old)} == waiting
    assert await _in_review_list(client, old)
    # Placing claims a period: refused on an ended agreement, naming the way out.
    r = await door("confirm", aside if status in ENDED else placed, period_index=k)
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
    # The page asks for nothing more: no document offers Place or Set aside.
    page = await _page(monkeypatch, old, NAME)
    assert "/set-aside\"" not in page and "/confirm\"" not in page, status


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


async def _two_in_one_period(session, old):
    """A 2.8.0 user issued two documents in one period: the second gets the first one's issue
    date, so both wait for review with a candidate period in common."""
    from sqlalchemy.orm.attributes import flag_modified

    from celerp.models.projections import Projection
    tpl = await session.get(Projection, {"company_id": old["company_id"], "entity_id": old["runs"][NAME]})
    docs = [await session.get(Projection, {"company_id": old["company_id"], "entity_id": d})
            for d in tpl.state["generated_doc_ids"]]
    docs[-1].state = {**docs[-1].state, "issue_date": docs[-2].state["issue_date"]}
    flag_modified(docs[-1], "state")
    await session.commit()


# What each claim door answers on a cancelled or void agreement, per kind of claim it acts on:
# "ended" refuses naming why it ended, "place" refuses naming Set aside, None is open (it changes
# no claim). A door the census finds that is not named here fails the guard.
WAY_OUT = {
    "skip": {"open": "ended", "billed": "ended", "void_bill": "ended", "placed": "ended"},
    "unskip": {"skipped": "ended"},
    "reopen": {"billed": "ended", "void_bill": "ended"},
    "send": {"billed": None},
    "confirm": {"placed": "place", "open": "place"},
    "set-aside": {"in_review": None},
    "withdraw": {"placed": "ended", "set_aside": None},
}


def test_every_claim_door_has_its_ended_answer():
    assert {_door(a.path) for a in status_census.claim_doors()} == set(WAY_OUT)


@pytest.mark.parametrize("status", ["cancelled", "void"])
async def test_an_ended_agreement_answers_its_rule_first_and_the_door_it_names_can_be_undone(
        client, session, monkeypatch, status):
    """Over every claim door x every kind of claim it acts on: the ended rule answers before any
    claimed-period refusal (a document placed in a period by another reads Set aside, never
    Withdraw placement or Reopen), and Set aside, the door it names, succeeds and its Return
    succeeds too, with a later period billed (which, while the agreement ran, kept the placement)."""
    from celerp_subscriptions.billing import BILLED, ENDED, ENDED_PLACE

    old = await _upgrade(session, monkeypatch, "2026-11-04", before_start=_two_in_one_period)
    hd, eid = old["headers"], old["runs"][NAME]
    (a, ca), (b, cb) = ((r["doc_id"], [c["period_index"] for c in r["candidates"]]) for r in await _review(client, old))
    k = min(set(ca) & set(cb))

    def post(path, **body):
        return client.post(_url(old, NAME, path), headers=hd, json=body or None)

    # The claims, made while the agreement still runs.
    assert (await post(f"/history/{a}/confirm", period_index=k)).status_code == 200
    for day in ("2026-11-04", "2026-12-20"):  # two bills of this release, after the documents placed
        _on(monkeypatch, day)
        assert (await _generate(client, old, NAME)).status_code == 200
    periods = (await _state(session, old, NAME))["periods"]
    bills = sorted(int(i) for i, e in periods.items() if e.get("state") == BILLED and not e.get("history"))
    assert len(bills) >= 2 and bills[-1] > max(cb), (bills, cb)
    void_bill, billed = bills[0], bills[-1]
    r = await client.post(f"/docs/{periods[str(void_bill)]['doc_id']}/void", headers=hd,
                          json={"reason": "Entered by mistake", "idempotency_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    open_k = max(int(i) for i in periods) + 1
    assert (await post(f"/periods/{open_k + 1}/skip", reason="Closed")).status_code == 200
    # While it runs, a later period is billed after the placement, so the placement stays.
    r = await post(f"/history/{a}/withdraw")
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "subscriptions.err_history_later_billed", r.text
    await _to(client, old, status)
    ref = (await _state(session, old, NAME))["ref_id"]
    claims = {"open": open_k, "billed": billed, "void_bill": void_bill, "placed": k, "skipped": open_k + 1}
    answers = {"ended": ENDED[status], "place": ENDED_PLACE[status]}

    for door, kinds in WAY_OUT.items():
        for kind, answer in kinds.items():
            if door in ("set-aside", "withdraw") and answer is None:
                continue  # the way out, walked below
            if door == "confirm":
                r = await post(f"/history/{b}/confirm", period_index=claims[kind])
            elif door == "withdraw":
                r = await post(f"/history/{a}/withdraw")
            elif door == "send":
                r = await post(f"/periods/{claims[kind]}/send", attempt=0)
            else:
                r = await post(f"/periods/{claims[kind]}/{door}", reason="Closed")
            got = r.json().get("detail") if r.status_code >= 400 else None
            key = got.get("message_key") if isinstance(got, dict) else None
            if answer is None:
                assert key not in (*ENDED.values(), *ENDED_PLACE.values()), (door, kind, r.text)
            else:
                assert r.status_code == 409 and key == answers[answer], (door, kind, r.text)
                assert got["message"] == t(answers[answer], "en", ref=ref)
    assert t("subscriptions.history_set_aside", "en") in t(ENDED_PLACE[status], "en", ref=ref)
    # The door named: Set aside finishes review, Return undoes it although a later period is
    # billed, and Set aside finishes it again.
    for path in (f"/history/{b}/set-aside", f"/history/{b}/withdraw", f"/history/{b}/set-aside"):
        r = await post(path)
        assert r.status_code == 200, (path, r.text)
    assert not await _review(client, old) and not await _in_review_list(client, old)
    assert (await _state(session, old, NAME))["periods"][str(k)]["doc_id"] == a


# Every refusal that names a door (status_census.named_door_refusals), the door it names, and the
# test that walks it: the door accepts in the state the refusal answered from, and the page where
# the refusal shows offers it. A refusal the census finds that is not named here fails the guard.
HERE = "test_every_refusal_on_a_running_agreement_names_a_door_that_accepts_and_is_on_its_page"
VOID_HERE = "test_a_void_agreement_offers_unvoid_where_its_refusals_name_it"
NAMED_DOORS = {
    "subscriptions.err_period_placed": ("withdraw", HERE),
    "subscriptions.err_period_placed_later_billed": ("void, reopen, withdraw", HERE),
    "subscriptions.err_history_later_billed": ("void, reopen", HERE),
    "subscriptions.err_period_billed": ("void", HERE),
    "subscriptions.err_period_void": ("reopen", HERE),
    "subscriptions.err_ended_void": ("unvoid", VOID_HERE),
    "subscriptions.err_status_void": ("unvoid", VOID_HERE),
    "subscriptions.err_bill_void": ("unvoid", VOID_HERE),
    "subscriptions.err_ended_place_void": ("set-aside, unvoid", VOID_HERE),
    "subscriptions.err_ended_place_cancelled": (
        "set-aside", "test_an_ended_agreement_answers_its_rule_first_and_the_door_it_names_can_be_undone"),
    "subscriptions.err_period_draft": ("reopen", "test_period_billed_once"),
    "subscriptions.err_early_due": ("generate-due", "test_billing_answers"),
    "subscriptions.err_agreement_status_lifecycle": ("status actions", "test_consent_doors"),
    "subscriptions.err_bill_refused": ("cancel, new subscription", "test_bill_refusal_doors"),
    "subscriptions.err_bill_refused_draft": ("+ Add item, activate", "test_bill_refusal_doors"),
}


def test_every_refusal_naming_a_door_is_walked():
    assert {key for key, _ in status_census.named_door_refusals()} == set(NAMED_DOORS)


def _refusal(r) -> tuple[str | None, str | None]:
    got = r.json().get("detail") if r.status_code == 409 else None
    return (got.get("message_key"), got.get("message")) if isinstance(got, dict) else (None, r.text)


async def test_every_refusal_on_a_running_agreement_names_a_door_that_accepts_and_is_on_its_page(
        client, session, monkeypatch):
    """A placement with later periods billed: Skip and Reopen on its period name the way on (void
    the later bills, Reopen their periods, then Withdraw placement), as does Withdraw placement.
    Each door named accepts, one after the other, and the agreement's page offers each (the bill's
    link, Reopen on its row, Withdraw placement); Withdraw placement is undone by placing it again."""
    from celerp_subscriptions.billing import BILLED

    old = await _upgrade(session, monkeypatch, "2026-11-04", before_start=_two_in_one_period)
    hd, eid = old["headers"], old["runs"][NAME]
    (a, ca), (_b, cb) = ((r["doc_id"], [c["period_index"] for c in r["candidates"]]) for r in await _review(client, old))
    k = min(set(ca) & set(cb))

    def post(path, **body):
        return client.post(_url(old, NAME, path), headers=hd, json=body or None)

    async def ref(doc_id):
        session.expire_all()
        return (await session.get(Projection, {"company_id": old["company_id"], "entity_id": doc_id})).state["ref_id"]

    assert (await post(f"/history/{a}/confirm", period_index=k)).status_code == 200
    for day in ("2026-11-04", "2026-12-20"):
        _on(monkeypatch, day)
        assert (await _generate(client, old, NAME)).status_code == 200
    periods = (await _state(session, old, NAME))["periods"]
    # The placement's period and the two bills generated after it, by the periods they bill.
    assert k == -6 and {i: (e["state"], bool(e.get("history"))) for i, e in periods.items()} == {
        "-6": (BILLED, True), "0": (BILLED, False), "1": (BILLED, False)}
    later = [(p, periods[str(p)]["doc_id"]) for p in (0, 1)]
    placed, refs = await ref(a), ", ".join([await ref(d) for _, d in later])
    for door in ("skip", "reopen"):
        assert _refusal(await post(f"/periods/{k}/{door}", reason="Closed")) == (
            "subscriptions.err_period_placed_later_billed",
            t("subscriptions.err_period_placed_later_billed", "en", ref=placed, later=refs)), door
    assert _refusal(await post(f"/history/{a}/withdraw")) == (
        "subscriptions.err_history_later_billed", t("subscriptions.err_history_later_billed", "en", later=refs))
    # The doors named, newest bill first: Skip on it names Void (the bill's link is on the page and
    # Void accepts), then Reopen (on its row, and it accepts).
    for p, doc in reversed(later):
        assert _refusal(await post(f"/periods/{p}/skip", reason="Closed"))[0] == "subscriptions.err_period_billed"
        assert f'href="/docs/{doc}"' in await _page(monkeypatch, old, NAME)
        r = await client.post(f"/docs/{doc}/void", headers=hd,
                              json={"reason": "Entered by mistake", "idempotency_key": str(uuid.uuid4())})
        assert r.status_code == 200, r.text
        assert _refusal(await post(f"/periods/{p}/skip", reason="Closed"))[0] == "subscriptions.err_period_void"
        assert f'hx-post="/subscriptions/{eid}/periods/{p}/reopen"' in await _page(monkeypatch, old, NAME)
        assert (await post(f"/periods/{p}/reopen")).status_code == 200
    assert _refusal(await post(f"/periods/{k}/skip", reason="Closed")) == (
        "subscriptions.err_period_placed", t("subscriptions.err_period_placed", "en", ref=placed))
    assert f'hx-post="/subscriptions/{eid}/history/{a}/withdraw"' in await _page(monkeypatch, old, NAME)
    assert (await post(f"/history/{a}/withdraw")).status_code == 200
    assert (await post(f"/history/{a}/confirm", period_index=k)).status_code == 200


async def test_a_placement_is_held_only_by_the_bills_after_its_earliest_candidate(monkeypatch):
    """The bills a placement waits on are the bills (not other placements) of the periods after
    the earliest period it could stand for, oldest first: one before it or at it, or another
    placement, never holds it."""
    from fastapi import HTTPException

    from celerp_subscriptions import billing, routes
    from celerp_subscriptions.billing import BILLED

    async def doc_ref(_session, _company_id, doc_id):
        return doc_id.upper()

    monkeypatch.setattr(billing, "doc_ref", doc_ref)
    periods = {"-9": {"state": BILLED, "doc_id": "before"},
               "-8": {"state": BILLED, "doc_id": "at"},
               "-6": {"state": BILLED, "doc_id": "placed", "history": True, "candidates": [-8, -6]},
               "-7": {"state": BILLED, "doc_id": "between"},
               "-5": {"state": BILLED, "doc_id": "other", "history": True, "candidates": [-5]},
               "1": {"state": BILLED, "doc_id": "after"}}
    assert await routes._later_bills(None, None, periods, -8) == "BETWEEN, AFTER"
    with pytest.raises(HTTPException) as refused:
        await routes._refuse_claimed(None, None, periods, -6)
    assert refused.value.detail == t("subscriptions.err_period_placed_later_billed",
                                     ref="PLACED", later="BETWEEN, AFTER")


async def test_a_void_agreement_offers_unvoid_where_its_refusals_name_it(client, session, monkeypatch):
    """A void agreement's period and status doors answer "Unvoid it": its page links to its
    document's page, which offers Unvoid, and Unvoid accepts."""
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    hd, eid = old["headers"], old["runs"][NAME]
    await _to(client, old, "void")
    assert _refusal(await client.post(_url(old, NAME, "/cancel"), headers=hd))[0] == "subscriptions.err_status_void"
    page = await _page(monkeypatch, old, NAME)
    assert f'<a href="/docs/{eid}"' in page and t("doc.unvoid", "en") in page
    assert t("btn.confirm_unvoid", "en") in await _page(monkeypatch, old, NAME, f"/docs/{eid}")
    r = await client.post(f"/docs/{eid}/unvoid", headers=hd, json={"idempotency_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text


async def _bill_void_reopen(client, hd, eid: str) -> dict:
    """Period 0 billed, its bill voided in Documents and the period reopened, as a person does."""
    bill = (await generate_due(client, hd, eid)).json()
    r = await client.post(f"/docs/{bill['doc_id']}/void", headers=hd,
                          json={"reason": "Entered by mistake", "idempotency_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/subscriptions/{eid}/periods/0/reopen", headers=hd)).status_code == 200
    return bill


async def _agreement_to(client, hd, eid: str, status: str) -> None:
    if status == "active":
        return
    if status == "void":
        r = await client.post(f"/docs/{eid}/void", headers=hd,
                              json={"reason": "Entered by mistake", "idempotency_key": str(uuid.uuid4())})
    else:
        r = await client.post(f"/subscriptions/{eid}/" + {"paused": "pause", "cancelled": "cancel"}[status], headers=hd)
    assert r.status_code == 200, (status, r.text)


# Every refusal the agreement and bill status guards and write_agreement raise (status_refusals),
# with the doors it names. A row with doors is walked by
# test_every_status_refusal_names_a_door_that_opens_in_the_state_it_answers: in whatever status the
# agreement holds, with the module on or off, one of the doors named accepts. A row naming a test
# is walked there; one naming no door is asserted to name none.
STATUS_REFUSALS = {
    "subscriptions.err_agreement_status_elsewhere": ("activate", "pause", "resume", "cancel"),
    "subscriptions.err_agreement_activate": ("activate", "void"),
    "subscriptions.err_agreement_status_lifecycle": ("activate", "pause", "resume", "cancel", "void", "unvoid", "revert"),
    "subscriptions.err_status_void": ("unvoid",),
    "subscriptions.err_ended_void": ("unvoid",),
    "subscriptions.err_ended_cancelled": ("new subscription",),
    "subscriptions.err_ended_place_void": "test_a_void_agreement_offers_unvoid_where_its_refusals_name_it",
    "subscriptions.err_ended_place_cancelled": (
        "test_an_ended_agreement_answers_its_rule_first_and_the_door_it_names_can_be_undone"),
    "subscriptions.err_bill_rebilled": (),
}
# Helpers the guards call that refuse on their own, each refusing whatever the status, and the test
# that walks them.
RAISING_HELPERS = {
    "refuse_unbuildable_terms": "test_schedule_terms_doors",
    "require_mode_permissions": "test_consent_doors",
    "locked_agreement": "test_billing_answers",
    "write_agreement": "test_every_status_refusal_names_a_door_that_opens_in_the_state_it_answers",
}
GUARDS = ("guard_agreement_status", "write_agreement", "guard_bill_status")


def status_refusals() -> tuple[set[str], set[str]]:
    """(keys, helpers): every catalog key a raise in GUARDS answers with, read off the code (the
    raise itself, the functions its value comes from and the constants they read), and every
    module function a guard calls outside a raise that refuses on its own."""
    import ast
    import inspect
    import re
    import textwrap

    from celerp_subscriptions import billing

    def fn(name):
        target = getattr(billing, name, None)
        return target if inspect.isfunction(target) and target.__module__ == billing.__name__ else None

    def tree(f):
        return ast.parse(textwrap.dedent(inspect.getsource(f)))

    def keys_of(node, seen) -> set[str]:
        found = set(re.findall(r"subscriptions\.err_\w+", ast.unparse(node)))
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id not in seen:
                seen.add(n.id)
                if (f := fn(n.id)) is not None:
                    found |= keys_of(tree(f), seen)
                elif isinstance(value := getattr(billing, n.id, None), (dict, str)):
                    found |= set(re.findall(r"subscriptions\.err_\w+", repr(value)))
        return found

    keys, helpers = set(), set()
    for name in GUARDS:
        body = tree(fn(name))
        assigned = {t.id: n.value for n in ast.walk(body) if isinstance(n, (ast.Assign, ast.NamedExpr))
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        raises = [n for n in ast.walk(body) if isinstance(n, ast.Raise) and n.exc is not None]
        in_raise = {id(m) for r in raises for m in ast.walk(r)}
        for r in raises:
            keys |= keys_of(r.exc, set())
            keys |= {k for n in ast.walk(r.exc) if isinstance(n, ast.Name) and n.id in assigned
                     for k in keys_of(assigned[n.id], set())}
        helpers |= {n.func.id for n in ast.walk(body) if isinstance(n, ast.Call) and id(n) not in in_raise
                    and isinstance(n.func, ast.Name) and (f := fn(n.func.id)) is not None
                    and any(isinstance(m, ast.Raise) for m in ast.walk(tree(f)))}
    return keys, helpers


def test_every_status_refusal_has_a_row():
    """A refusal added to a status guard, or a helper that refuses, fails here until it has a row."""
    import json

    keys, helpers = status_refusals()
    assert keys == set(STATUS_REFUSALS)
    assert helpers == set(RAISING_HELPERS)
    en = json.loads((Path(__file__).resolve().parents[1] / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert not [k for k, doors in STATUS_REFUSALS.items() if not doors and status_census.DOOR_WORDS.search(en[k])]
    assert set(WAY_OUT_STATUSES) == AGREEMENT_STATUSES - {"draft"}


async def _status_door(client, hd, eid: str, door: str):
    """A person's status door (or Skip, which changes what a period claims) on agreement ``eid``."""
    key = {"idempotency_key": str(uuid.uuid4())}
    if door == "new subscription":
        return await client.post(f"/subscriptions/{await agreement(client, hd, start='2026-01-01', activate=False)}/activate",
                                 headers=hd)
    if door == "skip":
        return await client.post(f"/subscriptions/{eid}/periods/1/skip", headers=hd, json={"reason": "Closed"})
    if door in ("activate", "pause", "resume", "cancel"):
        return await client.post(f"/subscriptions/{eid}/{door}", headers=hd)
    body = {"void": {"reason": "Entered by mistake", **key}, "unvoid": key}.get(door, {})
    return await client.post(f"/docs/{eid}/{ {'revert': 'revert-to-draft'}.get(door, door)}", headers=hd, json=body)


STATUS_DOORS = ("activate", "pause", "resume", "cancel", "void", "unvoid", "revert", "finalize", "skip")
# The status refusals the walk meets, by whether the module is on.
MET = {
    True: {"subscriptions.err_agreement_status_elsewhere", "subscriptions.err_agreement_activate",
           "subscriptions.err_agreement_status_lifecycle", "subscriptions.err_status_void",
           "subscriptions.err_ended_void", "subscriptions.err_ended_cancelled"},
    False: {"subscriptions.err_agreement_activate", "subscriptions.err_agreement_status_lifecycle"},
}


@pytest.mark.parametrize("module_on", [True, False])
async def test_every_status_refusal_names_a_door_that_opens_in_the_state_it_answers(
        client, session, monkeypatch, module_on):
    """An agreement billing on its own is brought to each status as a person does (void while the
    module is off, as Documents allows), then the module is left on or off and each status door is
    pressed. Each status refusal it answers names a door, and one of them accepts in that state."""
    from agreement_kit import company_id, schedule, tick
    from test_module_off_census import MODULE, _turn_off

    freeze_day(monkeypatch, "2026-01-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    made = {}
    for status in sorted(AGREEMENT_STATUSES):
        for door in STATUS_DOORS:
            eid = await agreement(client, hd, start="2026-01-01", activate=status != "draft")
            r = await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=6)
            assert r.status_code == 200, (status, r.text)
            if status in ("paused", "cancelled"):
                assert (await _status_door(client, hd, eid, {"paused": "pause", "cancelled": "cancel"}[status])).status_code == 200
            made[status, door] = eid
    await tick(session, cid, "2026-01-01")
    await _turn_off(client, hd, monkeypatch)
    for door in STATUS_DOORS:
        assert (await _status_door(client, hd, made["void", door], "void")).status_code == 200
    if module_on:
        assert (await client.post(f"/companies/me/modules/{MODULE}/enable", headers=hd)).status_code == 200
        monkeypatch.undo()
        freeze_day(monkeypatch, "2026-01-01")
    met, answers = set(), {}
    for (status, door), eid in made.items():
        assert (await client.get(f"/docs/{eid}", headers=hd)).json()["status"] == status
        r = await _status_door(client, hd, eid, door)
        key = (r.json().get("detail") or {}).get("message_key") if r.status_code == 409 else None
        answers[status, door] = key or r.status_code
        if key not in STATUS_REFUSALS:
            continue
        met.add(key)
        named = STATUS_REFUSALS[key]
        assert isinstance(named, tuple) and named, (status, door, key)
        for named_door in named:
            if (await _status_door(client, hd, eid, named_door)).status_code == 200:
                break
        else:
            raise AssertionError(("no door named opens", status, door, key, module_on))
    assert met == MET[module_on], (met, answers)


WAY_OUT_STATUSES = ("active", "paused", "cancelled", "void")


@pytest.mark.parametrize("module_on", [True, False])
@pytest.mark.parametrize("status", WAY_OUT_STATUSES)
async def test_a_let_go_document_comes_back_in_every_agreement_status(client, session, monkeypatch, status, module_on):
    """Bill, void the bill, Reopen, then the agreement is paused, cancelled or voided, and the module
    is left on or turned off: Unvoid on the bill undoes the Reopen, so the period reads billed on it
    again. Once a newer bill bills the period instead, Unvoid of the older one answers
    err_bill_rebilled (it names no door: test_every_status_refusal_has_a_row) in every status."""
    from test_module_off_census import _turn_off

    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    bill = await _bill_void_reopen(client, hd, eid)
    await _agreement_to(client, hd, eid, status)
    other = await agreement(client, hd, start="2026-03-01")
    older = await _bill_void_reopen(client, hd, other)
    newer = (await generate_due(client, hd, other)).json()
    await _agreement_to(client, hd, other, status)
    if not module_on:
        await _turn_off(client, hd, monkeypatch)
    r = await client.post(f"/docs/{bill['doc_id']}/unvoid", headers=hd, json={"idempotency_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/docs/{eid}", headers=hd)).json()
    assert (state["status"], state["periods"]["0"]["doc_id"], state["released_bills"]) == (status, bill["doc_id"], {})

    r = await client.post(f"/docs/{older['doc_id']}/unvoid", headers=hd, json={"idempotency_key": str(uuid.uuid4())})
    got = r.json()["detail"] if r.status_code == 409 else {}
    assert (got.get("message_key"), (got.get("params") or {}).get("newer")) == (
        "subscriptions.err_bill_rebilled", newer["ref_id"]), r.text
    assert [d["ref_id"] for d in await billed_docs(session, other) if d.get("status") not in ("draft", "void")] == [
        newer["ref_id"]]
