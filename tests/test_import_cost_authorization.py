# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""An importer who may not see costs cannot change an existing item's cost through any import.

Every import transport (the browser rows, the uploaded file the agent previews and commits,
and the raw record batch) refuses a cost cell on a row that updates an existing item with one
refusal that names no amount and does not depend on the cell or on the item's cost, so
repeated imports reveal nothing about that cost. New items and the other fields still import.
"""
from __future__ import annotations

import json
import uuid

import pytest

from test_cost_restatement import _state
from test_import_preflight_invariants import write_upload  # noqa: F401  (fixture)
from test_merge_unknown_cost import _read, _with_freight

_GRANTED = ["import_export_data", "edit_inventory", "set_inventory_prices"]
_KEY = "cost-auth"
# Proposed whole costs: below the freight the lot carries, the cost it holds (filled in per
# lot), above it, a blank cell, and cells that are no cost.
_PROPOSED = ["10", "equal", "1000", "", "abc", "-5"]


async def _grant(session, auth, see_costs: bool) -> None:
    from celerp.services.permissions import ROLE_LEVELS
    from test_import_invariants import _set_company_settings
    up = sorted(r for r in ROLE_LEVELS if ROLE_LEVELS[r] >= ROLE_LEVELS["operator"])
    owner_only = sorted(r for r in ROLE_LEVELS if ROLE_LEVELS[r] > ROLE_LEVELS["operator"])
    await _set_company_settings(session, auth["company_id"], role_grants={
        **{k: up for k in _GRANTED}, "view_inventory_costs": up if see_costs else owner_only})


async def _exported(client, auth, lot: str) -> str:
    """The lot's cost cell as the item file exports it."""
    r = await client.get("/items/export/csv", headers=auth["headers"], params={"cols": "sku,cost_price_total"})
    sku = (await _read(client, auth, lot))["sku"]
    [cell] = [line.split(",")[1] for line in r.text.splitlines() if line.split(",")[0] == sku]
    return cell


def _user(h: dict) -> str:
    from celerp.services.auth import decode_access_token
    return decode_access_token(h["Authorization"].split()[1])["sub"]


def _rows_body(row: dict, key: str) -> dict:
    return {"rows": [row], "upsert": True, "idempotency_key": key}


async def _call(transport: str, client, h, write_upload, auth, row: dict, *, lot: str,  # noqa: F811
                key: str = _KEY, uploads: dict | None = None):
    """(preview response or None, commit response) for one row through one transport. With
    ``uploads``, a file of the same text is uploaded once and previewed again by its id."""
    if transport == "rows":
        preview = await client.post("/items/import/rows/preview", headers=h, json=_rows_body(row, key))
        return preview, await client.post("/items/import/rows", headers=h, json=_rows_body(row, key))
    if transport == "file":
        cols = list(row)
        text = ",".join(cols) + "\n" + ",".join(str(row[c]) for c in cols) + "\n"
        uploads = {} if uploads is None else uploads
        if text not in uploads:
            uploads[text] = write_upload(str(auth["company_id"]), _user(h), text)
        fid = uploads[text]
        preview = await client.post("/items/import/preview", headers=h, json={"file_id": fid, "upsert": True})
        commit = await client.post("/items/import/commit", headers=h, json={
            "file_id": fid, "upsert": True, "preview_hash": preview.json().get("preview_hash")})
        return preview, commit
    data = {k: v for k, v in row.items() if k != "cost_price_total"}
    if "cost_price_total" in row:
        data["cost_total"] = row["cost_price_total"]
    created = lot is None
    record = {"entity_id": f"item:{uuid.uuid4()}" if created else lot,
              "event_type": "item.created" if created else "item.patched",
              "data": data, "idempotency_key": key,
              "source": "csv_import"}
    return None, await client.post("/items/import/batch", headers=h, json={"records": [record], "upsert": True})


def _input_free(r) -> dict:
    """A response without the fields that echo or bind the caller's own input (the uploaded
    file's id, its sample rows, the hash binding a preview to its input)."""
    body = r.json()
    for key in ("preview_hash", "file_id", "sample"):
        body.pop(key, None)
    return {"status": r.status_code, "body": body}


