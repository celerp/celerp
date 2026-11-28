# CLASS balance precision (copies of posted lines).
"""A line that copies a posted entry of these books keeps what that entry stored: every key
it posted with (its amount in a foreign bank's currency included) and its amount at the
books it was posted on, even after the company changed its books' currency. A line that
only claims to copy one is checked like any other.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

import bank_statement_helpers as B
from test_recon_bank_writer_guard import DAY, _fund_bank


async def _state(session, co, je_id: str) -> dict:
    from celerp.models.projections import Projection

    session.expire_all()
    row = await session.get(Projection, (uuid.UUID(co["company_id"]), je_id))
    return row.state


async def _stored_before(session, co, je_id: str, line, extra=(), **fields) -> list[dict]:
    """The record of ``je_id`` as an older release stored it: each line through ``line``, then
    the lines in ``extra``, and ``fields`` on the entry, written to the record a reversal reads.
    Returns the stored lines."""
    from sqlalchemy.orm.attributes import flag_modified

    from celerp.models.projections import Projection

    row = await session.get(Projection, (uuid.UUID(co["company_id"]), je_id))
    older = [line(e) for e in row.state["entries"]] + list(extra)
    row.state = {**row.state, **fields, "entries": older}
    flag_modified(row, "state")
    await session.commit()
    return older


async def _books_in(session, co, currency: str) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, uuid.UUID(co["company_id"]))
    company.settings = {**(company.settings or {}), "currency": currency}
    await session.commit()


async def _given(client, h, account: str, amount: float, fund: str, **fx) -> None:
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Gift", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": account, "debit": amount, "credit": 0.0, "fund_id": fund, **fx},
                    {"account": "4100", "debit": 0.0, "credit": round(amount * fx.get("rate", 1), 2), "fund_id": fund}]})
    assert r.status_code == 200, r.text


async def _transfer(client, h, ids, account: str, amount: float) -> str:
    r = await client.post("/accounting/funds/transfer", headers=h, json={
        "request_key": uuid.uuid4().hex, "date": DAY, "reason": "Set aside",
        "from_fund": ids["U1"], "to_fund": ids["U2"], "amount": amount, "account": account})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _reverse(client, h, je_id: str) -> str:
    r = await client.post(f"/accounting/funds/movements/{je_id}/reverse", headers=h,
                          json={"date": DAY, "reason": "Entered twice"})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def test_a_fund_reversal_keeps_the_cents_its_movement_posted_with_after_the_books_move_to_yen(client, session):
    co = await B.company(client)
    h = co["h"]
    ids = await _fund_bank(client, h, "1111")
    await _given(client, h, "1111", 10.55, ids["U1"])
    moved = await _transfer(client, h, ids, "1111", 10.55)
    await _books_in(session, co, "JPY")
    reversal = await _state(session, co, await _reverse(client, h, moved))
    assert sorted((e["debit"], e["credit"]) for e in reversal["entries"]) == [(0.0, 10.55)] * 2 + [(10.55, 0.0)] * 2


async def test_a_fund_reversal_on_a_foreign_bank_keeps_the_amount_in_the_bank_currency(client, session):
    from celerp_accounting.funds import post_fund_movement

    co = await B.company(client)
    h = co["h"]
    code = (await B.bank(client, h, currency="EUR", name="Euro Bank"))["chart_account_code"]
    ids = await _fund_bank(client, h, code)
    await _given(client, h, code, 500.0, ids["U1"], currency="EUR", rate=1.1)
    # A movement whose bank lines carry their euros, as a writer of prebuilt lines posts it.
    plain = (await _state(session, co, await _transfer(client, h, ids, code, 110.0)))["entries"]
    lines = [{**e, "fx_currency": "EUR", **({"fx_debit": 100.0} if e["debit"] else {"fx_credit": 100.0})}
             if e["account"] == code else e for e in plain]
    moved = await post_fund_movement(
        session, uuid.UUID(co["company_id"]), ts=date.fromisoformat(DAY), reason="Set aside",
        actor=uuid.UUID(co["user_id"]), request_key=uuid.uuid4().hex, kind="fund_transfer", entries=lines)
    await session.commit()
    reversal = await _state(session, co, await _reverse(client, h, moved["je_id"]))
    fx = sorted((e.get("fund_id"), e.get("fx_debit"), e.get("fx_credit")) for e in reversal["entries"] if e["account"] == code)
    assert fx == sorted([(ids["U1"], 100.0, None), (ids["U2"], None, 100.0)])
    bank = next(b for b in (await client.get("/accounting/bank-accounts", headers=h)).json()["items"]
                if b["chart_account_code"] == code)
    assert bank["balance"] == 500.0, bank


@pytest.mark.parametrize("named", ["stored", "absent"])
async def test_a_line_naming_a_posted_entry_it_does_not_copy_is_kept_at_the_books_precision(client, session, named):
    from celerp.events.engine import emit_event

    co = await B.company(client)
    h = co["h"]
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Sale", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "6200", "debit": 10.55, "credit": 0.0}, {"account": "4100", "debit": 0.0, "credit": 10.55}]})
    assert r.status_code == 200, r.text
    await _books_in(session, co, "JPY")
    source = r.json()["je_id"] if named == "stored" else "je:none"
    je_id = f"je:{uuid.uuid4()}"
    await emit_event(session, company_id=uuid.UUID(co["company_id"]), entity_id=je_id, entity_type="journal_entry",
                     event_type="acc.journal_entry.created",
                     data={"ts": DAY, "memo": "claims a copy", "status": "posted", "entries": [
                         {"account": "6200", "debit": 0.0, "credit": 10.45, "posted_from": source},
                         {"account": "4100", "debit": 10.45, "credit": 0.0, "posted_from": source}]},
                     actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex, metadata_={})
    await session.commit()
    stored = await _state(session, co, je_id)
    assert sorted((e["debit"], e["credit"]) for e in stored["entries"]) == [(0.0, 10.0), (10.0, 0.0)]


async def test_an_automatic_copy_of_posted_lines_keeps_their_cents_after_the_books_move_to_yen(client, session):
    from celerp.services.auto_je import _emit_auto_posted_je
    from celerp.services.peer_lines import copy_posted_lines

    co = await B.company(client)
    h = co["h"]
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Sale", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "6200", "debit": 10.55, "credit": 0.0}, {"account": "4100", "debit": 0.0, "credit": 10.55}]})
    assert r.status_code == 200, r.text
    source = r.json()["je_id"]
    await _books_in(session, co, "JPY")
    je_id = f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=uuid.UUID(co["company_id"]), user_id=None, je_id=je_id, record_id=None,
        idem_create=uuid.uuid4().hex, idem_posted=uuid.uuid4().hex, memo="copy", ts=DAY,
        entries=copy_posted_lines(source, (await _state(session, co, source))["entries"], reverse=True), metadata_={})
    await session.commit()
    stored = await _state(session, co, je_id)
    assert sorted((e["debit"], e["credit"]) for e in stored["entries"]) == [(0.0, 10.55), (10.55, 0.0)]


async def test_a_movement_posted_before_the_line_rule_reverses_as_it_was_posted(client, session):
    """A movement stored before every foreign line had to carry its foreign amount, and before a
    line in the books' own currency dropped its rate, still reverses: the reversal copies what
    was posted, line for line, and is not judged again as a person's new entry."""
    from celerp_accounting.funds import post_fund_movement

    co = await B.company(client)
    h = co["h"]
    code = (await B.bank(client, h, currency="EUR", name="Euro Bank"))["chart_account_code"]
    ids = await _fund_bank(client, h, code)
    await _given(client, h, code, 500.0, ids["U1"], currency="EUR", rate=1.1)
    plain = (await _state(session, co, await _transfer(client, h, ids, code, 110.0)))["entries"]
    lines = [{**e, "fx_currency": "EUR", **({"fx_debit": 100.0} if e["debit"] else {"fx_credit": 100.0})}
             if e["account"] == code else e for e in plain]
    moved = (await post_fund_movement(
        session, uuid.UUID(co["company_id"]), ts=date.fromisoformat(DAY), reason="Set aside",
        actor=uuid.UUID(co["user_id"]), request_key=uuid.uuid4().hex, kind="fund_transfer", entries=lines))["je_id"]
    await session.commit()
    older = await _stored_before(session, co, moved, lambda e: e if e["account"] == code else e | (
        {"fx_currency": "EUR", "fx_rate": 1.1} if e["debit"] else
        {"fx_currency": "USD", "fx_rate": 2.0, "fx_credit": e["credit"] / 2}))
    reversal = await _state(session, co, await _reverse(client, h, moved))
    keys = ("fx_currency", "fx_rate", "fx_debit", "fx_credit")
    got = [(e["account"], *(e.get(k) for k in keys)) for e in reversal["entries"]]
    want = [(e["account"], e.get("fx_currency"), e.get("fx_rate"), e.get("fx_credit"), e.get("fx_debit")) for e in older]
    assert sorted(got, key=repr) == sorted(want, key=repr)


