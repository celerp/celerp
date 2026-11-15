# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every batch import route answers a refused row with a keyed refusal.

A batch route's errors are what the import page and the AI show for the rows it did not
import. Each one is a refusal with its catalog key (``import.record_refused`` naming the row,
the reason inside it keyed too), so it reads in each reader's language. Every batch route of
the app is driven here with rows it refuses; a new batch route fails this test until it is
given its refused rows below, so none can ship its reasons as English text.
"""
from __future__ import annotations

import re

import pytest

from celerp.main import app

pytestmark = pytest.mark.asyncio

_BATCH = re.compile(r"/import/(batch|preview)$")


def _rec(entity_id: str, event_type: str, data: dict | None = None) -> dict:
    return {"entity_id": entity_id, "event_type": event_type, "data": data or {},
            "source": "import", "idempotency_key": f"keyed-rows:{entity_id}"}


def _settings(*rows: dict) -> dict:
    return {"records": [_rec(f"row-{i}", "settings.row", row) for i, row in enumerate(rows)]}


_TAX_ROWS = _settings({"name": ""}, {"name": "Bad rate", "rate": "seven"},
                      {"name": "Bad type", "rate": 7, "tax_type": "sometimes"})
_TERM_ROWS = _settings({"name": ""}, {"name": "Bad days", "days": "thirty"})
_CHART_ROWS = {"records": [
    "not a row",
    {"code": "", "name": "No code", "account_type": "asset"},
    {"code": "9101", "name": "Twice", "account_type": "asset"},
    {"code": "9101", "name": "Twice again", "account_type": "asset"},
    {"code": "9102", "name": "Orphan", "account_type": "asset", "parent_code": "9999"},
    {"code": "9103", "name": "Under orphan", "account_type": "asset", "parent_code": "9102"},
    {"code": "9104", "name": "Loop A", "account_type": "asset", "parent_code": "9105"},
    {"code": "9105", "name": "Loop B", "account_type": "asset", "parent_code": "9104"},
]}

# Each batch route, and rows it refuses.
REFUSED_ROWS: dict[str, dict] = {
    "/companies/import/batch": {"records": [_rec("co-1", "sys.user.created")]},
    "/companies/me/locations/import/batch": {"records": [{"name": ""}]},
    "/companies/me/taxes/import/batch": _TAX_ROWS,
    "/companies/me/purchasing-taxes/import/batch": _TAX_ROWS,
    "/companies/me/payment-terms/import/batch": _TERM_ROWS,
    "/companies/me/purchasing-payment-terms/import/batch": _TERM_ROWS,
    "/accounting/accounts/import/preview": _CHART_ROWS,
    "/accounting/accounts/import/batch": _CHART_ROWS,
    "/accounting/import/batch": {"records": [_rec("je-1", "acc.account.created")]},
    "/crm/contacts/import/batch": {"records": [
        _rec("ct-1", "crm.contact.deleted"),
        _rec("ct-2", "crm.contact.created", {"name": "Us", "is_self": True}),
    ]},
    "/docs/import/batch": {"records": [_rec("doc-1", "doc.voided")]},
    "/lists/import/batch": {"records": [_rec("list-1", "list.voided")]},
    "/manufacturing/import/batch": {"records": [_rec("mfg-1", "mfg.order.completed")]},
    "/items/import/batch": {"records": [_rec("item-1", "item.deleted")]},
}


def _batch_routes() -> set[str]:
    # The item and subscription previews check a file's cells before any row is written;
    # they answer cell checks, not refused rows.
    return {path for path, ops in app.openapi()["paths"].items() if "post" in ops and _BATCH.search(path)
            and not path.startswith(("/items/import/preview", "/subscriptions/"))}


def _keyed(error) -> bool:
    if not (isinstance(error, dict) and error.get("message_key") and error.get("message")):
        return False
    inner = (error.get("params") or {}).get("refusal")
    return error["message_key"] != "import.record_refused" or (isinstance(inner, dict) and bool(inner.get("message_key")))


async def test_every_batch_route_has_refused_rows_here(client):
    # The client mounts every bundled module's routes.
    assert _batch_routes() == set(REFUSED_ROWS)


@pytest.mark.parametrize("path", sorted(REFUSED_ROWS))
async def test_a_refused_row_is_a_keyed_refusal(client, auth, path):
    r = await client.post(path, headers=auth["headers"], json=REFUSED_ROWS[path])
    assert r.status_code == 200, r.text
    errors = r.json().get("errors")
    assert errors, r.text
    assert [e for e in errors if not _keyed(e)] == []
