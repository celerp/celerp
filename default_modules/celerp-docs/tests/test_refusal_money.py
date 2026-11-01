# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every amount a docs refusal names is written by the one money formatter, at the document
currency's precision and with its symbol: a KWD tax of 1.125 reads KD 1.125, never 1.12, and a
JPY payment reads ¥5, never 5.00."""
from __future__ import annotations

import ast
import inspect

import pytest

from memo_draw_support import auth, contact, draft, draw, key_of, memo_out, ok, pay, rate, service_line

# currency, conversion rate to the books currency, a tax amount, a payment, how each reads.
_CURRENCIES = {
    "KWD": (3.25, 1.125, 0.375, "KD 1.125", "KD 0.375"),
    "JPY": (0.0067, 35.0, 5.0, "¥35", "¥5"),
}


def _detail(r) -> dict:
    assert r.status_code == 409, r.text
    return r.json()["detail"]


def _refusals(r) -> list[dict]:
    detail = _detail(r)
    return [detail, *detail.get("also", [])]


def _one(r, key: str) -> dict:
    """The refusal ``key`` among those ``r`` gives: its params, with its English message."""
    found = [d for d in _refusals(r) if d.get("message_key") == key]
    assert found, (key, r.text)
    return {**found[0]["params"], "message": found[0]["message"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", list(_CURRENCIES))
async def test_every_refusal_amount_reads_in_the_document_currency(client, session, currency):
    h = await auth(session)
    fx, tax, paid, tax_text, paid_text = _CURRENCIES[currency]
    money = {"currency": currency, "conversion_rate": fx}
    customer = await contact(client, h, f"Store {currency}", currency=currency)

    memo, lids, _ = await memo_out(client, h, [(2, 10.0)], contact_id=customer, tax=tax, **money)
    partial = _one(await draw(client, h, memo, {lids[0]: 1}), "docs.draw_unrated_tax_partial")
    assert partial["amount"] == tax_text and f"a tax amount of {tax_text} with no rate" in partial["message"]

    target = await draft(client, h, contact_id=customer, lines=[service_line()], **money)
    existing = _one(await draw(client, h, memo, {lids[0]: 2}, target=target), "docs.draw_unrated_tax_existing")
    assert existing["amount"] == tax_text and tax_text in existing["message"]

    plain, plain_lids, _ = await memo_out(client, h, [(2, 10.0)], contact_id=customer, **money)
    unrated = await draft(client, h, contact_id=customer, lines=[service_line()], tax=tax, **money)
    terms = _one(await draw(client, h, plain, {plain_lids[0]: 1}, target=unrated), "docs.draw_terms_differ")
    assert f"a tax amount of {tax_text} with no rate" in terms["target_terms"]

    await ok(await pay(client, h, memo, paid))
    for r, key in ((await draw(client, h, memo, {lids[0]: 1}), "docs.draw_memo_has_payment"),
                   (await rate(client, h, memo, 8, dry_run=True), "docs.memo_rate_paid")):
        refused = _one(r, key)
        assert refused["amount"] == paid_text, (key, refused)
        assert refused["payments"] == f"{paid_text} on 2026-03-02", (key, refused)


def test_no_refusal_amount_is_formatted_by_hand():
    """No f-string in the docs routes formats a number at a fixed precision (``:.2f``): an amount
    goes through _money_text, which takes the document currency, so a new refusal cannot skip it."""
    from celerp_docs import routes

    fixed = [node.lineno for node in ast.walk(ast.parse(inspect.getsource(routes)))
             if isinstance(node, ast.FormattedValue) and node.format_spec is not None
             and ast.unparse(node.format_spec).rstrip("'\"").endswith("f")
             and "." in ast.unparse(node.format_spec)]
    assert fixed == [], f"fixed-precision number formatting at routes.py lines {fixed}"
    assert list(inspect.signature(routes._money_text).parameters) == ["amount", "currency"]
