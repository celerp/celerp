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
from datetime import date

import pytest
from sqlalchemy import update

from celerp.services.journal_accounts import RestrictedFundMovement, fund_account_balances, fund_movement_grant
from test_fund_movements import _emit, _refused, _setup

DATE = "2026-03-02"


async def _gift(session, auth, fund, amount=100):
    """A gift of ``amount`` to ``fund`` (None: the unrestricted group), banked."""
    return await _emit(session, auth, [("1111", amount, 0, fund), ("4100", 0, amount, fund)])


async def _bill(session, auth, fund, amount=60, ts="2026-03-01"):
    """Purpose spending of ``amount`` charged to ``fund``, paid from the bank; line 0 is the expense."""
    return await _emit(session, auth, [("6200", amount, 0, fund), ("1111", 0, amount, fund)], ts=ts)


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


async def test_backdated_movement_cannot_take_what_later_spending_used(client, session, auth):
    """U1 holds 100 on the movement's date but spends 80 of it a month later: a transfer of 100
    dated before the spending is refused, as it would leave U1 owing 80 from then on, and 20
    moves. A bank transfer's allocation by fund is held to the same least balance."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["U1"])
    await _bill(session, auth, f["U1"], 80, ts="2026-04-01")
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="100")
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_exceeds_balance", r.text
    assert r.json()["detail"]["params"]["available"] == "20.00"
    r = await _post(client, auth, "transfer", from_fund=f["U1"], to_fund=f["U2"], amount="20")
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["U1"]) == 0 and await _held(session, auth, f["U2"]) == 20

    await _gift(session, auth, f["R"])
    await _bill(session, auth, f["R"], 70, ts="2026-04-01")
    bank = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
        "bank_name": "Coast Bank", "account_number": "0002", "bank_type": "checking", "currency": "USD",
        "opening_balance": 0})
    source = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking", "currency": "USD",
        "opening_balance": 0, "account_code": "1111"})
    r = await client.post("/accounting/transfers", headers=auth["headers"], json={
        "from_bank_id": source.json()["id"], "to_bank_id": bank.json()["id"], "amount": 100.0, "date": DATE,
        "allocation": [{"fund": f["R"], "amount": 100}]})
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_exceeds_balance", r.text
    assert r.json()["detail"]["params"]["available"] == "30.00"


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


async def test_release_requires_purpose_spending_and_reverse_undoes(client, session, auth):
    """A release above the purpose spending it names is refused naming the shortfall rule; a
    release from an unrestricted fund, or to a restricted one, is refused by the template; a
    release within its spending posts and reversing it puts every fund back as it was."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    await _gift(session, auth, None)
    spending = await _bill(session, auth, None)
    r = await _post(client, auth, "release", fund=f["R"], to_fund=None,
                    backing=[{"je_id": spending, "line_index": 0, "amount": "80"}])
    assert r.status_code == 422 and _key(r) == "posting.fund.release_backing_used", r.text
    backing = [{"je_id": spending, "line_index": 0, "amount": "60"}]
    await _gift(session, auth, f["U1"])
    r = await _post(client, auth, "release", fund=f["U1"], to_fund=None, backing=backing)
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_template", r.text
    r2 = await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": "Second scholarship", "restricted": True, "purpose": "Scholarships"})
    r = await _post(client, auth, "release", fund=f["R"], to_fund=r2.json()["id"], backing=backing)
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_template", r.text
    before = await fund_account_balances(session, auth["company_id"])

    r = await _post(client, auth, "release", fund=f["R"], to_fund=None, backing=backing)
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == 40 and await _held(session, auth, None) == 100
    back = await client.post(f"/accounting/funds/movements/{r.json()['je_id']}/reverse", headers=auth["headers"],
                             json={"date": DATE, "reason": "Wrong bill"})
    assert back.status_code == 200, back.text
    assert await fund_account_balances(session, auth["company_id"]) == before


@pytest.mark.parametrize("writer", ["manual", "auto", "transfer", "recon_create", "recon_split", "recon_writeoff",
                                    "year_close", "close_in_grant", "reversal"])
