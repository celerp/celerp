# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Inventory Celerp cannot carry yet stops a Manager migration at the scan: stock that runs
below zero. It is a blocker naming the record type and how many there are, refused on both
modes before any company or run exists, and never a warning the user can click past.
Further inventory locations are carried, the stock held at them arriving at the default
location as a listed loss, and transfers between locations are listed as not carried."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from fixtures.manager_io import specs
from migration_support import migration_env, save_decisions, scan_upload  # noqa: F401 - fixture

LOCATED = "PurchaseInvoice (location)"
NEGATIVE = "DeliveryNote (negative stock)"
MODES = (("full_history", None), ("cutover", "2026-01-05"))          # the first record's date


async def _scan(client, tmp_path, build=specs.build_inventory_safety, **kinds) -> tuple[str, dict]:
    path = build(tmp_path / "safety.manager", **kinds)
    r = await scan_upload(client, path.read_bytes(), name="safety.manager")
    assert r.status_code == 200, r.text
    return r.json()["scan_token"], r.json()["scan"]


def _blockers(scan) -> dict[str, int]:
    return {b["source_type"]: b["count"] for b in scan["blockers"]}


async def _refused_everywhere(client, session, migration_env, scan_token, labels, modes=MODES) -> None:
    """Every mode refuses the decisions naming each blocker, and a start from a stored scan
    whose decisions were accepted elsewhere is refused before any company is created."""
    for mode, cutover in modes:
        r = await save_decisions(client, scan_token, mode=mode, cutover_date=cutover)
        assert r.status_code == 422, (mode, r.text)
        detail = json.dumps(r.json()["detail"])
        for label in labels:
            assert label in detail, (mode, label, detail)

    path = migration_env["data_dir"] / "migration_scans" / scan_token / "scan.json"
    stored = json.loads(path.read_text())
    for mode, cutover in modes:
        stored["decisions"] = {**(stored.get("decisions") or {}), "mode": mode, "cutover_date": cutover,
                               "mappings": {}, "prepared_by": None}
        path.write_text(json.dumps(stored))
        r = await client.post("/migrations/bootstrap/start", json={
            "scan_token": scan_token, "company_name": "Moved Co", "timezone": "UTC", "name": "Owner",
            "email": "owner@example.com", "password": "ownerpw123"})
        assert r.status_code == 422, (mode, r.text)
        assert (await session.execute(text("SELECT count(*) FROM companies"))).scalar_one() == 0
        assert (await session.execute(text("SELECT count(*) FROM migration_runs"))).scalar_one() == 0


def _not_a_warning(scan, labels) -> None:
    for label in labels:
        assert not [w for w in scan["warnings"] if label.split(" (")[0] in w], (label, scan["warnings"])


def _coverage(scan) -> dict[str, dict]:
    return {row["source_type"]: row for row in scan["coverage"]}


async def test_manager_further_location_scans_as_a_listed_loss(client, session, migration_env, tmp_path):
    """RED before the change: a further inventory location, and the bill that stocks it,
    each stop the migration.

    The location is carried, and the bill's stock is listed as arriving at the default
    location: neither is a blocker."""
    _, scan = await _scan(client, tmp_path, locations=1)
    assert _blockers(scan) == {}
    rows = _coverage(scan)
    assert (rows["CustomInventoryLocation"]["coverage_class"], rows[LOCATED]["count"],
            rows[LOCATED]["coverage_class"]) == ("mapped_with_loss", 1, "mapped_with_loss")


async def test_manager_inventory_transfer_listed_as_not_carried(client, session, migration_env, tmp_path):
    """RED before the change: a stock transfer stops the migration, though it changes no
    figure Manager reports."""
    _, scan = await _scan(client, tmp_path, transfers=1)
    assert _blockers(scan) == {}
    row = _coverage(scan)["InventoryTransfer"]
    assert (row["count"], row["coverage_class"]) == (1, "unsupported_nonfinancial")


async def test_manager_negative_stock_refused_at_scan(client, session, migration_env, tmp_path):
    """RED before the change: delivery notes are not read, so a delivery of 9 widgets
    against the 5 held passes the scan unnoticed."""
    scan_token, scan = await _scan(client, tmp_path, negative=True)
    assert _blockers(scan) == {NEGATIVE: 1}
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{NEGATIVE} (1)"])


async def test_manager_unsupported_inventory_refused_before_run_creation(
    client, session, migration_env, tmp_path,
):
    """RED before the change: negative stock scans clean enough to start a company."""
    scan_token, _ = await _scan(client, tmp_path, negative=True)
    await _refused_everywhere(client, session, migration_env, scan_token, [NEGATIVE])


