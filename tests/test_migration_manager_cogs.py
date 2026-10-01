# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager's cost of inventory sold is cost of goods sold in the moved books.

Manager books the cost of every inventory item sold to its built-in "Inventory - cost"
account. In Celerp that is cost of goods sold, so the profit and loss statement shows
it above gross profit and never among operating expenses."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions
from celerp.importers.schema import AccountType, CIFMode
from fixtures.manager_io import specs
from fixtures.manager_io.support import adapter, artifact, ref
from migration_support import real_client, real_engine  # noqa: F401 - fixtures
from test_migration_inventory_provenance import INVENTORY, MODES, _migrated

COST = ref("@ProfitAndLossStatementAccountInventoryPurchases")
SALES = ref("@ProfitAndLossStatementAccountInventorySales")


def _decisions(decisions: dict) -> MigrationDecisions:
    if decisions["mode"] == "cutover":
        return MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=specs.LIFECYCLE_CUTOVER)
    return MigrationDecisions(mode=CIFMode.FULL_HISTORY)


@pytest.mark.parametrize("decisions", MODES)
def test_inventory_cost_account_is_cost_of_goods_sold(decisions):
    """RED before the change: the account is mapped as a generic expense."""
    manifest = adapter().build_manifest([artifact(INVENTORY)], _decisions(decisions))
    accounts = {a.source_external_id: a for a in manifest.bundle.accounts}
    assert accounts[COST].account_type == AccountType.COGS
    assert accounts[SALES].account_type == AccountType.REVENUE


@pytest.mark.parametrize("decisions", MODES)
async def test_migrated_profit_and_loss_reports_inventory_cost_as_cogs(real_engine, real_client, monkeypatch,
                                                                         tmp_path, decisions):
    """RED before the change: the cost of sales is listed among operating expenses and
    gross profit equals sales."""
    books = await _migrated(real_engine, monkeypatch, tmp_path, decisions)
    cost_code = {m: t for (_, m), t in books.maps.items()}[COST]
    pnl = (await real_client.get("/accounting/pnl", headers=books.headers)).json()
    cogs = {line["code"]: D(str(line["amount"])) for line in pnl["cogs"]["lines"]}
    expenses = {line["code"] for line in pnl["expenses"]["lines"]}
    revenue = D(str(pnl["revenue"]["total"]))
    assert cost_code in cogs and cogs[cost_code] > 0
    assert cost_code not in expenses
    assert D(str(pnl["gross_profit"])) == revenue - D(str(pnl["cogs"]["total"]))
    assert D(str(pnl["net_profit"])) == D(str(pnl["gross_profit"])) - D(str(pnl["expenses"]["total"]))

    trial = (await real_client.get("/accounting/trial-balance", headers=books.headers)).json()
    assert trial["balanced"] is True
    by_code = {line["code"]: line for line in trial["lines"]}
    assert by_code[cost_code]["account_type"] == "cogs"
    profit_accounts = [line for line in trial["lines"] if line["account_type"] in ("revenue", "cogs", "expense")]
    # Net profit is the same figure the ledger holds, whichever section a line is in.
    assert D(str(pnl["net_profit"])).quantize(D("0.01")) == \
        -sum(D(str(line["net"])) for line in profit_accounts).quantize(D("0.01"))
    sheet = (await real_client.get("/accounting/balance-sheet", headers=books.headers)).json()
    assert sheet["balanced"] is True