async def test_restricted_money_leaves_only_by_release_or_authorized_reallocation(client, session, auth, writer):
    """Whichever writer produced it, an entry that takes money out of the restricted fund R is
    judged at the journal boundary: the entry types of manual entries, document postings, bank
    transfers, statement lines and year closes giving R's money to another fund are refused
    outside a movement; a year close cannot carry a fund's results to another fund; and the
    reversal of a reallocation or of an opening, taking money out of a restricted fund, needs
    the authority to reallocate."""
    from celerp_accounting.funds import post_fund_movement
    from celerp.services.journal_accounts import YEAR_CLOSE, UnbalancedJournalEntry

    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    if writer == "reversal":
        r2 = await client.post("/accounting/funds", headers=auth["headers"], json={
            "request_key": uuid.uuid4().hex, "name": "Second scholarship", "restricted": True,
            "purpose": "Scholarships"})
        await _gift(session, auth, None, 30)
        moved = [await _post(client, auth, "reallocate", from_fund=f["R"], to_fund=r2.json()["id"], amount="25",
                             donor_authorization="Donor letter 3"),
                 await _post(client, auth, "opening", assignments=[{"account": "1111", "fund": f["R"], "amount": "30"}])]
        assert [m.status_code for m in moved] == [200, 200], [m.text for m in moved]
        reverse = lambda m: client.post(f"/accounting/funds/movements/{m.json()['je_id']}/reverse",
                                        headers=auth["headers"], json={"date": DATE, "reason": "Donor withdrew"})
        await _set_role(session, auth, "manager")
        for m in moved:
            r = await reverse(m)
            assert r.status_code == 422 and _key(r) == "posting.fund.movement_permission", r.text
        assert await _held(session, auth, r2.json()["id"]) == 25 and await _held(session, auth, f["R"]) == 105
        await _set_role(session, auth, "admin")
        assert [(await reverse(m)).status_code for m in moved] == [200, 200]
    elif writer == "close_in_grant":
        await session.commit()  # a close starts its own transaction, as the route's does
        with pytest.raises(UnbalancedJournalEntry):
            await post_fund_movement(
                session, auth["company_id"], je_id="je:close:2026-12-31", kind=YEAR_CLOSE, ts=date(2026, 12, 31),
                reason="Close", actor=auth["user_id"],
                entries=[{"account": "4100", "debit": 100, "credit": 0, "fund_id": f["R"]},
                         {"account": "3200", "debit": 0, "credit": 100, "fund_id": f["U1"]}])
    else:
        cross = [("6200", 25, 0, f["R"]), ("1111", 0, 25, f["R"]), ("1111", 25, 0, f["U1"]), ("4100", 0, 25, f["U1"])]
        detail = await _refused(_emit(session, auth, cross, je_type=writer))
        assert detail["message_key"] == "posting.fund.movement_outside"
    assert await _held(session, auth, f["R"]) == 100


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


async def test_transfer_bank_to_bank_keeps_fund_split(client, session, auth):
    """A bank transfer moves each fund's share of the source account with it (rule B): R's 100
    gift in bank A moved to bank B leaves R holding bank B. With the unrestricted group also in
    bank A, a transfer splits pro rata to what each holds there; an explicit allocation by fund
    replaces the split, never above what a fund holds there and adding up to the amount moved."""
    f = await _setup(client, auth)

    async def bank(name, number, code=None):
        r = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
            "bank_name": name, "account_number": number, "bank_type": "checking", "currency": "USD",
            "opening_balance": 0, **({"account_code": code} if code else {})})
        assert r.status_code == 200, r.text
        return r.json()

    a, b = await bank("Harbor Bank", "0001", "1111"), await bank("Coast Bank", "0002")
    to = b["chart_account_code"]

    async def transfer(amount, **extra):
        return await client.post("/accounting/transfers", headers=auth["headers"], json={
            "from_bank_id": a["id"], "to_bank_id": b["id"], "amount": amount, "date": DATE, **extra})

    await _gift(session, auth, f["R"])
    r = await transfer(100.0)
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"], to) == 100 and await _held(session, auth, f["R"]) == 0

    await _gift(session, auth, f["R"])
    await _gift(session, auth, None, 50)
    r = await transfer(60.0)
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"], to) == 140 and await _held(session, auth, None, to) == 20
    assert await _held(session, auth, f["R"]) == 60 and await _held(session, auth, None) == 30
    from celerp_accounting.models import BankAccount
    from celerp_accounting.routes import _je_entries_for_account
    rows = await _je_entries_for_account(session, auth["company_id"], await session.get(BankAccount, uuid.UUID(b["id"])))
    assert [row["amount"] for row in rows if row["je_id"] == r.json()["je_id"]] == [60.0]

    r = await transfer(40.0, allocation=[{"fund": None, "amount": 31}, {"fund": f["R"], "amount": 9}])
    assert r.status_code == 422 and _key(r) == "posting.fund.movement_exceeds_balance", r.text
    assert r.json()["detail"]["params"]["available"] == "30.00"
    r = await transfer(40.0, allocation=[{"fund": None, "amount": 30}, {"fund": f["R"], "amount": 5}])
    assert r.status_code == 422 and _key(r) == "fund.transfer_allocation_total", r.text
    r = await transfer(40.0, allocation=[{"fund": None, "amount": 30}, {"fund": f["R"], "amount": 10}])
    assert r.status_code == 200, r.text
    assert await _held(session, auth, None) == 0 and await _held(session, auth, f["R"]) == 50
    assert await _held(session, auth, None, to) == 50 and await _held(session, auth, f["R"], to) == 150


