# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Funds: the setting, fund maintenance, and the fund rules every journal entry meets.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank, 4100 sales
revenue, 6200 rent). Funds are turned on through PUT /accounting/funds/setting, which on
first use adds the restricted net assets account. A line's fund is its ``fund_id``; a line
without one belongs to the unrestricted group. Every fund balances on its own, and a fund
that is archived takes no new posting, though a reversal or a restored entry still lands.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from celerp.services.journal_accounts import UnbalancedJournalEntry

pytestmark = pytest.mark.asyncio


async def _funds_on(client, auth, enabled: bool = True):
    return await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": enabled})


async def _create(client, auth, name: str, *, restricted: bool = True, purpose: str | None = "Scholarships"):
    return await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": name, "restricted": restricted, "purpose": purpose})


async def _fund(client, auth, name: str, **kw) -> str:
    r = await _create(client, auth, name, **kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _je(lines: list[tuple[str, float, float, str | None]]) -> list[dict]:
    return [{"account": a, "debit": d, "credit": c, **({"fund_id": f} if f else {})} for a, d, c, f in lines]


async def _manual(client, auth, lines):
    return await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-01", "memo": "Gift", "entries": _je(lines), "idempotency_token": uuid.uuid4().hex})


async def _emit(session, auth, lines, *, reverses: str | None = None, restores: bool = False) -> str:
    from celerp.events.engine import emit_event
    je_id = f"je:manual:{uuid.uuid4()}"
    await emit_event(session, company_id=auth["company_id"], entity_id=je_id, entity_type="journal_entry",
                     event_type="acc.journal_entry.created",
                     data={"memo": "Gift", "ts": "2026-03-01", "entries": _je(lines), "je_type": "manual",
                           "status": "posted", **({"reverses": reverses} if reverses else {})},
                     actor_id=auth["user_id"], location_id=None, source="test",
                     idempotency_key=f"test:{je_id}", metadata_={"restores": True} if restores else {})
    return je_id


def _refusal(r, key: str) -> dict:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key, detail
    return detail["params"]


async def test_fund_unbalanced_entry_refused_at_prepare(client, session, auth):
    assert (await _funds_on(client, auth)).status_code == 200
    gift = await _fund(client, auth, "Library gift")
    params = _refusal(await _manual(client, auth, [("1111", 100, 0, gift), ("4100", 0, 100, None)]),
                      "posting.fund.unbalanced")
    assert params["fund"] == "Library gift" and params["difference"] == "100.00"
    # The unrestricted group is one fund of its own: its side is named too when it is short.
    with pytest.raises(UnbalancedJournalEntry) as raised:
        await _emit(session, auth, [("1111", 0, 40, None), ("6200", 40, 0, gift)])
    assert raised.value.detail["message_key"] == "posting.fund.unbalanced"
    # Balanced per fund, the same lines post.
    r = await _manual(client, auth, [("1111", 100, 0, gift), ("4100", 0, 100, gift),
                                     ("6200", 30, 0, None), ("1111", 0, 30, None)])
    assert r.status_code == 200, r.text


async def test_archived_fund_refuses_new_postings_allows_reversal(client, session, auth):
    assert (await _funds_on(client, auth)).status_code == 200
    gift = await _fund(client, auth, "Roof appeal")
    first = await _manual(client, auth, [("1111", 50, 0, gift), ("4100", 0, 50, gift)])
    assert first.status_code == 200, first.text
    correction = await _emit(session, auth, [("4100", 50, 0, gift), ("1111", 0, 50, gift)])
    archived = await client.post(f"/accounting/funds/{gift}/archive", headers=auth["headers"])
    assert archived.status_code == 200, archived.text

    params = _refusal(await _manual(client, auth, [("1111", 10, 0, gift), ("4100", 0, 10, gift)]),
                      "posting.fund.archived")
    assert params["fund"] == "Roof appeal"
    # A reversal of what the fund already holds, and a restored entry, still land.
    await _emit(session, auth, [("1111", 50, 0, gift), ("4100", 0, 50, gift)], reverses=correction)
    await _emit(session, auth, [("1111", 5, 0, gift), ("4100", 0, 5, gift)], restores=True)
    # A fund nobody made is refused by name.
    params = _refusal(await _manual(client, auth, [("1111", 1, 0, "fund:nope"), ("4100", 0, 1, "fund:nope")]),
                      "posting.fund.unknown")
    assert params["fund"] == "fund:nope"

    restored = await client.post(f"/accounting/funds/{gift}/unarchive", headers=auth["headers"])
    assert restored.status_code == 200, restored.text
    r = await _manual(client, auth, [("1111", 10, 0, gift), ("4100", 0, 10, gift)])
    assert r.status_code == 200, r.text


async def test_fund_create_update_archive_validation(client, auth):
    # No fund exists until funds are on.
    _refusal(await _create(client, auth, "Early"), "fund.not_enabled")
    assert (await _funds_on(client, auth)).status_code == 200
    assert _refusal(await _create(client, auth, "  "), "fund.name_required") == {}
    gift = await _fund(client, auth, "Music fund")
    assert _refusal(await _create(client, auth, "music FUND"), "fund.name_taken")["name"] == "music FUND"
    _refusal(await _create(client, auth, "No purpose", purpose=" "), "fund.purpose_required")
    board = await _fund(client, auth, "Board reserve", restricted=False, purpose=None)

    r = await client.patch(f"/accounting/funds/{board}", headers=auth["headers"], json={"name": "Music Fund"})
    assert _refusal(r, "fund.name_taken")["name"] == "Music Fund"
    r = await client.patch(f"/accounting/funds/{gift}", headers=auth["headers"], json={"purpose": "Instruments"})
    assert r.status_code == 200, r.text  # no postings yet: the restriction may still change

    assert (await _manual(client, auth, [("1111", 80, 0, gift), ("4100", 0, 80, gift)])).status_code == 200
    for change in ({"purpose": "Tuition"}, {"restricted": False}):
        r = await client.patch(f"/accounting/funds/{gift}", headers=auth["headers"], json=change)
        assert _refusal(r, "fund.restriction_locked")["fund"] == "Music fund"
    r = await client.patch(f"/accounting/funds/{gift}", headers=auth["headers"], json={"name": "Music and arts"})
    assert r.status_code == 200, r.text

    params = _refusal(await client.post(f"/accounting/funds/{gift}/archive", headers=auth["headers"]),
                      "fund.balance_held")
    assert "Music and arts" in params["funds"] and "1111" in params["funds"]
    # Archive and unarchive of a fund with nothing in it, each repeat-safe.
    for _ in range(2):
        assert (await client.post(f"/accounting/funds/{board}/archive", headers=auth["headers"])).status_code == 200
    listed = {f["id"]: f for f in (await client.get("/accounting/funds", headers=auth["headers"])).json()["items"]}
    assert listed[board]["archived"] is True and listed[gift]["archived"] is False
    assert listed[gift] | {"archived": False} == {"id": gift, "name": "Music and arts", "restricted": True,
                                                  "purpose": "Instruments", "archived": False}
    for _ in range(2):
        assert (await client.post(f"/accounting/funds/{board}/unarchive", headers=auth["headers"])).status_code == 200
    listed = {f["id"]: f for f in (await client.get("/accounting/funds", headers=auth["headers"])).json()["items"]}
    assert listed[board]["archived"] is False


async def test_funds_cannot_be_turned_off_while_restricted_balances(client, auth):
    assert (await _funds_on(client, auth)).status_code == 200
    roof = await _fund(client, auth, "Roof appeal")
    music = await _fund(client, auth, "Music fund")
    empty = await _fund(client, auth, "Empty fund")
    assert (await _manual(client, auth, [("1111", 70, 0, roof), ("4100", 0, 70, roof),
                                         ("1111", 20, 0, music), ("4100", 0, 20, music)])).status_code == 200
    params = _refusal(await _funds_on(client, auth, False), "fund.balance_held")
    assert "Roof appeal" in params["funds"] and "Music fund" in params["funds"]
    assert "Empty fund" not in params["funds"]

    # Once the funds hold nothing, funds go off; a posting then carries no fund.
    assert (await _manual(client, auth, [("4100", 70, 0, roof), ("1111", 0, 70, roof),
                                         ("4100", 20, 0, music), ("1111", 0, 20, music)])).status_code == 200
    assert (await _funds_on(client, auth, False)).status_code == 200
    _refusal(await _manual(client, auth, [("1111", 5, 0, empty), ("4100", 0, 5, empty)]), "posting.fund.funds_off")
    assert (await _manual(client, auth, [("1111", 5, 0, None), ("4100", 0, 5, None)])).status_code == 200
    # Turning funds on again resumes the checks.
    assert (await _funds_on(client, auth)).status_code == 200
    _refusal(await _manual(client, auth, [("1111", 5, 0, empty), ("4100", 0, 5, None)]), "posting.fund.unbalanced")


async def test_net_assets_lines_follow_the_fund_kind(client, auth):
    """Rule 3: a restricted net assets line carries a restricted fund, a retained earnings
    line never does. Turning funds on adds the restricted net assets account once."""
    assert (await _funds_on(client, auth)).status_code == 200
    assert (await _funds_on(client, auth)).status_code == 200
    accounts = (await client.get("/accounting/posting-accounts", headers=auth["headers"])).json()
    rna = next(a for a in accounts["roles"] if a["role"] == "restricted_net_assets")["code"]
    gift = await _fund(client, auth, "Library gift")
    board = await _fund(client, auth, "Board reserve", restricted=False, purpose=None)
    _refusal(await _manual(client, auth, [("1111", 10, 0, None), (rna, 0, 10, None)]),
             "posting.fund.restricted_net_assets_needs_fund")
    _refusal(await _manual(client, auth, [("1111", 10, 0, board), (rna, 0, 10, board)]),
             "posting.fund.restricted_net_assets_needs_fund")
    _refusal(await _manual(client, auth, [("1111", 10, 0, gift), ("3200", 0, 10, gift)]),
             "posting.fund.retained_earnings_restricted")


async def test_no_funds_company_posts_as_before(session, auth):
    """A fund on a line in a company that never turned funds on is refused; nothing else changes."""
    with pytest.raises(HTTPException) as raised:
        await _emit(session, auth, [("1111", 5, 0, "fund:any"), ("4100", 0, 5, "fund:any")])
    assert raised.value.detail["message_key"] == "posting.fund.funds_off"
    await _emit(session, auth, [("1111", 5, 0, None), ("4100", 0, 5, None)])


async def test_restricted_net_assets_unmapped_only_once_funds_are_on(client, session, auth):
    """A seeded company with funds off has every role it reconciles mapped, so placing its
    stock at a start or a restore raises no posting-account notice; with funds on and
    restricted net assets unmapped, the role is reported."""
    from celerp.models.company import Company
    from celerp.services.account_roles import reconcile_company, unmapped_roles

    assert await reconcile_company(session, auth["company_id"]) == []
    settings = dict((await session.get(Company, auth["company_id"])).settings or {})
    assert unmapped_roles({**settings, "funds_enabled": True}) == ["restricted_net_assets"]


_STOCK_ROLES = ("inventory", "inventory_purchased", "inventory_opening", "cogs", "work_in_progress",
                "landed_freight", "landed_insurance", "landed_duty", "landed_import_vat", "consignor_payable")


async def _role_account(client, auth, role: str, spare: str) -> str:
    """The account ``role`` posts to, mapping it to a new asset account ``spare`` when unmapped."""
    accounts = (await client.get("/accounting/posting-accounts", headers=auth["headers"])).json()
    code = next(a for a in accounts["roles"] if a["role"] == role).get("code")
    if code:
        return code
    kind = "liability" if role == "consignor_payable" else "asset"
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": spare, "name": f"Stock {role}", "account_type": kind})
    assert r.status_code in (200, 201), r.text
    r = await client.put(f"/accounting/posting-accounts/{role}", headers=auth["headers"], json={"code": spare})
    assert r.status_code == 200, r.text
    return spare


async def test_stock_lines_refuse_restricted_fund(client, session, auth):
    """Rule 7: a line on an inventory, cost of goods sold, work in progress, landed-cost clearing
    or consignor payable account carrying a restricted fund is refused naming the account. The
    same line in an unrestricted fund, or in no fund, posts."""
    assert (await _funds_on(client, auth)).status_code == 200
    gift = await _fund(client, auth, "Library gift")
    board = await _fund(client, auth, "Board reserve", restricted=False, purpose=None)
    for n, role in enumerate(_STOCK_ROLES):
        code = await _role_account(client, auth, role, f"19{n:02d}")
        with pytest.raises(HTTPException) as raised:
            await _emit(session, auth, [(code, 10, 0, gift), ("1111", 0, 10, gift)])
        assert raised.value.detail["message_key"] == "posting.fund.stock_unrestricted", (role, raised.value.detail)
        assert raised.value.detail["params"]["code"] == code
        await _emit(session, auth, [(code, 10, 0, board), ("1111", 0, 10, board)])
        await _emit(session, auth, [(code, 10, 0, None), ("1111", 0, 10, None)])

    # A bill line bringing stock in with a restricted fund is refused at finalize with the
    # reason, and the bill stays a draft; with a board-designated fund it posts.
    from test_credit_note_settlement_owned import _post
    from test_income_accounts import _draft, _issued, _item, _line

    paper = await _item(client, auth, "Paper stock")
    bill = await _draft(client, auth, "bill", [{**_line(paper, "Paper stock", 40), "receive_as": "stock", "fund_id": gift}])
    r = await _post(client, auth, f"/docs/{bill}/finalize")
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.fund.stock_unrestricted", r.text
    assert (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()["status"] == "draft"
    await _issued(client, auth, "bill", [{**_line(paper, "Paper stock", 40), "receive_as": "stock", "fund_id": board}])