async def test_manager_inventory_blockers_are_never_warning_only(client, session, migration_env, tmp_path):
    """RED before the change: negative stock appears only as a warning."""
    _, scan = await _scan(client, tmp_path, locations=2, transfers=2, negative=True)
    assert set(_blockers(scan)) == {NEGATIVE}
    for warning in scan["warnings"]:
        for name in ("DeliveryNote", "GoodsReceipt"):
            assert name not in warning, warning


async def test_manager_inventory_blocker_coverage_names_type_and_count(client, session, migration_env, tmp_path):
    """RED before the change: negative stock is not counted as a blocker, and further
    locations and transfers are."""
    scan_token, scan = await _scan(client, tmp_path, locations=2, transfers=2, negative=True)
    assert _blockers(scan) == {NEGATIVE: 1}
    assert all(b["reason"] for b in scan["blockers"])
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{NEGATIVE} (1)"])


NEGATIVE_SALE = "SalesInvoice (negative stock)"


async def test_manager_negative_stock_refused_at_scan_full_history(client, session, migration_env, tmp_path):
    """RED before the change: delivery notes are not read and a flagged invoice takes no
    stock out, so stock that runs 10 below zero and is later made good scans clean.

    The history ends holding 15 widgets, so only a check at every movement sees it."""
    from celerp.importers.adapters.base import MigrationDecisions, ScanError
    from celerp.importers.schema import CIFMode
    from fixtures.manager_io.support import adapter, artifact

    scan_token, scan = await _scan(client, tmp_path, specs.build_negative_recovery)
    assert _blockers(scan) == {NEGATIVE: 1, NEGATIVE_SALE: 1}
    _not_a_warning(scan, [NEGATIVE, NEGATIVE_SALE])
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{NEGATIVE} (1)", f"{NEGATIVE_SALE} (1)"],
                              modes=(("full_history", None),))
    with pytest.raises(ScanError, match=re.escape(f"{NEGATIVE} (1), {NEGATIVE_SALE} (1)")):
        adapter().build_manifest([artifact(tmp_path / "safety.manager")], MigrationDecisions(mode=CIFMode.FULL_HISTORY))


async def test_manager_negative_stock_refused_at_scan_cutover(client, session, migration_env, tmp_path):
    """RED before the change: the opening position at the 01-09 cutover is 5 - 9 - 6 = -10
    widgets in Manager, yet the scan passes it, since neither movement that took stock below
    zero is read."""
    from celerp.importers.adapters.base import MigrationDecisions, ScanError
    from celerp.importers.schema import CIFMode
    from fixtures.manager_io.support import adapter, artifact

    cutover = specs.NEGATIVE_CUTOVER.isoformat()
    scan_token, scan = await _scan(client, tmp_path, specs.build_negative_recovery)
    assert _blockers(scan) == {NEGATIVE: 1, NEGATIVE_SALE: 1}
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{NEGATIVE} (1)", f"{NEGATIVE_SALE} (1)"],
                              modes=(("cutover", cutover),))
    decisions = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=specs.NEGATIVE_CUTOVER)
    for build in (adapter().build_manifest, adapter().source_expectations):
        with pytest.raises(ScanError, match=re.escape(f"{NEGATIVE} (1), {NEGATIVE_SALE} (1)")):
            build([artifact(tmp_path / "safety.manager")], decisions)


async def test_manager_single_named_location_imports_at_the_default_location(
    client, session, migration_env, tmp_path,
):
    """RED before the change: the goods receipt and the flagged bill each stop the
    migration, since they put stock into Store 2, which is not Manager's default location.

    Both locations are carried, and the 8 widgets held at Store 2 are imported at the
    default location, each record listed as such, so the user can move them with a
    transfer once the import is done."""
    from celerp.importers.adapters.base import MigrationDecisions
    from celerp.importers.schema import CIFMode
    from fixtures.manager_io.support import adapter, artifact, ref

    placed = "GoodsReceipt (location)"
    _, scan = await _scan(client, tmp_path, specs.build_single_named_location)
    assert _blockers(scan) == {}
    rows = _coverage(scan)
    assert {label: (rows[label]["count"], rows[label]["coverage_class"]) for label in (placed, LOCATED)} == {
        placed: (1, "mapped_with_loss"), LOCATED: (1, "mapped_with_loss")}
    for decisions in (MigrationDecisions(mode=CIFMode.FULL_HISTORY),
                      MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=date(2026, 1, 5))):
        bundle = adapter().build_manifest([artifact(tmp_path / "safety.manager")], decisions).bundle
        assert [loc.name for loc in bundle.locations] == ["Head Office", "Store 2"]
        held = [(a.quantity, a.value) for a in bundle.inventory_adjustments if a.item_external_id == ref("WID")]
        assert (sum(q for q, _ in held), sum(v or D(0) for _, v in held)) == (D("8"), D("32.00"))


