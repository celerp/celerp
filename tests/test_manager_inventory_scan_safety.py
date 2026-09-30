# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Inventory Celerp cannot carry yet stops a Manager migration at the scan: more than one
inventory location, stock transfers between locations, and stock that runs below zero.
Each is a blocker naming the record type and how many there are, refused on both modes
before any company or run exists, and never a warning the user can click past."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from fixtures.manager_io import specs
from migration_support import migration_env, save_decisions, scan_upload  # noqa: F401 - fixture

LOCATIONS = "CustomInventoryLocation (multiple locations)"
LOCATED = "PurchaseInvoice (multiple locations)"
TRANSFERS = "InventoryTransfer (inter-location transfer)"
NEGATIVE = "DeliveryNote (negative stock)"
MODES = (("full_history", None), ("cutover", "2026-01-31"))


async def _scan(client, tmp_path, **kinds) -> tuple[str, dict]:
    path = specs.build_inventory_safety(tmp_path / "safety.manager", **kinds)
    r = await scan_upload(client, path.read_bytes(), name="safety.manager")
    assert r.status_code == 200, r.text
    return r.json()["scan_token"], r.json()["scan"]


def _blockers(scan) -> dict[str, int]:
    return {b["source_type"]: b["count"] for b in scan["blockers"]}


async def _refused_everywhere(client, session, migration_env, scan_token, labels) -> None:
    """Both modes refuse the decisions naming each blocker, and a start from a stored scan
    whose decisions were accepted elsewhere is refused before any company is created."""
    for mode, cutover in MODES:
        r = await save_decisions(client, scan_token, mode=mode, cutover_date=cutover)
        assert r.status_code == 422, (mode, r.text)
        detail = json.dumps(r.json()["detail"])
        for label in labels:
            assert label in detail, (mode, label, detail)

    path = migration_env["data_dir"] / "migration_scans" / scan_token / "scan.json"
    stored = json.loads(path.read_text())
    for mode, cutover in MODES:
        stored["decisions"] = {**(stored.get("decisions") or {}), "mode": mode, "cutover_date": cutover,
                               "mappings": {}, "prepared_by": None}
        path.write_text(json.dumps(stored))
        r = await client.post("/migrations/bootstrap/start", json={
            "scan_token": scan_token, "company_name": "Moved Co", "name": "Owner",
            "email": "owner@example.com", "password": "ownerpw123"})
        assert r.status_code == 422, (mode, r.text)
        assert (await session.execute(text("SELECT count(*) FROM companies"))).scalar_one() == 0
        assert (await session.execute(text("SELECT count(*) FROM migration_runs"))).scalar_one() == 0


def _not_a_warning(scan, labels) -> None:
    for label in labels:
        assert not [w for w in scan["warnings"] if label.split(" (")[0] in w], (label, scan["warnings"])


async def test_manager_multiple_locations_refused_at_scan(client, session, migration_env, tmp_path):
    """RED before the change: extra inventory locations are listed as not carried and the
    migration goes ahead, putting stock held at the second location in the first."""
    scan_token, scan = await _scan(client, tmp_path, locations=1)
    assert _blockers(scan) == {LOCATIONS: 1, LOCATED: 1}
    _not_a_warning(scan, [LOCATIONS])
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{LOCATIONS} (1)", f"{LOCATED} (1)"])


async def test_manager_inventory_transfer_refused_at_scan(client, session, migration_env, tmp_path):
    """RED before the change: stock transfers are listed as not carried and dropped."""
    scan_token, scan = await _scan(client, tmp_path, transfers=1)
    assert _blockers(scan) == {TRANSFERS: 1}
    _not_a_warning(scan, [TRANSFERS])
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{TRANSFERS} (1)"])


async def test_manager_negative_stock_refused_at_scan(client, session, migration_env, tmp_path):
    """RED before the change: delivery notes are not read, so a delivery of 9 widgets
    against the 5 held passes the scan unnoticed."""
    scan_token, scan = await _scan(client, tmp_path, negative=True)
    assert _blockers(scan) == {NEGATIVE: 1}
    await _refused_everywhere(client, session, migration_env, scan_token, [f"{NEGATIVE} (1)"])


async def test_manager_unsupported_inventory_refused_before_run_creation(
    client, session, migration_env, tmp_path,
):
    """RED before the change: every one of these scans clean enough to start a company."""
    for kinds, labels in (({"locations": 1}, [LOCATIONS]), ({"transfers": 1}, [TRANSFERS]),
                          ({"negative": True}, [NEGATIVE])):
        scan_token, _ = await _scan(client, tmp_path, **kinds)
        await _refused_everywhere(client, session, migration_env, scan_token, labels)


async def test_manager_inventory_blockers_are_never_warning_only(client, session, migration_env, tmp_path):
    """RED before the change: locations and transfers appear only as warnings."""
    _, scan = await _scan(client, tmp_path, locations=2, transfers=2, negative=True)
    assert set(_blockers(scan)) == {LOCATIONS, LOCATED, TRANSFERS, NEGATIVE}
    for warning in scan["warnings"]:
        for name in ("CustomInventoryLocation", "InventoryTransfer", "DeliveryNote", "GoodsReceipt"):
            assert name not in warning, warning


async def test_manager_inventory_blocker_coverage_names_type_and_count(client, session, migration_env, tmp_path):
    """RED before the change: none of these are counted as blockers."""
    scan_token, scan = await _scan(client, tmp_path, locations=2, transfers=2, negative=True)
    assert _blockers(scan) == {LOCATIONS: 2, LOCATED: 1, TRANSFERS: 2, NEGATIVE: 1}
    assert all(b["reason"] for b in scan["blockers"])
    await _refused_everywhere(client, session, migration_env, scan_token, [
        f"{LOCATIONS} (2)", f"{LOCATED} (1)", f"{TRANSFERS} (2)", f"{NEGATIVE} (1)"])
