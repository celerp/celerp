# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Moving money between funds: post_fund_movement, its routes and the template and cap each
movement type meets at the journal boundary.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank,
4100 sales revenue, 6200 rent, 3200 retained earnings, 2210 long-term loans), funds turned on
(test_fund_movements._setup: restricted fund R "Library gift" for scholarships, board-designated
funds U1 and U2), and a gift of 100 to R in the bank unless a test says otherwise.
"""
from __future__ import annotations

import ast
import uuid

import pytest
from sqlalchemy import update

from celerp.services.journal_accounts import RestrictedFundMovement, fund_account_balances, fund_movement_grant
from test_fund_movements import _emit, _refused, _setup

DATE = "2026-03-02"


async def _gift(session, auth, fund, amount=100):
    """A gift of ``amount`` to ``fund`` (None: the unrestricted group), banked."""
    return await _emit(session, auth, [("1111", amount, 0, fund), ("4100", 0, amount, fund)])


async def _bill(session, auth, fund, amount=60):
    """Purpose spending of ``amount`` charged to ``fund``, paid from the bank; line 0 is the expense."""
    return await _emit(session, auth, [("6200", amount, 0, fund), ("1111", 0, amount, fund)])


async def _post(client, auth, path, **body):
    return await client.post(f"/accounting/funds/{path}", headers=auth["headers"],
                             json={"request_key": uuid.uuid4().hex, "date": DATE, "reason": "Board minute 12", **body})


def _key(r) -> str:
    return r.json()["detail"]["message_key"]


async def _entry(session, auth, je_id) -> dict:
    from celerp.models.projections import Projection
    return (await session.get(Projection, (auth["company_id"], je_id), populate_existing=True)).state


async def _held(session, auth, fund, account="1111"):
    from decimal import Decimal
    return (await fund_account_balances(session, auth["company_id"])).get(fund, {}).get(account, Decimal(0))


async def _set_role(session, auth, role):
    from celerp.models.accounting import UserCompany
    await session.execute(update(UserCompany).where(
        UserCompany.user_id == auth["user_id"], UserCompany.company_id == auth["company_id"]).values(role=role))
    await session.flush()


def test_only_post_fund_movement_enters_the_grant():
    """Outside journal_accounts.py, the grant's name appears in app code only inside
    post_fund_movement in the accounting funds module."""
    from fund_census import sources

    found = []
    for module, path in sources():
        if module == "celerp.services.journal_accounts":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] + [tree]:
            body = fn.body if fn is not tree else [s for s in tree.body
                                                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for node in (n for s in body for n in ast.walk(s)):
                if isinstance(node, (ast.Name, ast.Attribute, ast.alias)) and "fund_movement_grant" in (
                        getattr(node, "id", None), getattr(node, "attr", None), getattr(node, "name", None)):
                    found.append((module, getattr(fn, "name", "<module>")))
    users = {(m, f) for m, f in found if f != "<module>"}
    assert users == {("celerp_accounting.funds", "post_fund_movement")}, found


@pytest.mark.parametrize("je_type", ["fund_transfer", "fund_release", "fund_reallocation", "fund_opening",
                                     "year_close", "manual"])
async def test_net_assets_only_movement_refused(client, session, auth, je_type):
    """R: Dr restricted net assets 100 / Cr bank 100 with U1: Dr bank 100 / Cr retained earnings
    100, entered for its own grant by a manager, matches no type's template and cap."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    lines = [(f["rna"], 100, 0, f["R"]), ("1111", 0, 100, f["R"]), ("1111", 100, 0, f["U1"]), ("3200", 0, 100, f["U1"])]
    je_id = f"je:fund:{uuid.uuid4()}"
    async with fund_movement_grant(auth["company_id"], je_id, auth["user_id"]):
        await _refused(_emit(session, auth, lines, entity_id=je_id, je_type=je_type))