async def _row_counts(session) -> dict[str, int]:
    from celerp.models.base import Base

    return {table.name: (await session.execute(text(f'SELECT count(*) FROM "{table.name}"'))).scalar_one()
            for table in Base.metadata.sorted_tables}


async def test_manager_unsupported_inventory_refused_before_run_creation_bootstrap(
    client, session, migration_env, tmp_path,
):
    """RED before the change: every one of these scans clean, so a bootstrap start creates
    the owner, a staged company and a run, and imports into it.

    On a fresh install, a start from a scan holding any inventory blocker is refused in
    both modes before anything is written: no user, no staged company, no run, and no
    business row in any table."""
    before = await _row_counts(session)
    for build, kinds in ((specs.build_inventory_safety, {"locations": 2, "transfers": 2, "negative": True}),
                         (specs.build_negative_recovery, {})):
        scan_token, scan = await _scan(client, tmp_path, build, **kinds)
        await _refused_everywhere(client, session, migration_env, scan_token,
                                  [f"{label} ({count})" for label, count in _blockers(scan).items()])
    after = await _row_counts(session)
    assert {name: count for name, count in after.items() if count != before[name]} == {}
    for table in ("users", "companies", "migration_runs", "migration_entity_maps", "projections", "ledger"):
        assert after[table] == 0, table


async def test_scan_preview_shows_inventory_blocker_rows(client, session, migration_env, tmp_path, monkeypatch):
    """RED before the change: the scan preview lists no inventory blocker, only warnings the
    user can click past.

    The coverage step of the migration wizard shows each inventory blocker as a blocking
    row naming the record type, its count and why, and none of them as a warning."""
    import html

    import httpx

    import celerp.main
    import ui.api_client as api
    from ui.app import app as ui_app

    def local(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        return httpx.AsyncClient(base_url="http://api", transport=httpx.ASGITransport(app=celerp.main.app),
                                 headers={**(headers or {}), **({"Authorization": f"Bearer {token}"} if token else {})},
                                 follow_redirects=follow_redirects, timeout=timeout)
    monkeypatch.setattr(api, "_local_client", local)

    scan_token, scan = await _scan(client, tmp_path, locations=2, transfers=2, negative=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://localhost",
                                 cookies={"celerp_migration_scan": scan_token}) as ui:
        r = await ui.get("/setup/migrate/coverage")
    assert r.status_code == 200, r.text
    page = html.unescape(r.text)
    reasons = {b["source_type"]: b["reason"] for b in scan["blockers"]}
    blocking, _, warnings = page.partition("<h3>Warnings</h3>")
    for label, count in {NEGATIVE: 1}.items():
        assert f"<li>{label}: {count} records. {reasons[label]}</li>" in blocking, label
        assert label not in warnings, label


ZERO_QUANTITY = {"PurchaseInvoice": ("PurchaseInvoice (unsupported feature)", 64),
                 "SalesInvoice": ("SalesInvoice (unsupported feature)", 69)}


@pytest.mark.parametrize("source_type", sorted(ZERO_QUANTITY))
async def test_manager_own_stock_zero_quantity_line_refused_at_scan(
    client, session, migration_env, tmp_path, source_type,
):
    """RED before the change: a bill or invoice flagged to move its own stock, with an item
    line of quantity 0, crashed the scan with a server error instead of naming the record.

    Celerp cannot move no stock at a cost, so the document is a blocker under the same
    reason a zero-quantity goods receipt or delivery note is, refused on every mode."""
    from celerp.importers.adapters.base import MigrationDecisions, ScanError
    from celerp.importers.schema import CIFMode
    from fixtures.manager_io.encoder import write_manager_file
    from fixtures.manager_io.support import adapter, artifact

    label, flag = ZERO_QUANTITY[source_type]
    record = (specs._bill("BZ", date(2026, 1, 5), {"WID": (D("0"), D("4"))}, {flag: True})
              if source_type == "PurchaseInvoice" else
              specs._sale("SZ", date(2026, 1, 5), {"WID": D("0")}, {flag: True}))

    def build(path, **_):
        return write_manager_file(path, [*specs._stocked_masters(), record])

    scan_token, scan = await _scan(client, tmp_path, build)
    assert _blockers(scan) == {label: 1}
    (blocker,) = scan["blockers"]
    assert "A zero or negative quantity." in json.dumps(blocker)
    _not_a_warning(scan, [label])
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{label} (1)"])
    with pytest.raises(ScanError, match=re.escape(f"{label} (1)")):
        adapter().build_manifest([artifact(tmp_path / "safety.manager")], MigrationDecisions(mode=CIFMode.FULL_HISTORY))
