# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An imported journal is a new posting: each line goes to an active account with nothing
under it, as a manual journal does. A header holds no balance of its own (it sums the
accounts below it) and an inactive account takes nothing new."""
from __future__ import annotations

import uuid

import pytest

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


async def _import(client, session, auth, debit_account: str):
    je = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex,
        "data": {"status": "posted", "ts": "2026-03-01", "memo": "import", "entries": [
            {"account": debit_account, "debit": 50.0, "credit": 0.0},
            {"account": "1111", "debit": 0.0, "credit": 50.0}]}}]})
    assert r.status_code == 200, r.text
    return r.json(), await session.get(Projection, (auth["company_id"], je)) is not None


async def test_an_imported_journal_posts_only_to_active_leaf_accounts(client, session, auth):
    h = auth["headers"]
    for body in ({"code": "6999", "name": "Child", "account_type": "expense", "parent_code": "6000"},
                 {"code": "6998", "name": "Retired", "account_type": "expense", "parent_code": None}):
        assert (await client.post("/accounting/accounts", headers=h, json=body)).status_code == 200
    assert (await client.patch("/accounting/accounts/6998", headers=h, json={"is_active": False})).status_code == 200

    for account, problem in (("6000", "header"), ("6998", "inactive")):
        result, stored = await _import(client, session, auth, account)
        assert result["created"] == 0 and len(result["errors"]) == 1, result
        assert f"Account {account} is" in str(result["errors"][0]), (problem, result)
        assert not stored, account

    result, stored = await _import(client, session, auth, "6999")
    assert result["created"] == 1 and not result["errors"], result
    assert stored


async def _import_lines(client, auth, lines, copies: int = 1, entity_id: str | None = None):
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": entity_id or f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex,
        "data": {"status": "posted", "ts": "2026-03-01", "memo": "import", "entries": [dict(e) for e in lines]}}
        for _ in range(copies)]})
    assert r.status_code == 200, r.text
    return r.json()


async def _keep_books_in(session, company_id, currency: str) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, uuid.UUID(str(company_id)))
    company.settings = {**(company.settings or {}), "currency": currency}
    await session.commit()


async def _trial_balance_balanced(client, auth) -> bool:
    r = await client.get("/accounting/trial-balance", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["balanced"]


async def test_an_imported_journal_that_does_not_balance_is_refused(client, auth):
    result = await _import_lines(client, auth, [{"account": "6100", "debit": 5.0}, {"account": "1111", "credit": 10.0}])
    assert result["created"] == 0 and len(result["errors"]) == 1, result
    assert "doesn't balance" in str(result["errors"][0]) and "Add 5.00 to the debit side" in str(result["errors"][0]), result
    assert await _trial_balance_balanced(client, auth)


# An entry has two lines at least, each a positive amount on one side: the same side in the books'
# currency and in any it was typed in, and an amount still there once kept at its currency's unit.
_ONE_SIDED_POSITIVE = {
    "at least two lines": [[], [{"account": "6100", "debit": 10.0}]],
    "has no amount": [
        [{"account": "6100", "debit": 10.0}, {"account": "1111", "credit": 10.0}, {"account": "6200", "debit": 0.0}],
        [{"account": "6100", "debit": 10.004}, {"account": "1111", "credit": 10.0}, {"account": "6200", "credit": 0.004}],
    ],
    "can't be negative": [
        [{"account": "6100", "debit": -10.0}, {"account": "1111", "credit": -10.0}],
        [{"account": "6100", "debit": 10.0, "credit": -10.0}, {"account": "1111", "credit": 0.0}],
        [{"account": "6100", "debit": 10.0, "fx_debit": -1500.0, "fx_currency": "JPY", "fx_rate": 0.0067},
         {"account": "1111", "credit": 10.0}],
    ],
    "not both": [
        [{"account": "6100", "debit": 10.0, "credit": 5.0}, {"account": "1111", "credit": 5.0}],
        [{"account": "6100", "debit": 10.0, "fx_debit": 1500.0, "fx_credit": 1.0, "fx_currency": "JPY",
          "fx_rate": 0.0067}, {"account": "1111", "credit": 10.0}],
        [{"account": "6100", "debit": 10.0, "fx_credit": 1500.0, "fx_currency": "JPY", "fx_rate": 0.0067},
         {"account": "1111", "credit": 10.0}],
    ],
}


@pytest.mark.parametrize("needle, lines", [(n, lines) for n, cases in _ONE_SIDED_POSITIVE.items() for lines in cases])
async def test_an_imported_line_that_is_negative_or_on_both_sides_is_refused(client, auth, needle, lines):
    result = await _import_lines(client, auth, lines)
    assert result["created"] == 0 and needle in str(result["errors"]), result
    assert await _trial_balance_balanced(client, auth)


async def test_the_journal_boundary_refuses_a_line_with_no_account_or_an_unbalanced_entry(session, auth):
    """The rules live at the boundary every journal writer passes, not in a route."""
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    for lines, needle in (([{"debit": 10.0}, {"account": "1111", "credit": 10.0}], "is not in the chart"),
                          ([{"account": "6100", "debit": 10.0}, {"account": "1111", "credit": 9.0}], "doesn't balance"),
                          *((lines, needle) for needle, cases in _ONE_SIDED_POSITIVE.items() for lines in cases)):
        with pytest.raises(HTTPException) as exc:
            await emit_event(
                session, company_id=uuid.UUID(str(auth["company_id"])), entity_id=f"je:{uuid.uuid4()}",
                entity_type="journal_entry", event_type="acc.journal_entry.created",
                data={"status": "posted", "ts": "2026-03-01", "entries": lines},
                actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex, metadata_={})
        await session.rollback()
        assert exc.value.status_code == 422 and needle in str(exc.value.detail), exc.value.detail


@pytest.mark.parametrize("currency", ["ZZZ", "usd", "978", "<script>"])
async def test_an_imported_line_in_a_currency_celerp_does_not_recognise_is_refused(client, auth, currency):
    result = await _import_lines(client, auth, [
        {"account": "6100", "debit": 10.0, "fx_debit": 3.0, "fx_currency": currency, "fx_rate": 3.3},
        {"account": "1111", "credit": 10.0}])
    assert result["created"] == 0 and len(result["errors"]) == 1, result
    assert result["errors"][0]["params"]["refusal"]["message_key"] == "error.invalid_currency", result


async def test_an_imported_line_in_a_withdrawn_currency_is_stored(client, auth):
    result = await _import_lines(client, auth, [
        {"account": "6100", "debit": 9.95, "fx_debit": 19.5, "fx_currency": "BGN", "fx_rate": 0.51},
        {"account": "1111", "credit": 9.95}])
    assert result["created"] == 1 and not result["errors"], result


@pytest.mark.parametrize("books, lines", [
    ("USD", [{"account": "6100", "debit": 10.004}, {"account": "6200", "credit": 10.0}]),
    ("JPY", [{"account": "6100", "debit": 100.49}, {"account": "6200", "credit": 100.0}]),
    # Balanced as typed, but not once each line is kept at cents: 10.01 against 5.00 + 5.00.
    ("USD", [{"account": "6100", "debit": 10.005}, {"account": "6200", "credit": 5.0025},
             {"account": "6300", "credit": 5.0025}]),
])
async def test_an_imported_journal_off_by_less_than_the_books_smallest_unit_is_refused(client, session, auth, books, lines):
    """300 entries each off by a fraction of a cent would leave the trial balance off by their
    sum, so each is refused, however small its gap. (Expense accounts only: a bank keeps the
    currency it was opened in, so a books-currency switch would leave it mislabelled.)"""
    await _keep_books_in(session, auth["company_id"], books)
    result = await _import_lines(client, auth, lines, copies=300)
    assert result["created"] == 0 and result["errors"], result
    assert "doesn't balance" in str(result["errors"][0]), result
    assert await _trial_balance_balanced(client, auth)


async def test_an_imported_journal_is_stored_at_the_books_precision(client, session, auth):
    je = f"je:{uuid.uuid4()}"
    result = await _import_lines(client, auth, [{"account": "6100", "debit": 10.004}, {"account": "1111", "credit": 10.004}],
                                 entity_id=je)
    assert result["created"] == 1 and not result["errors"], result
    stored = (await session.get(Projection, (auth["company_id"], je))).state["entries"]
    assert [(e.get("debit"), e.get("credit")) for e in stored] == [(10.0, None), (None, 10.0)], stored


async def test_a_foreign_amount_too_small_for_the_books_is_kept_with_its_books_side_empty(client, session, auth):
    je = f"je:{uuid.uuid4()}"
    lines = [{"account": "6100", "debit": 0.0001, "fx_debit": 1.0, "fx_currency": "JPY", "fx_rate": 0.0001},
             {"account": "6200", "credit": 0.0001, "fx_credit": 1.0, "fx_currency": "JPY", "fx_rate": 0.0001}]
    result = await _import_lines(client, auth, lines, entity_id=je)
    assert result["created"] == 1 and not result["errors"], result
    stored = (await session.get(Projection, (auth["company_id"], je))).state["entries"]
    assert [(e.get("debit"), e.get("fx_debit"), e.get("credit"), e.get("fx_credit")) for e in stored] == [
        (0.0, 1.0, None, None), (None, None, 0.0, 1.0)], stored


async def test_a_reversal_copies_both_currencies_onto_the_other_side(client, session, auth):
    """A copy of posted lines (copy_posted_lines, reverse) swaps the books' and the foreign amount
    together, so the reversal keeps one meaning per line and passes the boundary."""
    from celerp.events.engine import emit_event
    from celerp.services.peer_lines import copy_posted_lines

    je = f"je:{uuid.uuid4()}"
    lines = [{"account": "6100", "debit": 10.05, "fx_debit": 1500.0, "fx_currency": "JPY", "fx_rate": 0.0067},
             {"account": "6200", "credit": 10.05}]
    assert (await _import_lines(client, auth, lines, entity_id=je))["created"] == 1
    posted = (await session.get(Projection, (auth["company_id"], je))).state["entries"]
    reversal = f"{je}:reversal"
    await emit_event(
        session, company_id=uuid.UUID(str(auth["company_id"])), entity_id=reversal, entity_type="journal_entry",
        event_type="acc.journal_entry.created", keep_gifts=True,
        data={"status": "posted", "ts": "2026-03-02", "entries": copy_posted_lines(je, posted, reverse=True)},
        actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex, metadata_={})
    await session.commit()
    stored = (await session.get(Projection, (auth["company_id"], reversal))).state["entries"]
    assert [(e["account"], e.get("credit"), e.get("fx_credit"), e.get("debit"), e.get("fx_debit")) for e in stored] == [
        ("6100", 10.05, 1500.0, None, None), ("6200", None, None, 10.05, None)], stored


# A line in the books' own currency (USD here) and a line in another one mean the same
# whichever door they come through: (typed line, the line an import carries, refusal key).
_LINE_REFUSALS = [
    ({"debit": 10.0, "currency": "USD", "rate": 1.5},
     {"debit": 10.0, "fx_debit": 10.0, "fx_currency": "USD", "fx_rate": 1.5}, "acct.err_fx_base_rate"),
    ({"debit": 10.0, "currency": "EUR"}, {"fx_debit": 10.0, "fx_currency": "EUR"}, "acct.err_fx_no_rate"),
    ({"debit": 10.0, "rate": 2.0}, {"debit": 20.0, "fx_debit": 10.0, "fx_rate": 2.0}, "acct.err_fx_rate_no_currency"),
    ({"debit": 10.0, "currency": "EUR", "rate": 0.0},
     {"debit": 0.0, "fx_debit": 10.0, "fx_currency": "EUR", "fx_rate": 0.0}, "acct.err_fx_rate_invalid"),
    ({"debit": 10.0, "currency": "QQQ", "rate": 2.0},
     {"debit": 20.0, "fx_debit": 10.0, "fx_currency": "QQQ", "fx_rate": 2.0}, "error.invalid_currency"),
]


@pytest.mark.parametrize("typed, imported, key", _LINE_REFUSALS)
async def test_a_typed_line_and_an_imported_one_are_refused_with_the_same_message(
        client, session, auth, typed, imported, key):
    await _keep_books_in(session, auth["company_id"], "USD")
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-01", "memo": "typed", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "6100", **typed}, {"account": "1111", "credit": 10.0}]})
    assert r.status_code == 422, r.text
    result = await _import_lines(client, auth, [{"account": "6100", **imported}, {"account": "1111", "credit": 10.0}])
    assert result["created"] == 0, result
    refused = result["errors"][0]["params"]["refusal"]
    assert r.json()["detail"]["message_key"] == refused["message_key"] == key, (r.text, result)
    assert r.json()["detail"]["message"] == refused["message"], (r.text, result)


@pytest.mark.parametrize("line, key", [
    ({"debit": 10.0, "fx_debit": 9.0, "fx_currency": "USD", "fx_rate": 1.0}, "acct.err_fx_base_rate"),
    ({"debit": 10.0, "fx_credit": 10.0, "fx_currency": "USD"}, "acct.err_je_both_sides"),
    ({"debit": 10.0, "fx_currency": "EUR", "fx_rate": 1.1}, "acct.err_fx_no_amount"),
    ({"fx_debit": 10.0, "fx_currency": "EUR", "fx_rate": 1.1}, "acct.err_fx_no_books_amount"),
])
async def test_an_imported_line_whose_currency_fields_say_something_else_is_refused(client, session, auth, line, key):
    await _keep_books_in(session, auth["company_id"], "USD")
    result = await _import_lines(client, auth, [{"account": "6100", **line}, {"account": "1111", "credit": 10.0}])
    assert result["created"] == 0 and result["errors"][0]["params"]["refusal"]["message_key"] == key, result


async def test_an_imported_line_repeating_the_books_currency_is_stored_as_a_books_currency_line(client, session, auth):
    await _keep_books_in(session, auth["company_id"], "USD")
    je = f"je:{uuid.uuid4()}"
    result = await _import_lines(client, auth, [
        {"account": "6100", "debit": 10.0, "fx_debit": 10.0, "fx_currency": "USD", "fx_rate": 1.0},
        {"account": "1111", "credit": 10.0}], entity_id=je)
    assert result["created"] == 1, result
    stored = (await session.get(Projection, (auth["company_id"], je))).state["entries"]
    assert not any(key.startswith("fx_") for e in stored for key in e), stored


async def test_a_bank_reads_a_stored_line_naming_the_books_currency_as_a_books_currency_line():
    """A line stored before the line rule may still name the books' own currency with its
    amounts repeated; every bank reader takes it as the boundary does: a books-currency line."""
    from celerp_accounting.routes import _bank_amounts, _carries_foreign_amount, _moves_on_bank

    older = {"account": "1111", "debit": 10.0, "fx_debit": 10.0, "fx_currency": "usd", "fx_rate": 1.0}
    assert _bank_amounts(older, "USD", "USD") == (10, 0)
    assert _bank_amounts(older, "THB", "USD") is None
    assert not _carries_foreign_amount(older, "USD")
    assert _carries_foreign_amount(older, "THB")
    back = {"account": "1111", "credit": 10.0}
    assert _moves_on_bank([older, back], lambda line: None, "USD") == []


async def test_an_imported_line_rate_is_stored_as_every_door_stores_a_rate(client, session, auth):
    """A rate given with more places than a rate keeps is stored rounded, as a typed one is."""
    from celerp.services.money import round_exchange_rate

    await _keep_books_in(session, auth["company_id"], "USD")
    je, rate = f"je:{uuid.uuid4()}", 1.123456789012345
    result = await _import_lines(client, auth, [
        {"account": "6100", "debit": 11.23, "fx_debit": 10.0, "fx_currency": "EUR", "fx_rate": rate},
        {"account": "1111", "credit": 11.23}], entity_id=je)
    assert result["created"] == 1, result
    stored = (await session.get(Projection, (auth["company_id"], je))).state["entries"]
    assert stored[0]["fx_rate"] == float(round_exchange_rate(rate)) != rate, stored


async def _post_at_boundary(session, auth, lines):
    """Post *lines* through the journal boundary itself, as any writer could; the stored lines or the refusal."""
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    await _keep_books_in(session, auth["company_id"], "USD")
    je = f"je:{uuid.uuid4()}"
    try:
        await emit_event(
            session, company_id=uuid.UUID(str(auth["company_id"])), entity_id=je,
            entity_type="journal_entry", event_type="acc.journal_entry.created",
            data={"status": "posted", "ts": "2026-03-01", "entries": [dict(e) for e in lines]},
            actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex, metadata_={})
        await session.commit()
    except HTTPException as exc:
        await session.rollback()
        return exc.detail
    return (await session.get(Projection, (auth["company_id"], je))).state["entries"]


def _line(books, fx, rate, currency="EUR"):
    return {"account": "6100", "debit": books, "fx_debit": fx, "fx_currency": currency,
            **({"fx_rate": rate} if rate is not None else {})}


# Shapes our own writers post: a give-back piece, split_to_base parts, a transfer split over three
# funds, a fee converted at the paid rate, and our own bundle round trip on large VND amounts.
_OWN_WRITER_SHAPES = {
    "give-back piece": [_line(0.05, 0.05, 1.123456)],
    "split parts": [_line(b, 0.05, 1.123456) for b in (0.06, 0.05, 0.06)],
    "three-fund transfer": [_line(1111.11, f, 33.3333) for f in (33.34, 33.33, 33.33)],
    "fee at the paid rate": [_line(12.34, 0.35, 35.5)],
    "bundle VND 1e11": [_line(4000000.05, 1e11, 0.000040000001, "VND")],
    "bundle VND 3.14e12": [_line(123456789.37, 3.14e12, 0.000039317449, "VND")],
}


@pytest.mark.parametrize("name", list(_OWN_WRITER_SHAPES))
async def test_a_foreign_line_our_own_writers_post_is_accepted(session, auth, name):
    lines = _OWN_WRITER_SHAPES[name]
    total = round(sum(ln["debit"] for ln in lines), 2)
    stored = await _post_at_boundary(session, auth, [*lines, {"account": "1111", "credit": total}])
    assert isinstance(stored, list), stored
    assert [(e["debit"], e["fx_rate"]) for e in stored[:-1]] == [(ln["debit"], ln["fx_rate"]) for ln in lines], stored


@pytest.mark.parametrize("books, fx, rate, gap", [
    (0.01, 100.0, 1.1, "109.99"),
    (1103540.0, 1000000.0, 1.1035, "40.00"),
])
async def test_a_foreign_line_whose_books_amount_is_not_its_amount_at_its_rate_is_refused(session, auth, books, fx, rate, gap):
    detail = await _post_at_boundary(session, auth, [_line(books, fx, rate), {"account": "1111", "credit": books}])
    assert isinstance(detail, dict) and detail["message_key"] == "acct.err_fx_books_disagree", detail
    assert f"a gap of {gap} USD" in detail["message"] and "leave the rate out" in detail["message"], detail


async def test_a_foreign_line_with_books_and_no_rate_stores_the_rate_its_amounts_imply(session, auth):
    stored = await _post_at_boundary(session, auth, [_line(1103540.0, 1000000.0, None), {"account": "1111", "credit": 1103540.0}])
    assert isinstance(stored, list) and stored[0]["fx_rate"] == 1.10354 and stored[0]["debit"] == 1103540.0, stored


# The bound for JPY 2 at rate R in USD books is 0.01 + R x 1 + 2 x 1e-12 / 2. At R = 0.003333333333 it is
# 0.013333333334, and books 0.02 are exactly that far from 2 x R: accepted. One books unit more, or one
# unit less in the rate's last place, is past it.
@pytest.mark.parametrize("books, rate, accepted", [
    (0.02, 0.003333333333, True),
    (0.03, 0.003333333333, False),
    (0.02, 0.003333333332, False),
])
async def test_the_bound_is_exact_at_its_edge(session, auth, books, rate, accepted):
    got = await _post_at_boundary(session, auth, [_line(books, 2.0, rate, "JPY"), {"account": "1111", "credit": books}])
    assert isinstance(got, list) is accepted, got


async def _typed_and_imported(client, session, auth, fx, rate, books):
    """The same EUR line typed and imported: each door's stored line, or its refusal message."""
    await _keep_books_in(session, auth["company_id"], "USD")
    typed = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-01", "memo": "typed", "idempotency_token": uuid.uuid4().hex, "entries": [
            {"account": "6100", "debit": fx, "currency": "EUR", "rate": rate},
            {"account": "1111", "credit": books}]})
    je = f"je:{uuid.uuid4()}"
    result = await _import_lines(client, auth, [
        {"account": "6100", "debit": books, "fx_debit": fx, "fx_currency": "EUR", "fx_rate": rate},
        {"account": "1111", "credit": books}], entity_id=je)
    if typed.status_code != 200:
        return typed.json()["detail"]["message"], str(result["errors"])
    typed_je = typed.json()["je_id"]
    money = ("account", "debit", "credit", "fx_debit", "fx_credit", "fx_currency", "fx_rate")
    return [{k: line.get(k) or None for k in money} for line in  # an empty side: 0 typed, absent imported
            [(await session.get(Projection, (auth["company_id"], e))).state["entries"][0] for e in (typed_je, je)]]


async def test_a_typed_line_is_kept_at_its_currency_places_before_it_converts_as_an_imported_one_is(client, session, auth):
    """EUR 10.0049 is kept as EUR 10.00, so typed it books 11.00 at 1.1, the line an importer sending
    those figures stores."""
    typed, imported = await _typed_and_imported(client, session, auth, 10.0049, 1.1, 11.0)
    assert typed == imported and typed["debit"] == 11.0 and typed["fx_debit"] == 10.0, (typed, imported)


async def test_a_typed_and_an_imported_line_meet_the_same_refusal(client, session, auth):
    typed, imported = await _typed_and_imported(client, session, auth, 0.004, 1.1, 0.0)
    assert "EUR" in typed and typed in imported, (typed, imported)
