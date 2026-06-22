# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""I1 - importing items via the cloud relay dropped the server.

Root cause: the relay forwards each proxied HTTP request as ONE base64'd WebSocket
message. The host client used the `websockets` default `max_size` of 1 MiB, so a
large item-import body overflowed the frame -> the WS connection dropped -> the
host became unreachable through the relay ("dropped the server"). Fixed by raising
the host client's `max_size` to 160 MiB (celerp/gateway/client.py) and chunking the
import to <=500 records per call.

These tests verify the merged fix:
  1. the host client is actually configured with the raised frame size;
  2. a realistic large item import fits the new frame but would have overflowed the
     old 1 MiB default (i.e. the fix is what closes the I1 scenario);
  3. the import endpoint processes a full 500-record batch and isolates bad rows
     without erroring - the server stays up and responsive.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path

import pytest

from celerp.events.types import EventType

_OLD_DEFAULT_MAX = 1 * 1024 * 1024          # websockets library default that caused I1
_RAISED_MAX = 160 * 1024 * 1024             # the merged fix in celerp/gateway/client.py
_CLIENT_SRC = Path(__file__).resolve().parents[1] / "celerp" / "gateway" / "client.py"


async def _register(client) -> str:
    email = f"i1-{uuid.uuid4().hex[:10]}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "I1 Co", "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _item_record(i: int) -> dict:
    return {
        "entity_id": f"item:i1-{uuid.uuid4()}",
        "entity_type": "item",
        "event_type": EventType.ITEM_CREATED,
        "data": {
            "sku": f"I1-{i:05d}",
            "name": f"Bulk Imported Item number {i} with a realistically long descriptive name",
            "category": "Imported",
            "sell_by": "piece",
            "quantity": 1,
            "cost_price": 1.23,
            "wholesale_price": 2.34,
            "retail_price": 3.45,
        },
        "idempotency_key": f"i1:{uuid.uuid4()}",
        "source": "csv_import",
        "source_ts": None,
    }


# ── 1. The merged transport fix is present ────────────────────────────────────

def test_host_client_ws_max_size_is_raised():
    """The host relay client must connect with the raised WS frame size; the library
    default (1 MiB) is what dropped the connection on a large import (I1)."""
    src = _CLIENT_SRC.read_text()
    assert "max_size=160 * 1024 * 1024" in src, (
        "celerp/gateway/client.py must raise the WS max_size to 160 MiB so large import "
        "bodies traverse the relay instead of overflowing the frame and dropping the host"
    )
    # And it must not silently fall back to the library default on the live connect call.
    connect = re.search(r"websockets\.connect\((.*?)\)", src, re.S)
    assert connect and "max_size=" in connect.group(1), "websockets.connect must set max_size explicitly"


# ── 2. A realistic large import fits the new frame, not the old ───────────────

def test_large_item_import_fits_new_frame_but_overflowed_old():
    """A ~4,000-item import (the I1 scenario) framed as one base64'd WS message
    exceeds the old 1 MiB default (would drop -> I1) but fits the raised 160 MiB."""
    records = [_item_record(i) for i in range(4000)]
    body = json.dumps({"records": records, "filename": "big_import.csv"}).encode()
    framed = len(base64.b64encode(body))  # the relay sends the body base64-encoded
    assert framed > _OLD_DEFAULT_MAX, (
        f"test payload ({framed} B framed) should exceed the old 1 MiB cap to model I1"
    )
    assert framed <= _RAISED_MAX, (
        f"a 4k-item import ({framed} B framed) must fit the raised 160 MiB WS frame"
    )


# ── 3. The endpoint stays up under a full batch and bad rows ──────────────────

@pytest.mark.asyncio
async def test_max_batch_import_succeeds_and_server_stays_responsive(client):
    """A full 500-record batch (the per-call max) imports cleanly and the server is
    still responsive afterwards - it does not drop."""
    token = await _register(client)
    records = [_item_record(i) for i in range(500)]
    r = await client.post(
        "/items/import/batch",
        headers=_h(token),
        json={"records": records, "filename": "max_batch.csv"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 500, body
    # Server is still alive and serving after the large import.
    health = await client.get("/items/import/batches", headers=_h(token))
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_import_isolates_bad_rows_without_crashing(client):
    """A batch mixing valid and invalid rows must not 500; bad rows are skipped and
    reported while the valid ones import - one malformed row can't drop the import."""
    token = await _register(client)
    good = [_item_record(i) for i in range(5)]
    bad = []
    for i in range(3):
        rec = _item_record(1000 + i)
        rec["data"].pop("sell_by")  # missing required unit -> per-row error, not a crash
        bad.append(rec)
    records = good + bad
    r = await client.post(
        "/items/import/batch",
        headers=_h(token),
        json={"records": records, "filename": "mixed.csv"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 5, body
    assert body["skipped"] == 3, body
    assert len(body["errors"]) == 3, body
