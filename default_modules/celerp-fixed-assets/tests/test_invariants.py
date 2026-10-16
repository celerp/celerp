# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Invariants that hold after every operation: registered cost equals the source lines
that carry it, net book value is cost less both reductions, every reversal mirrors what
it reverses, and the tax books never touch the ledger."""
from __future__ import annotations

from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_corrections import _bill, _correct, _void
from test_register import _nbv_holds
from test_runs import _contributed, _managed_from, _reverse, _run

pytestmark = pytest.mark.asyncio

D = Decimal
MONEY = ("cost", "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus")


async def _sources_carry_cost(fa: FA) -> None:
    """The signed amounts of the claimed source lines equal the cost of the assets they carry."""
    claimed = sum((-1 if c.get("kind") == "reduction" else 1) * D(str(c["amount"]))
                  for c in await fa.entities("fa_src") if c.get("status") == "claimed")
    carried = sum(D(a["cost"]) for a in await _assets(fa) if a.get("sources"))
    assert claimed == carried, (claimed, carried)


async def _assets(fa: FA) -> list[dict]:
    return (await fa.ok(await fa.get("/assets")))["items"]


async def test_invariant_source_cost_equals_registered_cost(fa):
    """Source cost equals registered cost after registration, a void and Replace source,
    and Link additional source; the class accounts hold what the register holds."""
    await _managed_from(fa, "2025-01-01")
    for n, (description, cls) in enumerate((("Delivery van", "Vehicles"), ("Laptop", "Computer equipment"),
                                            ("Office fit-out", "Assets under construction"),
                                            ("Forklift", "Vehicles"))):
        line = await fa.line(description)
        done = await fa.register(line["source"], key=f"inv-{n}", class_name=cls, mode="separate")
        costs = [D((await fa.asset(a["id"]))["cost"]) for a in done["assets"]]
        assert sum(costs) == D(line["amount"])
    await _sources_carry_cost(fa)
    doc = await _bill(fa, "Pickup truck", "12000", "2026-01-10")
    old = await fa.line("Pickup truck")
    truck = (await fa.register(old["source"], key="truck"))["assets"][0]["id"]
    await _void(fa, doc)
    await _bill(fa, "Pickup truck", "12100", "2026-01-10")
    new = await fa.line("Pickup truck")
    await _correct(fa, truck, {"replace_source": {"old": old["source"], "new": new["source"]}},
                   posting_date="2026-04-20")
    await _sources_carry_cost(fa)
    je = await fa.journal("2026-09-20", [{"account": "1210", "debit": 800}, {"account": "1111", "credit": 800}])
    await _correct(fa, truck, {"link_source": {"source": f"{je}:0"}})
    credit = await fa.journal("2026-09-21", [{"account": "1111", "debit": 300}, {"account": "1210", "credit": 300}])
    await _correct(fa, truck, {"link_source": {"source": f"{credit}:1"}})
    assert D((await fa.asset(truck))["cost"]) == D("12600")
    await _sources_carry_cost(fa)
    register = {"1210": D(0), "1240": D(0)}
    classes = {c["id"]: c for c in (await fa.classes()).values()}
    for a in await _assets(fa):
        register[classes[a["class_id"]]["asset_account"]] += D(a["cost"])
        await fa.fold_matches(a["id"])
    assert register == {"1210": await fa.balance("1210"), "1240": await fa.balance("1240")}


async def test_invariant_nbv_identity(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    await fa.operate(f"/assets/{car}/impair", {"date": "2026-03-31", "recoverable_amount": "9000",
                                               "reason": "Damage"}, key="imp")
    asset = await fa.asset(car)
    assert D(asset["accumulated_impairment"]) > 0 and D(asset["accumulated_depreciation"]) > 0
    for a in await _assets(fa):
        _nbv_holds(a)
        await fa.fold_matches(a["id"])


async def test_invariant_reversal_exact_opposite(fa):
    await _managed_from(fa)
    van = await fa.line("Delivery van")
    await fa.register(van["source"], key="reg")
    await fa.operate("/sources/undo", {"source": van["source"]}, key="unreg")
    car = await _contributed(fa)
    await _run(fa, "2026-02")
    await _reverse(fa, "2026-02")
    await _run(fa, "2026-03")
    await fa.operate(f"/assets/{car}/dispose", {"date": "2026-04-10", "reason": "scrapped"},
                     key="disp")
    await fa.operate(f"/assets/{car}/undo-disposal", {}, key="undisp")
    movements = {m["entity_id"]: m for m in await fa.entities("fa_movement")}
    reversals = [m for m in movements.values() if m.get("reverses")]
    assert {movements[r["reverses"]]["operation"] for r in reversals} >= {"registration", "run", "disposal"}
    for r in reversals:
        original = movements[r["reverses"]]
        assert r["asset_id"] == original["asset_id"]
        for bucket in MONEY:
            assert D(str((r["amounts"] or {}).get(bucket) or 0)) == -D(str((original["amounts"] or {}).get(bucket) or 0))
        assert D(str(r.get("quantity") or 0)) == -D(str(original.get("quantity") or 0))
        assert sorted((c.get("account"), -D(str(c["amount"]))) for c in r.get("contributions") or []) == sorted(
            (c.get("account"), D(str(c["amount"]))) for c in original.get("contributions") or [])
    # Each reversed journal is voided in place with its lines intact, so the ledger nets it out exactly.
    journals = await fa.module_journals()
    voided = [je for je in journals if je["status"] == "void"]
    assert voided
    for je in voided:
        assert sum(D(str(e.get("debit") or 0)) for e in je["entries"]) == sum(
            D(str(e.get("credit") or 0)) for e in je["entries"]) > 0


async def test_invariant_tax_books_never_post(fa, zz):
    from test_l10n import test_l10n_tax_books_never_post

    await test_l10n_tax_books_never_post(fa, zz)