@pytest.mark.parametrize("transport", ["rows", "file", "batch"])
async def test_an_importer_who_cannot_see_costs_cannot_read_or_change_an_existing_cost(
        client, session, auth, write_upload, transport):  # noqa: F811
    from test_company_settings_owners import _user_with_role
    from test_merge_unknown_cost import _cost_column
    await _cost_column(session, auth)
    lot = await _with_freight(client, session, auth)
    sku = (await _read(client, auth, lot))["sku"]
    await _grant(session, auth, see_costs=False)
    operator = await _user_with_role(client, session, auth["headers"], "operator")

    async def _set_goods(goods: float) -> str:
        r = await client.patch(f"/items/{lot}", headers=auth["headers"],
                               json={"fields_changed": {"cost_total": {"new": goods}}})
        assert r.status_code == 200, r.text
        return await _exported(client, auth, lot)

    # Unauthorized: one refusal for every cost cell, whatever the lot's own cost is.
    refused: dict[tuple, tuple] = {}
    uploads: dict[str, str] = {}
    blank: dict[float, tuple] = {}
    for goods in (100.0, 400.0):
        held = await _set_goods(goods)
        for proposed in _PROPOSED:
            cell = held if proposed == "equal" else proposed
            # A refused row writes nothing, so every refused cell may reuse one key.
            # The same refused file is previewed against each protected cost.
            if proposed == "":
                key, files = f"{_KEY}-blank-{goods}", None
            else:
                key, files = _KEY, uploads
            preview, commit = await _call(transport, client, operator, write_upload, auth,
                                          {"sku": sku, "name": "Beads", "cost_price_total": cell}, lot=lot, key=key,
                                          uploads=files)
            assert await _exported(client, auth, lot) == held, (goods, proposed)
            if proposed == "":
                blank[goods] = (preview, commit)
                continue
            refused[(goods, proposed)] = (preview, commit)

    shapes = {json.dumps([_input_free(r) for r in pair if r is not None], sort_keys=True)
              for pair in refused.values()}
    assert len(shapes) == 1, "\n".join(shapes)
    [shape] = shapes
    assert "view_inventory_costs" in shape and "130" not in shape and "430" not in shape, shape
    assert all(commit.status_code in (200, 422) for _p, commit in refused.values())
    # The same cells against two different protected costs: the whole responses, byte for byte.
    for proposed in (p for p in _PROPOSED if p not in ("equal", "")):
        a, b = refused[(100.0, proposed)], refused[(400.0, proposed)]
        assert [r.content for r in a if r is not None] == [r.content for r in b if r is not None], proposed
    # A blank cost cell changes no cost: the row's other fields import.
    for preview, commit in blank.values():
        assert commit.status_code == 200, commit.text
        assert not commit.json()["errors"], commit.text
        if preview is not None:
            assert preview.json()["errors"] == [], preview.text
    if transport != "batch":
        assert _input_free(blank[100.0][0]) == _input_free(blank[400.0][0])

    # Unauthorized still creates new items with a cost and changes an existing item's other fields.
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": "Shelf", "type": "warehouse"})
    assert r.status_code == 200, r.text
    new_sku = f"NEW-{uuid.uuid4().hex[:6]}"
    _p, commit = await _call(transport, client, operator, write_upload, auth,
                             {"sku": new_sku, "name": "Fresh", "location_name": "Shelf", "sell_by": "piece", "quantity": 1,
                              "cost_price_total": "50"},
                             lot=None,
                             key=f"{_KEY}-new")
    assert commit.status_code == 200 and commit.json()["created"] == 1, commit.text
    _p, commit = await _call(transport, client, operator, write_upload, auth,
                             {"sku": sku, "name": "Renamed"}, lot=lot, key=f"{_KEY}-rename")
    assert commit.status_code == 200 and commit.json()["updated"] == 1, commit.text
    assert (await _read(client, auth, lot))["name"] == "Renamed"

    # Authorized: the cost is checked and changed as before.
    await _grant(session, auth, see_costs=True)
    held = await _set_goods(100.0)
    outcomes = {}
    for proposed in _PROPOSED:
        cell = held if proposed == "equal" else proposed
        preview, commit = await _call(transport, client, operator, write_upload, auth,
                                      {"sku": sku, "name": "Renamed", "cost_price_total": cell}, lot=lot,
                                      key=f"{_KEY}-ok-{uuid.uuid4().hex[:6]}")
        outcomes[proposed] = (preview, commit, await _exported(client, auth, lot))
        await _set_goods(100.0)
    below = outcomes["10"][1]
    assert below.status_code == 422 or below.json()["errors"], below.text
    assert "30.00" in below.text and "view_inventory_costs" not in below.text, below.text
    assert outcomes["1000"][1].status_code == 200 and outcomes["1000"][2] == "1000.0", outcomes["1000"][1].text
    assert outcomes["equal"][1].status_code == 200 and outcomes["equal"][2] == held
    assert outcomes[""][1].status_code == 200 and outcomes[""][2] == held
    for bad in ("abc", "-5"):
        r = outcomes[bad][1]
        assert (r.status_code == 422 or r.json()["errors"]) and outcomes[bad][2] == held, (bad, r.text)
    if transport != "batch":
        assert outcomes["1000"][0].json()["errors"] == []
        if transport == "rows":
            assert outcomes["1000"][0].json()["cost_changes"] == [
                {"row": 1, "sku": sku, "old": held, "new": "1000.0"}]
    assert (await _state(session, auth, lot))["cost_base"] == 100.0
