# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Country contracts: each test country pack loads on a fresh company and its tax codes go
through every posting path with every journal entry balanced and a rebuild from the ledger
equal to the stored projections. Codes are found by category and rate, never by name. A new
country adds its own test here."""
from __future__ import annotations

import pytest

from tax_money_support import codes, code, company, every_path, ok, rebuild_divergence, unbalanced_entries

pytestmark = pytest.mark.asyncio


async def _country(client, session, country: str) -> tuple[dict, dict]:
    from test_l10n_country_update import _apply_country
    auth = await company(session)
    await _apply_country(client, auth, country)
    lists = {side: ok(await client.get(path, headers=auth["headers"])) for side, path in
             (("sales", "/companies/me/taxes"), ("purchase", "/companies/me/purchasing-taxes"))}
    return auth, lists


def _find(rows: list[dict], category: str, rate: float | None = None, point: str | None = None) -> str:
    def current(row):
        return row["versions"][-1]["rate"]
    found = [r for r in rows if r["category"] == category and (rate is None or current(r) == rate)
             and (point is None or r.get("withholding_point") == point)]
    assert found, (category, rate, point, rows)
    return found[0]["id"]


async def _balanced(session, auth):
    assert await unbalanced_entries(session, auth) == []
    assert await rebuild_divergence(session, auth["company_id"]) == []


async def test_contract_zz_ph(client, session):
    auth, lists = await _country(client, session, "zzPH")
    await every_path(client, auth, sale=[_find(lists["sales"], "standard", 12.0)],
                     buy=[_find(lists["purchase"], "standard", 12.0)],
                     sale_wht=_find(lists["sales"], "withholding", point="payment"),
                     buy_wht=_find(lists["purchase"], "withholding", point="payment"))
    await _balanced(session, auth)


async def test_contract_zz_es(client, session):
    """IVA with recargo, reverse charge on purchases, IRPF withheld on the invoice itself,
    IRPF withheld at payment with a credit note, and a cash-basis IVA code, each on every path."""
    auth, lists = await _country(client, session, "zzES")
    iva = _find(lists["sales"], "standard", 21.0)
    iva_in = _find(lists["purchase"], "standard", 21.0)
    recargo = _find(lists["sales"], "standard", 5.2)
    rc = _find(lists["purchase"], "reverse_charge")
    irpf = _find(lists["sales"], "withholding", point="document")
    await every_path(client, auth, sale=[iva, recargo, irpf], buy=[iva_in])
    await every_path(client, auth, sale=[iva], buy=[rc])
    irpf_pay = (await codes(client, auth, [*lists["sales"], code("IRPF at payment", 15, "sales", "withholding",
                                                                 withholding_point="payment")]))["IRPF at payment"]["id"]
    await every_path(client, auth, sale=[iva], buy=[iva_in], sale_wht=irpf_pay)
    sales = ok(await client.get("/companies/me/taxes", headers=auth["headers"]))
    caja = (await codes(client, auth, [*sales, code("IVA caja", 21, "sales", cash_basis=True)]))["IVA caja"]["id"]
    caja_in = (await codes(client, auth, [*lists["purchase"], code("IVA caja", 21, "purchase", cash_basis=True)],
                           "purchase"))["IVA caja"]["id"]
    await every_path(client, auth, sale=[caja], buy=[caja_in])
    await _balanced(session, auth)


async def test_contract_zz_et(client, session):
    auth, lists = await _country(client, session, "zzET")
    await every_path(client, auth,
                     sale=[_find(lists["sales"], "standard", 15.0), _find(lists["sales"], "withholding", 3.0, "document")],
                     buy=[_find(lists["purchase"], "standard", 15.0),
                          _find(lists["purchase"], "withholding", 3.0, "document")])
    await _balanced(session, auth)