_SWAP = {"debit": "credit", "credit": "debit", "fx_debit": "fx_credit", "fx_credit": "fx_debit"}


def _amounts(e: dict) -> tuple:
    return (e["account"], e.get("fund_id"), e.get("fx_currency"), *(float(e.get(k) or 0) for k in _SWAP))


def _undone(older: list[dict], reversal: list[dict]) -> None:
    """The reversal holds every older line's amounts, books and foreign, on the other side, so
    the two together leave nothing on any account in either representation."""
    swapped = [_amounts({**e, **{_SWAP[k]: e.get(k) for k in _SWAP}}) for e in older]
    assert sorted(map(_amounts, reversal), key=repr) == sorted(swapped, key=repr)


# An older release kept a foreign amount finer than its currency, or a line that moves nothing.
_OLDER = {"fine": (100.004, False), "zero": (100.0, True)}


@pytest.mark.parametrize("older", sorted(_OLDER))
async def test_a_fund_reversal_of_an_older_movement_undoes_exactly_what_it_posted(client, session, older):
    fx, zero = _OLDER[older]
    co = await B.company(client)
    h = co["h"]
    code = (await B.bank(client, h, currency="EUR", name="Euro Bank"))["chart_account_code"]
    ids = await _fund_bank(client, h, code)
    await _given(client, h, code, 500.0, ids["U1"], currency="EUR", rate=1.1)
    moved = await _transfer(client, h, ids, code, 110.0)
    stored = await _stored_before(session, co, moved, lambda e: e if e["account"] != code else e | {
        "fx_currency": "EUR", "fx_rate": 1.1, ("fx_debit" if e["debit"] else "fx_credit"): fx},
        [{"account": code, "debit": 0.0, "credit": 0.0, "fund_id": ids["U1"]}] if zero else [])
    _undone(stored, (await _state(session, co, await _reverse(client, h, moved)))["entries"])