async def test_movement_needs_actor_permission_read_at_boundary(client, session, auth):
    """Inside a grant, an actor whose role lacks manage_accounting is refused, read from the
    database at the boundary; a reallocation also needs reallocate_restricted_funds, which a
    manager does not have by default."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["U1"])
    lines = [("3200", 10, 0, f["U1"]), ("1111", 0, 10, f["U1"]), ("1111", 10, 0, f["U2"]), ("3200", 0, 10, f["U2"])]
    await _set_role(session, auth, "operator")
    je_id = f"je:fund:{uuid.uuid4()}"
    async with fund_movement_grant(auth["company_id"], je_id, auth["user_id"]):
        detail = await _refused(_emit(session, auth, lines, entity_id=je_id, je_type="fund_transfer"))
    assert detail["message_key"] == "posting.fund.movement_permission"
    assert detail["params"]["permission"]["message_key"] == "perm.name.manage_accounting"

    await _set_role(session, auth, "admin")
    r2 = (await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": "Library gift two", "restricted": True,
        "purpose": "Scholarships"})).json()["id"]
    await _gift(session, auth, f["R"])
    await _set_role(session, auth, "manager")
    lines = [(f["rna"], 10, 0, f["R"]), ("1111", 0, 10, f["R"]), ("1111", 10, 0, r2), (f["rna"], 0, 10, r2)]
    je_id = f"je:fund:{uuid.uuid4()}"
    async with fund_movement_grant(auth["company_id"], je_id, auth["user_id"]):
        detail = await _refused(_emit(session, auth, lines, entity_id=je_id, je_type="fund_reallocation"))
    assert detail["params"]["permission"]["message_key"] == "perm.name.reallocate_restricted_funds"


async def test_transfer_validation_restricted_refused(client, session, auth):
    """Transfer between funds: the same fund, an amount not above 0, a restricted source or
    destination, or more than the source holds on the account are refused naming the rule;
    a valid transfer posts and moves the balance."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["U1"])
    await _gift(session, auth, f["R"])
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U1"], amount="10")
    assert r.status_code == 422 and _key(r) == "fund.movement_same_fund", r.text
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="0")
    assert r.status_code == 422, r.text
    r = await _post(client, auth, "transfer", from_fund=f["R"], to_fund=f["U2"], amount="10")
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_template", r.text
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["R"], amount="10")
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_template", r.text
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="101")
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_exceeds_balance", r.text
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=None, amount="40")
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["U1"]) == 60 and await _held(session, auth, None) == 40


