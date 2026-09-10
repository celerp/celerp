# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GTIN / RFID-EPC identifiers must be resolvable, searchable, and exported.

The item event model persists with extra=allow, so raw gtin/rfid_epc VALUES do
round-trip through the projection at merge base. These tests pin the genuinely
absent behaviours instead:

- The item list has no gtin and no rfid_epc query parameter, and neither key is
  in the searchable field set, so filtering by either identifier is impossible.
- The CSV export writes a fixed column list that omits gtin and rfid_epc, and the
  DictWriter uses extrasaction="ignore", so both are dropped from every export
  even though they are stored.
"""
from __future__ import annotations

import csv
import io
import uuid

import pytest


async def _token(client) -> str:
    email = f"idsearch-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _create_item(client, headers, **fields) -> str:
    payload = {"status": "available", "name": "Ident Item", "quantity": 1, "sell_by": "piece"}
    payload.update(fields)
    r = await client.post("/items", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_inventory_search_by_gtin_and_epc(client):
    """The item list must filter to exactly the item carrying a given gtin or
    rfid_epc.

    RED at merge base: list_items has no gtin/rfid_epc query parameter and
    neither key is searchable, so the filter is a no-op. A distractor item that
    carries neither identifier is returned alongside the target, so the result
    set is {target, distractor} instead of {target} and the assertion fails.
    """
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    tag = uuid.uuid4().hex[:6]
    gtin = "12345670"
    epc = "EPCSEARCH1"
    target_id = await _create_item(
        client, headers, sku=f"IDS-T-{tag}", gtin=gtin, rfid_epc=epc,
    )
    # A distractor with neither identifier: a no-op filter returns it too, which
    # is exactly what must fail at merge base.
    distractor_id = await _create_item(client, headers, sku=f"IDS-D-{tag}")
    assert distractor_id != target_id

    r = await client.get("/items", params={"gtin": gtin}, headers=headers)
    assert r.status_code == 200, r.text
    ids = {it["id"] for it in r.json()["items"]}
    assert ids == {target_id}, f"gtin filter must return only the target; got {ids}"

    r = await client.get("/items", params={"rfid_epc": epc}, headers=headers)
    assert r.status_code == 200, r.text
    ids = {it["id"] for it in r.json()["items"]}
    assert ids == {target_id}, f"rfid_epc filter must return only the target; got {ids}"


@pytest.mark.asyncio
async def test_gtin_rfid_epc_csv_round_trip(client):
    """The CSV export must carry gtin and rfid_epc columns holding the item's
    stored identifier values.

    RED at merge base: the export's fixed _COLS list omits both columns, and the
    DictWriter uses extrasaction="ignore", so the header row lacks "gtin" and
    "rfid_epc" and the values never appear in the export.
    """
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    tag = uuid.uuid4().hex[:6]
    sku = f"IDS-CSV-{tag}"
    gtin = "12345670"
    epc = "EPCCSV1"
    await _create_item(client, headers, sku=sku, gtin=gtin, rfid_epc=epc)

    r = await client.get("/items/export/csv", headers=headers)
    assert r.status_code == 200, r.text

    reader = csv.DictReader(io.StringIO(r.text))
    fieldnames = reader.fieldnames or []
    assert "gtin" in fieldnames, f"export header missing gtin column; got {fieldnames}"
    assert "rfid_epc" in fieldnames, f"export header missing rfid_epc column; got {fieldnames}"

    rows = [row for row in reader if row.get("sku") == sku]
    assert len(rows) == 1, f"expected exactly one export row for {sku}; got {len(rows)}"
    row = rows[0]
    assert row.get("gtin") == gtin, f"export row must carry the stored gtin; got {row.get('gtin')!r}"
    assert row.get("rfid_epc") == epc, \
        f"export row must carry the stored rfid_epc; got {row.get('rfid_epc')!r}"
