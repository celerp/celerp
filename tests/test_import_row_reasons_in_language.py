# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""An item import row the server refuses says why in the reader's language and names the row by its SKU."""

from __future__ import annotations

import uuid

import pytest

from fasthtml.common import to_xml

from ui.routes.csv_import import import_result_errors, import_result_panel

from test_helpers import in_language
from test_import_upsert import _setup


def _row(sku: str, **data) -> dict:
    return {
        "entity_id": f"item:{uuid.uuid4().hex[:10]}",
        "event_type": "item.created",
        "data": {"sku": sku, "name": "Row", "quantity": 1, "sell_by": "piece", **data},
        "source": "csv_import",
        "idempotency_key": f"csv:item:{uuid.uuid4().hex}",
    }


@pytest.mark.asyncio
async def test_refused_import_rows_read_in_german_with_their_sku(client, session):
    _, _, token = await _setup(session)
    rows = [
        _row("ROW-NOUNIT", sell_by=""),
        _row("ROW-NEG", quantity=-2),
        _row("ROW-LOC", location_id="not-a-location"),
        _row("ROW-OWNED", attributes={"files": "x"}),
    ]
    r = await client.post("/items/import/batch", headers={"Authorization": f"Bearer {token}"},
                          json={"records": rows})
    assert r.status_code == 200, r.text
    errors = r.json()["errors"]
    assert len(errors) == 4, errors
    for sku, error in zip(("ROW-NOUNIT", "ROW-NEG", "ROW-LOC", "ROW-OWNED"), errors):
        english, german = in_language("en", error), in_language("de", error)
        assert sku in english and sku in german, error
        assert german.startswith("Zeile"), german
        assert german != english and "{" not in german and "'message'" not in german, german


def test_the_import_result_page_shows_a_keyed_row_reason_as_a_sentence():
    error = {"message": "Row (SKU=A1): sell_by is required", "message_key": "import.row_refused",
             "params": {"sku": "A1", "refusal": {"message": "sell_by is required",
                                                 "message_key": "import.row.sell_by_required", "params": {}}}}
    assert import_result_errors({"errors": [error]}) == ["Row (SKU=A1): sell_by is required"]
    html = to_xml(import_result_panel(created=0, skipped=1, updated=0, errors=[error], entity_label="inventory",
                                   back_href="/inventory", import_more_href="/inventory/import"))
    assert "message_key" not in html and "Row (SKU=A1): sell_by is required" in html, html