@pytest.mark.parametrize("older", sorted(_OLDER))
async def test_an_accountant_reversal_of_an_older_entry_undoes_exactly_what_it_posted(client, session, older):
    fx, zero = _OLDER[older]
    co = await B.company(client)
    h = co["h"]
    code = (await B.bank(client, h, currency="EUR", name="Euro Bank"))["chart_account_code"]
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Accrual", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": code, "debit": 100.0, "currency": "EUR", "rate": 1.1},
                    {"account": "4100", "credit": 110.0}]})
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]
    stored = await _stored_before(
        session, co, je_id, lambda e: e | {"fx_debit": fx} if e["account"] == code else e,
        [{"account": "6100", "debit": 0.0, "credit": 0.0}] if zero else [],
        peer_origin={"journal": je_id, "firm": "Firm", "firm_company": str(uuid.uuid4())})
    r = await client.post(f"/accounting/journal-entries/{je_id}/reverse", headers=h)
    assert r.status_code == 200, r.text
    _undone(stored, (await _state(session, co, r.json()["je_id"]))["entries"])


async def test_an_automatic_copy_keeps_a_posted_line_that_moves_nothing(client, session):
    """A line an older release posted with no amount is copied as posted, so the copy's line
    numbers are its source's."""
    from celerp.services.auto_je import _emit_auto_posted_je
    from celerp.services.peer_lines import copy_posted_lines

    co = await B.company(client)
    h = co["h"]
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": DAY, "memo": "Sale", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "6200", "debit": 10.55, "credit": 0.0}, {"account": "4100", "debit": 0.0, "credit": 10.55}]})
    assert r.status_code == 200, r.text
    source = r.json()["je_id"]
    stored = await _stored_before(session, co, source, lambda e: e, [{"account": "2120", "debit": 0.0, "credit": 0.0}])
    je_id = f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=uuid.UUID(co["company_id"]), user_id=None, je_id=je_id, record_id=None,
        idem_create=uuid.uuid4().hex, idem_posted=uuid.uuid4().hex, memo="copy", ts=DAY,
        entries=copy_posted_lines(source, stored, reverse=True), metadata_={})
    await session.commit()
    _undone(stored, (await _state(session, co, je_id))["entries"])