async def test_fund_balances_follow_a_void_out_of_a_locked_period(client, session, auth):
    """An entry voided after its period locked stays on its own day and its reversal posts
    on the reversal date, so a fund holds the gift between the two and nothing after."""
    f = await _setup(client, auth)
    je = await _gift(session, auth, f["U1"], 20)
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": "2026-03-31"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/journal-entries/{je}/void", headers=auth["headers"],
                          json={"reason": "Posted twice", "reversal_date": "2026-05-02"})
    assert r.status_code == 200, r.text
    held = lambda as_of: fund_account_balances(session, auth["company_id"], as_of=as_of)
    assert (await held("2026-04-30")).get(f["U1"]) == {"1111": 20, "4100": -20}
    assert f["U1"] not in await held("2026-05-02") and f["U1"] not in await held(None)


async def _close(client, auth, year_end="2026-12-31"):
    return await client.post("/accounting/close-year", headers=auth["headers"], json={"fiscal_year_end": year_end})


async def _results(session, auth, account):
    """Each fund's balance on ``account``, through the year end."""
    return {fund: held.get(account) for fund, held in (await fund_account_balances(
        session, auth["company_id"], as_of="2026-12-31")).items() if held.get(account)}


async def test_year_close_by_fund_no_double_count(client, session, auth):
    """Each fund's income and expense close to its own net assets: R's gift less R's spending
    to restricted net assets on R, a board-designated fund's to retained earnings on that fund,
    the unrestricted group's to retained earnings with no fund. No result is counted twice,
    and every income and expense account ends the year at zero on every fund."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    await _bill(session, auth, f["R"], 30)
    await _gift(session, auth, f["U1"], 20)
    await _gift(session, auth, None, 50)
    await _bill(session, auth, None, 15)
    before = await _results(session, auth, "3200")
    r = await _close(client, auth)
    assert r.status_code == 200, r.text
    assert await _results(session, auth, "4100") == {} and await _results(session, auth, "6200") == {}
    assert await _results(session, auth, f["rna"]) == {f["R"]: -70}
    after = await _results(session, auth, "3200")
    assert after.get(f["U1"]) == -20 and after.get(None, 0) - before.get(None, 0) == -35
    assert f["R"] not in after
    entry = await _entry(session, auth, r.json()["je_id"])
    assert entry["je_type"] == "year_close" and entry["authorized_by"] == str(auth["user_id"])


async def test_year_close_reverse_restores_and_reclose(client, session, auth):
    """Reverse close, once the period is open again, restores every fund's income and expense
    balances; reversing again returns the same reversal; closing again posts a new entry under
    the next cycle. While the year is locked the reversal is refused. A manager, who may manage
    accounting but not reallocate restricted funds, may reverse it: each fund's results return
    to that same fund."""
    f = await _setup(client, auth)
    await _gift(session, auth, f["R"])
    await _gift(session, auth, None, 50)
    closed = await _close(client, auth)
    assert closed.status_code == 200, closed.text
    reverse = lambda: client.post("/accounting/close-year/2026-12-31/reverse", headers=auth["headers"],
                                  json={"reason": "Late bills"})
    r = await reverse()
    assert r.status_code in (409, 422), r.text
    assert await _results(session, auth, "4100") == {}

    await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": None})
    await _set_role(session, auth, "manager")
    r = await reverse()
    assert r.status_code == 200, r.text
    assert await _results(session, auth, "4100") == {f["R"]: -100, None: -50}
    assert await _results(session, auth, f["rna"]) == {}
    again = await reverse()
    assert again.status_code == 200 and again.json()["je_id"] == r.json()["je_id"], again.text
    assert await _results(session, auth, "4100") == {f["R"]: -100, None: -50}

    reclosed = await _close(client, auth)
    assert reclosed.status_code == 200, reclosed.text
    assert reclosed.json()["je_id"] != closed.json()["je_id"]
    assert await _results(session, auth, "4100") == {} and await _results(session, auth, f["rna"]) == {f["R"]: -100}