async def test_fund_transfer_audited_and_reversible(client, session, auth):
    """A transfer records its reason and the user who authorized it; reversing it moves the
    money back, a second reverse returns the same reversal, and the movement itself cannot be voided."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["U1"])
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="30")
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]
    entry = await _entry(session, auth, je_id)
    assert entry["je_type"] == "fund_transfer" and entry["reason"] == "Board minute 12"
    assert entry["authorized_by"] == str(auth["user_id"])
    back = await client.post(f"/accounting/funds/movements/{je_id}/reverse", headers=auth["headers"],
                             json={"date": DATE, "reason": "Entered twice"})
    assert back.status_code == 200, back.text
    again = await client.post(f"/accounting/funds/movements/{je_id}/reverse", headers=auth["headers"],
                              json={"date": DATE, "reason": "Entered twice"})
    assert again.json()["je_id"] == back.json()["je_id"]
    assert await _held(session, auth, f["U1"]) == 100 and await _held(session, auth, f["U2"]) == 0


async def test_expense_then_release(client, session, auth):
    """Purpose bill 60 charged to R: a release of 60 is refused and R holds 40. The same bill
    charged to the unrestricted group: a release of 60 posts once, a second on the same backing
    is refused, and reversing the release frees it."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    charged = await _bill(session, auth, f["R"])
    r = await _post(client, auth, "release", fund=f["R"], to_fund=None,
                    backing=[{"je_id": charged, "line_index": 0, "amount": "60"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.release_backing_charged", r.text
    assert await _held(session, auth, f["R"]) == 40

    await _gift(session, auth, f["R"])
    await _gift(session, auth, None)
    unrestricted = await _bill(session, auth, None)
    backing = [{"je_id": unrestricted, "line_index": 0, "amount": "60"}]
    r = await _post(client, auth, "release", fund=f["R"], to_fund=None, backing=backing)
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == 80 and await _held(session, auth, None) == 100
    again = await _post(client, auth, "release", fund=f["R"], to_fund=None, backing=backing)
    assert again.status_code == 422 and _key(again) == "posting.fund.release_backing_used", again.text
    back = await client.post(f"/accounting/funds/movements/{r.json()['je_id']}/reverse", headers=auth["headers"],
                             json={"date": DATE, "reason": "Wrong bill"})
    assert back.status_code == 200, back.text
    r = await _post(client, auth, "release", fund=f["R"], to_fund=None, backing=backing)
    assert r.status_code == 200, r.text


async def test_reallocation_same_purpose_authorized_only(client, session, auth):
    """A reallocation goes only to a restricted fund with the identical purpose (never to an
    unrestricted fund, even one given the same purpose), carries a
    reason and a donor authorization reference, and needs reallocate_restricted_funds."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])

    async def fund(name, purpose, restricted=True):
        return (await client.post("/accounting/funds", headers=auth["headers"], json={
            "request_key": uuid.uuid4().hex, "name": name, "restricted": restricted, "purpose": purpose})).json()["id"]

    same, other = await fund("Library gift two", "Scholarships"), await fund("Roof appeal", "Roof")
    designated = await fund("Scholarship reserve", "Scholarships", restricted=False)
    ok = {"from_fund": f["R"], "amount": "25", "donor_authorization": "Letter 2026-02"}
    for to_fund in (other, f["U1"], designated):
        r = await _post(client, auth, "reallocate", **{**ok, "to_fund": to_fund})
        assert r.status_code == 422 and _key(r) == "posting.fund.movement_template", r.text
    r = await _post(client, auth, "reallocate", **{**ok, "to_fund": same, "donor_authorization": " "})
    assert r.status_code == 422 and _key(r) == "posting.fund.reallocation_needs_authorization", r.text
    await _set_role(session, auth, "manager")
    r = await _post(client, auth, "reallocate", **{**ok, "to_fund": same})
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_permission", r.text
    await _set_role(session, auth, "admin")
    r = await _post(client, auth, "reallocate", **{**ok, "to_fund": same})
    assert r.status_code == 200, r.text
    assert await _held(session, auth, same) == 25 and await _held(session, auth, f["R"]) == 75


async def test_opening_cannot_exceed_unrestricted_balance(client, session, auth):
    """Opening balances by fund: assigning more of an account than the unrestricted group holds
    on it is refused naming the account and the excess; a liability alone cannot be assigned to
    a restricted fund; an asset with its liability can."""
    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 100, 0, None), ("3200", 0, 100, None)])
    await _emit(session, auth, [("1111", 30, 0, None), ("2210", 0, 30, None)])
    r = await _post(client, auth, "opening", assignments=[{"account": "1111", "fund": f["R"], "amount": "60"}])
    assert r.status_code == 200, r.text
    r = await _post(client, auth, "opening", assignments=[{"account": "1111", "fund": f["R"], "amount": "80"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_exceeds_balance", r.text
    assert r.json()["detail"]["params"]["code"] == "1111" and r.json()["detail"]["params"]["available"] == "70.00"
    r = await _post(client, auth, "opening", assignments=[{"account": "2210", "fund": f["R"], "amount": "30"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.opening_restricted_negative", r.text
    r = await _post(client, auth, "opening", assignments=[{"account": "2210", "fund": f["R"], "amount": "30"},
                                                         {"account": "1111", "fund": f["R"], "amount": "30"}])
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == 90 and await _held(session, auth, f["R"], "2210") == -30


async def test_opening_balances_by_fund_validation(client, session, auth):
    """An opening assignment to an unknown or archived fund, on an income or expense account,
    or from a restricted fund is refused naming it."""
    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 100, 0, None), ("3200", 0, 100, None)])
    r = await _post(client, auth, "opening", assignments=[{"account": "1111", "fund": "nope", "amount": "10"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.unknown", r.text
    r = await _post(client, auth, "opening", assignments=[{"account": "6200", "fund": f["U1"], "amount": "10"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_income_expense", r.text
    r = await _post(client, auth, "opening", assignments=[{"account": "1111", "fund": None, "amount": "10"}])
    assert r.status_code == 422 and _key(r) == "fund.movement_same_fund", r.text


async def test_void_of_movement_refused_on_every_path(client, session, auth):
    """A posted movement cannot be voided: not by the void route, not by the automatic void,
    not by a void event emitted directly. It is undone by its reversal."""
    from celerp.events.engine import emit_event
    from celerp.services.auto_je import _void_je_if_posted
    from fastapi import HTTPException

    f = await _setup(client, auth)
    await _gift(session, auth, f["U1"])
    je_id = (await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="30")).json()["je_id"]
    r = await client.post(f"/accounting/journal-entries/{je_id}/void", headers=auth["headers"], json={"reason": "x"})
    assert r.status_code == 422, r.text
    for attempt in (
        lambda: _void_je_if_posted(session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="d",
                                   je_id=je_id, idem_key=uuid.uuid4().hex, reason="x", trigger="test"),
        lambda: emit_event(session, company_id=auth["company_id"], entity_id=je_id, entity_type="journal_entry",
                           event_type="acc.journal_entry.voided", data={"void_reason": "x"},
                           actor_id=auth["user_id"], location_id=None, source="test",
                           idempotency_key=uuid.uuid4().hex, metadata_={})):
        with pytest.raises(HTTPException) as raised:
            await attempt()
        assert raised.value.detail["message_key"] == "posting.fund.movement_void"
    assert await _held(session, auth, f["U2"]) == 30


async def test_fund_routes_pass_authenticated_actor(client, session, auth):
    """A movement route records the signed-in user, never one named in the body."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["U1"])
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="5",
                    actor=str(uuid.uuid4()), authorized_by=str(uuid.uuid4()))
    assert r.status_code == 200, r.text
    entry = await _entry(session, auth, r.json()["je_id"])
    assert entry["authorized_by"] == str(auth["user_id"])
    with pytest.raises(RestrictedFundMovement):
        await _emit(session, auth, [("3200", 1, 0, f["U1"]), ("1111", 0, 1, f["U1"]), ("1111", 1, 0, f["U2"]),
                                    ("3200", 0, 1, f["U2"])], je_type="fund_transfer")
