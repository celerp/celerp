# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Seeded hostile text through every bank request field stored in a text column of limited
length (adding and changing a bank, adding and changing a matching rule) and an answer
remembered as a rule: a value is stored as sent when a reference reading says its column can hold it,
otherwise refused naming the field. Never a server error."""

from __future__ import annotations

import random
from typing import Annotated

import pytest
from sqlalchemy import update

import bank_statement_helpers as B
from celerp_accounting.models import BankAccount, BankStatementLine, ReconciliationRule

pytestmark = pytest.mark.asyncio

_THAI = "ก"
_EMOJI = "\U0001F600"


def _limit(column) -> int:
    return column.expression.type.length


def _hostile(rng: random.Random, limit: int) -> str:
    """A value around ``limit`` characters, or one carrying a character a database text column
    cannot hold, or a whitespace, control or format twin of a plain value."""
    kind = rng.choice(["under", "at", "over", "far", "thai_at", "thai_over", "emoji_at", "emoji_over",
                       "nul", "surrogate", "control", "format", "spaces", "nbsp"])
    n = {"under": limit - 1, "at": limit, "over": limit + 1, "far": limit * 4}.get(kind)
    if n is not None:
        return "N" + "z" * (n - 1)
    return {"thai_at": _THAI * limit, "thai_over": _THAI * (limit + 1), "emoji_at": _EMOJI * limit,
            "emoji_over": _EMOJI * (limit + 1), "nul": "AB\x00CD", "surrogate": "AB\ud800CD",
            "control": "AB\x01\x7fCD", "format": "AB‮CD​", "spaces": "  AB  ",
            "nbsp": " AB "}[kind]


def _holds(value: str, limit: int) -> bool:
    """The reference: a text column of ``limit`` characters holds any text of at most that many
    characters that UTF-8 can encode and that has no NUL."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(value) <= limit and "\x00" not in value


def _refused_naming(r, field: str) -> None:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and any(e["loc"][-1] == field for e in detail), detail


_BANK = {"bank_name": BankAccount.bank_name, "account_number": BankAccount.account_number}
_RULE = {"match_pattern": ReconciliationRule.match_pattern, "contact_id": ReconciliationRule.contact_id,
         "default_memo": ReconciliationRule.default_memo}


async def _bank_case(client, co, rng, patch: bool):
    field = rng.choice(sorted(_BANK))
    limit = _limit(_BANK[field])
    value = _hostile(rng, limit)
    body = {"bank_name": "Main", "account_number": "1234", "bank_type": "checking", "currency": "USD"}
    if patch:
        bk = await B.bank(client, co["h"])
        r = await client.patch(f"/accounting/bank-accounts/{bk['id']}", json={field: value}, headers=co["h"])
    else:
        r = await client.post("/accounting/bank-accounts", json={**body, field: value}, headers=co["h"])
    assert r.status_code < 500, r.text
    if not _holds(value, limit):
        return _refused_naming(r, field)
    assert r.status_code == 200, r.text
    bank_id = r.json()["id"]
    assert (await B.bank_row(client, co["h"], bank_id))[field] == value


async def _rule_case(client, co, rng, patch: bool):
    field = rng.choice(sorted(_RULE))
    limit = _limit(_RULE[field])
    value = _hostile(rng, limit)
    body = {"match_pattern": "COFFEE", "target_account_code": "5100"}
    if patch:
        made = await client.post("/accounting/rules", json=body, headers=co["h"])
        assert made.status_code == 200, made.text
        r = await client.patch(f"/accounting/rules/{made.json()['id']}", json={field: value}, headers=co["h"])
    else:
        r = await client.post("/accounting/rules", json={**body, field: value}, headers=co["h"])
    assert r.status_code < 500, r.text
    if not _holds(value, limit):
        return _refused_naming(r, field)
    assert r.status_code == 200, r.text
    assert r.json()[field] == value


async def _remember_case(client, session, co):
    """An answer remembered as a rule on a line imported before lines carried a counterparty:
    its description, longer than a pattern holds, is all the line says."""
    bk = await B.bank(client, co["h"])
    words = " ".join(f"PAYEE{i}" for i in range(80))
    await B.statement(client, co, bk["id"], [("2026-01-05", words, "-5.00")], opening="0.00", closing="-5.00",
                      start="2026-01-01", end="2026-01-31")
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    await session.execute(update(BankStatementLine).where(BankStatementLine.id == line["id"]).values(
        counterparty_key=None, counterparty_account=None))
    await session.commit()
    r = await B.decide_on(client, co["h"], bk["id"], line["id"], "post", account_code="5100", remember=True)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"], r.text
    rules = (await client.get("/accounting/rules", headers=co["h"])).json()["items"]
    [rule] = [x for x in rules if x["origin"] == "answer"]
    assert 0 < len(rule["match_pattern"]) <= _limit(ReconciliationRule.match_pattern)


@pytest.mark.parametrize("seed", range(30))
async def test_hostile_text_in_a_stored_bank_field_is_stored_as_sent_or_refused_naming_it(client, session, seed):
    rng = random.Random(seed)
    co = await B.company(client)
    case = rng.choice(["bank", "bank_patch", "rule", "rule_patch", "remember"])
    if case.startswith("bank"):
        await _bank_case(client, co, rng, case == "bank_patch")
    elif case.startswith("rule"):
        await _rule_case(client, co, rng, case == "rule_patch")
    else:
        await _remember_case(client, session, co)


async def test_every_bank_request_field_stored_in_a_text_column_reads_its_limit_from_the_column():
    """Each request model next to the table it writes: a field named like one of the table's
    text columns of limited length takes that column's limit and refuses what it cannot store."""
    from pydantic import TypeAdapter, ValidationError
    from celerp_accounting.bank_statements import Decision, SplitIn
    from celerp_accounting.routes import BankAccountCreate, BankAccountPatch, ReconRuleCreate, ReconRulePatch

    stores = [(BankAccountCreate, BankAccount), (BankAccountPatch, BankAccount),
              (ReconRuleCreate, ReconciliationRule), (ReconRulePatch, ReconciliationRule),
              (Decision, ReconciliationRule), (SplitIn, ReconciliationRule)]
    checked = 0
    for request, table in stores:
        for name, info in request.model_fields.items():
            column = getattr(table, name, None)
            if column is None or getattr(column.expression.type, "length", None) is None:
                continue
            if "Literal" in str(info.annotation):    # a closed set of words, never free text
                continue
            adapter = TypeAdapter(Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation)
            n = _limit(column)
            assert adapter.validate_python("x" * n) == "x" * n, (request.__name__, name)
            for bad in ("x" * (n + 1), "a\x00", "a\ud800"):
                with pytest.raises(ValidationError):
                    adapter.validate_python(bad)
            checked += 1
    assert checked >= 16
