# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The balance sheet follows the chart's tree to any depth.

An imported chart can nest accounts several levels deep, and an account that
later gained sub-accounts keeps what was posted to it directly. Each header shows
the total of everything under it, its own postings appear once under it, and
every account's balance counts exactly once in the section total.
"""
from __future__ import annotations

import csv
import io
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

pytestmark = pytest.mark.asyncio


async def _account(client, auth, code: str, account_type: str, parent: str | None) -> None:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent})
    assert r.status_code == 200, r.text


async def _post(client, auth, debit: str, credit: str, amount: float) -> None:
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-01-15", "memo": "Opening", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": debit, "debit": amount}, {"account": credit, "credit": amount}]})
    assert r.status_code == 200, r.text


async def _chain(client, auth, account_type: str, codes: list[str], amounts: list[float], post) -> None:
    """Each account in ``codes`` sits under the one before it and is posted to
    while it still has nothing under it."""
    parent = None
    for code, amount in zip(codes, amounts):
        await _account(client, auth, code, account_type, parent)
        await post(code, amount)
        parent = code


def _shape(lines: list[dict]) -> list[tuple]:
    return [(l["code"], l["depth"], l["amount"], bool(l.get("is_parent"))) for l in lines]


async def _four_level_sheet(client, auth) -> dict:
    """Four-level asset and liability trees, each level posted to before it gained a
    sub-account, and the balance sheet they make."""
    async def debit_asset(code, amount):
        await _post(client, auth, code, "3100", amount)

    async def credit_liability(code, amount):
        await _post(client, auth, "6200", code, amount)

    await _chain(client, auth, "asset", ["1900", "1910", "1911", "1912"], [1000.0, 200.0, 300.0, 40.0],
                 debit_asset)
    await _account(client, auth, "1913", "asset", "1910")
    await debit_asset("1913", 5.0)
    await _chain(client, auth, "liability", ["2900", "2910", "2911", "2912"], [10.0, 20.0, 30.0, 40.0],
                 credit_liability)

    r = await client.get("/accounting/balance-sheet", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_four_level_tree_counts_every_balance_once(client, auth):
    sheet = await _four_level_sheet(client, auth)
    assert (sheet["assets"]["total"], sheet["liabilities"]["total"]) == (1545.0, 100.0)
    # Every literal account balance appears on exactly one line that is not a header total.
    for key in ("assets", "liabilities"):
        literal = [l for l in sheet[key]["lines"] if not l.get("is_parent")]
        assert len({l["code"] for l in literal}) == len(literal)
        assert round(sum(l["amount"] for l in literal), 2) == sheet[key]["total"]
    assert sheet["balanced"] is True

    assert _shape(sheet["assets"]["lines"]) == [
        ("1900", 0, 1545.0, True),
        ("1900", 1, 1000.0, False),
        ("1910", 1, 545.0, True),
        ("1910", 2, 200.0, False),
        ("1911", 2, 340.0, True),
        ("1911", 3, 300.0, False),
        ("1912", 3, 40.0, False),
        ("1913", 2, 5.0, False),
    ]
    assert _shape(sheet["liabilities"]["lines"]) == [
        ("2900", 0, 100.0, True),
        ("2900", 1, 10.0, False),
        ("2910", 1, 90.0, True),
        ("2910", 2, 20.0, False),
        ("2911", 2, 70.0, True),
        ("2911", 3, 30.0, False),
        ("2912", 3, 40.0, False),
    ]


async def test_the_exported_sheet_marks_subtotals_so_the_balances_add_up(client, auth, ui_client):
    """In the CSV every line says whether it is an account's balance or the subtotal of
    a header, and how deep it sits. Adding up the balance lines of a section gives the
    section total; the subtotals only restate them."""
    sheet = await _four_level_sheet(client, auth)
    with patch("ui.api_client.get_balance_sheet", new=AsyncMock(return_value=sheet)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "USD"})):
        r = await ui_client.get("/reports/export/balance-sheet/csv?as_of=2026-12-31", cookies=_authed())
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("\ufeff"))))
    assert list(rows[0]) == ["Section", "Level", "Line", "Code", "Account", "Amount"]

    for key, section in (("assets", "Assets"), ("liabilities", "Liabilities")):
        lines = [row for row in rows if row["Section"] == section]
        [total] = [row for row in lines if row["Line"] == "Total"]
        balances = [row for row in lines if row["Line"] == "Balance"]
        subtotals = [row for row in lines if row["Line"] == "Subtotal"]
        assert len(balances) + len(subtotals) + 1 == len(lines)
        assert round(sum(float(row["Amount"]) for row in balances), 2) == float(total["Amount"]) \
            == sheet[key]["total"]
        assert [(row["Code"], int(row["Level"]), float(row["Amount"]), row["Line"] == "Subtotal")
                for row in lines if row["Line"] != "Total"] == _shape(sheet[key]["lines"])


@pytest.mark.parametrize("currency,expected", [("USD", ["1545.00", "1000.00", "5.00"]), ("JPY", ["1545", "1000", "5"])])
async def test_the_exported_amounts_carry_the_currencys_decimal_places(client, auth, ui_client, currency, expected):
    """Every amount in the CSV is written at the company currency's decimal places
    (1545.00 in dollars, 1545 in yen), never as a raw float such as 1545.0, and with
    no symbol or digit grouping so a spreadsheet still reads it as a number."""
    sheet = await _four_level_sheet(client, auth)
    with patch("ui.api_client.get_balance_sheet", new=AsyncMock(return_value=sheet)), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": currency})):
        r = await ui_client.get("/reports/export/balance-sheet/csv?as_of=2026-12-31", cookies=_authed())
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assets = [row["Amount"] for row in rows if row["Section"] == "Assets" and row["Line"] != "Total"]
    assert [assets[0], assets[1], assets[-1]] == expected


async def test_income_not_yet_closed_is_named_apart_from_the_retained_earnings_account(client, auth):
    """A company with a closed year in its retained earnings account (3200) and income
    since then shows two equity lines with two different names: "Retained Earnings"
    once, for the account, and the income not yet closed under its own name, on the
    API and on the rendered sheet."""
    from fasthtml.common import to_xml
    from ui.routes.financial_reports import _balance_sheet_view

    await _account(client, auth, "1900", "asset", None)
    await _post(client, auth, "1900", "3200", 100.0)   # a year already closed into 3200
    await _post(client, auth, "1900", "4100", 40.0)    # income since the close
    r = await client.get("/accounting/balance-sheet", headers=auth["headers"])
    assert r.status_code == 200, r.text
    sheet = r.json()
    names = [l["name"] for l in sheet["equity"]["lines"]]
    assert names.count("Retained Earnings") == 1, names
    [unclosed] = [l for l in sheet["equity"]["lines"] if l.get("synthetic")]
    assert (unclosed["name"], unclosed["amount"]) == ("Net income not yet closed", 40.0)

    html = to_xml(_balance_sheet_view(sheet, "USD", as_of="2026-12-31"))
    assert html.count("Retained Earnings") == 1
    assert "Net income not yet closed" in html
