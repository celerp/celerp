# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""One logical item import is one history and undo operation.

A semantic import (browser confirm, file preview and commit, or direct mapped
rows) is written in bounded chunks but recorded as one Import History entry:
the response names that entry, an exact retry resolves to it and appends
nothing, an interrupted import leaves nothing behind, and Undo removes every
item it created. Undo releases the operation so the same source can be
imported again as a new history entry. The raw event batch endpoint keeps its
per-call history and cannot be undone. Every import and undo route stays behind
the canonical permission gate.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from celerp.services import import_stage
from httpx import ASGITransport, AsyncClient

from celerp.events.types import EventType
from test_helpers import clear_sample_items, grant_permission, perm_setup

# The shared semantic importer's writer chunk, made small here so a few rows
# cross the same chunk boundaries a large import does.
_CHUNK = 5
_TRANSPORTS = ["browser", "rows", "file"]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    import celerp_inventory.services as svc
    monkeypatch.setattr(svc, "IMPORT_CHUNK", _CHUNK)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Keep browser import stages and uploaded files inside the test's own directory."""
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path


@pytest.fixture
async def ctx(client, session, data_dir):
    from celerp.services.auth import decode_access_token
    s = await perm_setup(client, session)
    for role in ("admin", "manager"):
        token = s[f"{role}_h"]["Authorization"].split()[1]
        claims = decode_access_token(token)
        s[f"{role}_token"] = token
        s[f"{role}_user_id"] = claims["sub"]
        s["company_id"] = claims["company_id"]
    await clear_sample_items(session, s["company_id"])
    return s


def _csv(names: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "sell_by", "quantity"])
    for name in names:
        writer.writerow([name, "piece", "1"])
    return buf.getvalue()


def _distinct_csv(count: int, prefix: str = "Item") -> str:
    return _csv([f"{prefix} {i:05d}" for i in range(count)])


def _rows(csv_text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def _write_upload(ctx: dict, text: str, *, role: str = "admin", filename: str = "items.csv") -> str:
    """Seed an owned transient upload, as the upload endpoint would."""
    from celerp.ai.files import upload_dir
    file_id = f"ai_up_{uuid.uuid4().hex}"
    data = text.encode()
    (upload_dir() / f"{file_id}.bin").write_bytes(data)
    (upload_dir() / f"{file_id}.meta").write_text(json.dumps({
        "filename": filename, "content_type": "text/csv", "size": len(data),
        "company_id": ctx["company_id"], "user_id": ctx[f"{role}_user_id"],
    }))
    return file_id


@asynccontextmanager
async def _browser():
    """The real UI app, with its API client routed to the in-process API."""
    from celerp.main import app as api_app
    from ui.app import app as ui_app

    def _bridged(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return AsyncClient(
            transport=ASGITransport(app=api_app), base_url="http://test",
            headers=merged, timeout=timeout, follow_redirects=follow_redirects,
        )

    with patch("ui.api_client._local_client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            yield c


class _Operation:
    """One logical import of one source through one transport.

    Calling ``run`` again repeats the exact same operation: the same mapped rows
    and operation key (direct rows), the same uploaded file and preview (file),
    or the same file staged and confirmed again (browser).
    """

    def __init__(self, client, ctx: dict, transport: str, csv_text: str, *, filename: str = "items.csv"):
        self.client, self.ctx, self.transport = client, ctx, transport
        self.csv_text, self.rows = csv_text, _rows(csv_text)
        self.key = f"op-{uuid.uuid4().hex}"
        self.filename = filename
        self.file_id: str | None = None

    async def run(self) -> tuple[int, dict | None]:
        """Run the import; return the HTTP status and the importer's result body."""
        return await getattr(self, f"_{self.transport}")()

    async def _rows(self):
        h = self.ctx["admin_h"]
        r = await self.client.post("/items/import/rows/preview", headers=h, json={
            "rows": self.rows, "upsert": False, "idempotency_key": self.key,
        })
        assert r.status_code == 200 and r.json()["errors"] == [], r.text
        r = await self.client.post("/items/import/rows", headers=h, json={
            "rows": self.rows, "upsert": False, "idempotency_key": self.key,
            "preview_hash": r.json()["preview_hash"], "filename": self.filename,
        })
        return r.status_code, (r.json() if r.status_code == 200 else None)

    async def _file(self):
        h = self.ctx["admin_h"]
        if self.file_id is None:
            self.file_id = _write_upload(self.ctx, self.csv_text, filename=self.filename)
        r = await self.client.post("/items/import/preview", headers=h, json={"file_id": self.file_id})
        assert r.status_code == 200 and r.json()["errors"] == [], r.text
        r = await self.client.post("/items/import/commit", headers=h, json={
            "file_id": self.file_id, "preview_hash": r.json()["preview_hash"],
        })
        return r.status_code, (r.json() if r.status_code == 200 else None)

    async def _browser(self):
        import ui.api_client as ui_api
        from ui.routes.inventory import _import_operation_key

        # The review step's preview of exactly these rows and this operation key.
        r = await self.client.post("/items/import/rows/preview", headers=self.ctx["admin_h"], json={
            "rows": self.rows, "upsert": False, "idempotency_key": _import_operation_key(self.rows, False),
        })
        assert r.status_code == 200 and r.json()["errors"] == [], r.text
        preview_hash = r.json()["preview_hash"]
        ref = import_stage.write_stage(self.ctx["company_id"], self.csv_text)

        results: list[dict] = []
        real_import_rows = ui_api.import_rows

        async def _capture(*a, **k):
            result = await real_import_rows(*a, **k)
            results.append(result)
            return result

        with patch("ui.api_client.import_rows", new=_capture):
            async with _browser() as ui:
                r = await ui.post(
                    "/inventory/import/confirm",
                    data={"csv_ref": ref, "preview_hash": preview_hash},
                    cookies={"celerp_token": self.ctx["admin_token"]},
                )
        assert len(results) <= 1
        return r.status_code, (results[0] if results else None)


async def _run_ok(op: _Operation) -> dict:
    status, body = await op.run()
    assert status == 200 and body is not None, (status, body)
    assert body["errors"] == [], body["errors"][:5]
    return body


async def _history(client, h) -> list[dict]:
    r = await client.get("/items/import/batches", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["batches"]


async def _batch_rows(session, company_id: str) -> list:
    from sqlalchemy import select

    from celerp.models.import_batch import ImportBatch
    session.expire_all()
    return list((await session.execute(
        select(ImportBatch).where(ImportBatch.company_id == uuid.UUID(company_id))
        .order_by(ImportBatch.imported_at)
    )).scalars().all())


async def _item_ids(session, company_id: str) -> set[str]:
    """Item ids of the company, leaving out sample items the first import clears."""
    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    return {eid for eid in (await session.execute(
        select(Projection.entity_id).where(
            Projection.company_id == uuid.UUID(company_id), Projection.entity_type == "item",
        )
    )).scalars().all() if not eid.startswith("item:demo-")}


async def _commit_rows(client, h, rows: list[dict], *, upsert: bool, key: str | None,
                       decisions: dict | None = None) -> dict:
    """Preview and commit mapped rows over the direct API; return the importer's result."""
    r = await client.post("/items/import/rows/preview", headers=h, json={
        "rows": rows, "upsert": upsert, "idempotency_key": key, "decisions": decisions or {},
    })
    assert r.status_code == 200 and r.json()["errors"] == [], r.text
    r = await client.post("/items/import/rows", headers=h, json={
        "rows": rows, "upsert": upsert, "idempotency_key": key, "preview_hash": r.json()["preview_hash"],
        "decisions": decisions or {},
    })
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == [], r.json()["errors"][:5]
    return r.json()


async def _names_with_sku(session, company_id: str, sku: str) -> list[str]:
    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    return sorted((await session.execute(
        select(Projection.state["name"].as_string()).where(
            Projection.company_id == uuid.UUID(company_id), Projection.entity_type == "item",
            Projection.state["sku"].as_string() == sku,
        )
    )).scalars().all())


async def _snapshot(session, company_id: str) -> dict:
    from test_onboarding_import_invariants import _business_snapshot
    return await _business_snapshot(session, company_id)


# ---------------------------------------------------------------------------
# A three-chunk semantic import is one history operation
# ---------------------------------------------------------------------------


class TestLogicalImportHistory:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_three_chunk_semantic_import_creates_one_history_batch(self, client, session, ctx, transport):
        rows = 2 * _CHUNK + 1
        body = await _run_ok(_Operation(client, ctx, transport, _distinct_csv(rows)))
        assert body["created"] == rows
        history = await _history(client, ctx["admin_h"])
        assert len(history) == 1, [(b["id"], b["row_count"]) for b in history]
        assert history[0]["id"] == body["batch_id"]
        assert history[0]["status"] == "active"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_three_chunk_history_row_count_matches_all_created_rows(self, client, session, ctx, transport):
        rows = 2 * _CHUNK + 1
        before = await _item_ids(session, ctx["company_id"])
        body = await _run_ok(_Operation(client, ctx, transport, _distinct_csv(rows)))
        created_ids = await _item_ids(session, ctx["company_id"]) - before
        assert body["created"] == len(created_ids) == rows

        listed = next(b for b in await _history(client, ctx["admin_h"]) if b["id"] == body["batch_id"])
        assert listed["row_count"] == rows
        (batch,) = [b for b in await _batch_rows(session, ctx["company_id"]) if str(b.id) == body["batch_id"]]
        assert batch.row_count == rows
        assert set(batch.entity_ids) == created_ids and len(batch.entity_ids) == rows
        assert len(set(batch.idempotency_keys)) == rows

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_three_chunk_undo_removes_all_created_rows(self, client, session, ctx, transport):
        rows = 2 * _CHUNK + 1
        before = await _item_ids(session, ctx["company_id"])
        body = await _run_ok(_Operation(client, ctx, transport, _distinct_csv(rows)))
        assert len(await _item_ids(session, ctx["company_id"]) - before) == rows

        r = await client.post(f"/items/import/batches/{body['batch_id']}/undo", headers=ctx["admin_h"])
        assert r.status_code == 200, r.text
        assert r.json()["removed"] == rows
        assert await _item_ids(session, ctx["company_id"]) == before
        history = await _history(client, ctx["admin_h"])
        assert [b["status"] for b in history] == ["undone"]


# ---------------------------------------------------------------------------
# Retry, interruption, and undo keep one logical history identity
# ---------------------------------------------------------------------------


class TestLogicalImportRetry:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_exact_retry_returns_same_logical_batch_and_appends_nothing(self, client, session, ctx, transport):
        rows = _CHUNK + 1
        op = _Operation(client, ctx, transport, _distinct_csv(rows))
        first = await _run_ok(op)
        assert first["created"] == rows and first["batch_id"]
        after_first = await _snapshot(session, ctx["company_id"])

        second = await _run_ok(op)
        assert (second["created"], second["skipped"]) == (0, rows)
        assert second["batch_id"] == first["batch_id"]
        assert await _snapshot(session, ctx["company_id"]) == after_first
        history = await _history(client, ctx["admin_h"])
        assert [(b["id"], b["row_count"], b["status"]) for b in history] == [(first["batch_id"], rows, "active")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_interrupted_after_first_chunk_leaves_nothing_and_the_retry_writes_it_whole(
        self, client, session, ctx, transport, monkeypatch,
    ):
        import celerp_inventory.services as svc
        rows = _CHUNK + 1
        before = await _item_ids(session, ctx["company_id"])
        op = _Operation(client, ctx, transport, _distinct_csv(rows))

        real_write = svc.write_import_batch
        calls: list[int] = []

        async def _interrupt_after_first_chunk(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("connection lost after the first chunk")
            return await real_write(*a, **k)

        monkeypatch.setattr(svc, "write_import_batch", _interrupt_after_first_chunk)
        try:
            status, _ = await op.run()
        except RuntimeError:
            status = None
        assert len(calls) == 2
        assert status != 200
        monkeypatch.setattr(svc, "write_import_batch", real_write)
        await session.rollback()  # the failed request's session closes without committing

        # One import is one transaction: the written first chunk is not durable.
        assert await _item_ids(session, ctx["company_id"]) == before
        assert await _history(client, ctx["admin_h"]) == []

        body = await _run_ok(op)
        assert (body["created"], body["skipped"]) == (rows, 0)
        created_ids = await _item_ids(session, ctx["company_id"]) - before
        assert len(created_ids) == rows
        history = await _history(client, ctx["admin_h"])
        assert [(b["id"], b["row_count"], b["status"]) for b in history] == [(body["batch_id"], rows, "active")]
        (batch,) = await _batch_rows(session, ctx["company_id"])
        assert set(batch.entity_ids) == created_ids

    @pytest.mark.asyncio
    async def test_interrupted_upsert_retry_plans_later_rows_as_the_first_attempt_did(
        self, client, session, ctx, monkeypatch,
    ):
        import celerp_inventory.services as svc
        # The first row and the first row of the second chunk are two new lots sharing
        # a SKU. After an attempt that failed past the first chunk, the retry must
        # still plan the second-chunk row as its
        # own new lot, as the uninterrupted import does, not as an update of row 1's lot.
        rows = [{"name": f"Lot {i:04d}", "sku": f"LOT-{i:04d}", "sell_by": "piece", "quantity": "1"}
                for i in range(_CHUNK + 2)]
        rows[0].update(name="First lot", sku="SHARED")
        rows[_CHUNK].update(name="Second lot", sku="SHARED")
        lots = {"separate_lots": ["SHARED"]}
        key = f"op-{uuid.uuid4().hex}"
        before = await _item_ids(session, ctx["company_id"])

        real_write = svc.write_import_batch
        calls: list[int] = []

        async def _interrupt_after_first_chunk(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("connection lost after the first chunk")
            return await real_write(*a, **k)

        monkeypatch.setattr(svc, "write_import_batch", _interrupt_after_first_chunk)
        with pytest.raises(RuntimeError):
            await _commit_rows(client, ctx["admin_h"], rows, upsert=True, key=key, decisions=lots)
        monkeypatch.setattr(svc, "write_import_batch", real_write)
        await session.rollback()  # the failed request's session closes without committing
        assert await _item_ids(session, ctx["company_id"]) == before

        body = await _commit_rows(client, ctx["admin_h"], rows, upsert=True, key=key, decisions=lots)
        assert (body["created"], body["updated"], body["skipped"]) == (_CHUNK + 2, 0, 0)
        assert len(await _item_ids(session, ctx["company_id"]) - before) == _CHUNK + 2
        assert await _names_with_sku(session, ctx["company_id"], "SHARED") == ["First lot", "Second lot"]

    @pytest.mark.asyncio
    async def test_exact_retry_of_upsert_with_a_repeated_new_sku_returns_the_same_batch(self, client, session, ctx):
        rows = [{"name": name, "sku": "TWIN", "sell_by": "piece", "quantity": "1"} for name in ("Lot A", "Lot B")]
        lots = {"separate_lots": ["TWIN"]}
        first = await _commit_rows(client, ctx["admin_h"], rows, upsert=True, key=None, decisions=lots)
        assert first["created"] == 2 and first["batch_id"]
        after_first = await _snapshot(session, ctx["company_id"])

        again = await _commit_rows(client, ctx["admin_h"], rows, upsert=True, key=None, decisions=lots)
        assert (again["created"], again["updated"], again["skipped"]) == (0, 0, 2)
        assert again["batch_id"] == first["batch_id"]
        assert await _snapshot(session, ctx["company_id"]) == after_first

    @pytest.mark.asyncio
    async def test_same_key_with_different_rows_is_imported_not_treated_as_a_retry(self, client, session, ctx):
        h = ctx["admin_h"]
        key = f"op-{uuid.uuid4().hex}"
        first = await _commit_rows(client, h, [{"name": "Apple", "sell_by": "piece", "quantity": "1"}],
                                   upsert=False, key=key)
        second = await _commit_rows(client, h, [{"name": "Banana", "sell_by": "piece", "quantity": "7"}],
                                    upsert=False, key=key)
        assert second["created"] == 1 and second["batch_id"] != first["batch_id"]
        assert {b["id"] for b in await _history(client, h)} == {first["batch_id"], second["batch_id"]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_undo_then_reimport_creates_a_new_active_history_operation(self, client, session, ctx, transport):
        rows = _CHUNK + 1
        before = await _item_ids(session, ctx["company_id"])
        op = _Operation(client, ctx, transport, _distinct_csv(rows))
        first = await _run_ok(op)
        r = await client.post(f"/items/import/batches/{first['batch_id']}/undo", headers=ctx["admin_h"])
        assert r.status_code == 200, r.text
        assert await _item_ids(session, ctx["company_id"]) == before

        again = await _run_ok(op)
        assert again["created"] == rows
        assert again["batch_id"] and again["batch_id"] != first["batch_id"]
        assert len(await _item_ids(session, ctx["company_id"]) - before) == rows
        history = await _history(client, ctx["admin_h"])
        assert sorted((b["id"], b["row_count"], b["status"]) for b in history) == sorted([
            (first["batch_id"], rows, "undone"), (again["batch_id"], rows, "active"),
        ])

        # Undo released the old history entry's operation; the new entry holds it.
        batches = {str(b.id): b for b in await _batch_rows(session, ctx["company_id"])}
        assert batches[first["batch_id"]].operation_key is None
        assert batches[again["batch_id"]].operation_key

        r = await client.post(f"/items/import/batches/{again['batch_id']}/undo", headers=ctx["admin_h"])
        assert r.status_code == 200 and r.json()["removed"] == rows, r.text
        assert await _item_ids(session, ctx["company_id"]) == before


# ---------------------------------------------------------------------------
# The raw event batch endpoint keeps per-call history
# ---------------------------------------------------------------------------


def _raw_records(count: int, tag: str) -> list[dict]:
    return [
        {
            "entity_id": f"item:raw-{tag}-{i}",
            "entity_type": "item",
            "event_type": EventType.ITEM_CREATED,
            "data": {"sku": f"RAW-{tag}-{i}", "name": f"Raw {tag} {i}", "sell_by": "piece", "quantity": 1},
            "idempotency_key": f"raw:{tag}:{i}",
            "source": "csv_import",
            "source_ts": None,
        }
        for i in range(count)
    ]


class TestRawBatchHistory:
    @pytest.mark.asyncio
    async def test_raw_batch_import_keeps_per_call_history(self, client, session, ctx):
        h = ctx["admin_h"]
        tag_a, tag_b = uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
        first = await client.post("/items/import/batch", headers=h,
                                  json={"records": _raw_records(3, tag_a), "filename": "a.csv"})
        second = await client.post("/items/import/batch", headers=h,
                                   json={"records": _raw_records(2, tag_b), "filename": "b.csv"})
        assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
        a, b = first.json(), second.json()
        assert (a["created"], b["created"]) == (3, 2)
        assert a["batch_id"] and b["batch_id"] and a["batch_id"] != b["batch_id"]

        history = await _history(client, h)
        assert sorted((x["id"], x["row_count"], x["filename"], x["status"]) for x in history) == sorted([
            (a["batch_id"], 3, "a.csv", "active"), (b["batch_id"], 2, "b.csv", "active"),
        ])

        # An exact retry of one raw call writes nothing and records no new history.
        before = await _snapshot(session, ctx["company_id"])
        retry = await client.post("/items/import/batch", headers=h,
                                  json={"records": _raw_records(3, tag_a), "filename": "a.csv"})
        assert retry.status_code == 200 and retry.json()["created"] == 0, retry.text
        assert await _snapshot(session, ctx["company_id"]) == before

        # A raw call cannot show it only added items, so it cannot be undone.
        assert {x["id"]: x["reversible"] for x in history} == {a["batch_id"]: False, b["batch_id"]: False}
        r = await client.post(f"/items/import/batches/{a['batch_id']}/undo", headers=h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "import_not_reversible", r.text
        assert await _snapshot(session, ctx["company_id"]) == before


# ---------------------------------------------------------------------------
# Equal-looking create rows stay distinct rows
# ---------------------------------------------------------------------------


class TestRowIdentity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_identical_create_rows_have_distinct_row_identity(self, client, session, ctx, transport):
        from sqlalchemy import select

        from celerp.models.ledger import LedgerEntry
        before = await _item_ids(session, ctx["company_id"])
        body = await _run_ok(_Operation(client, ctx, transport, _csv(["Twin", "Twin"])))
        assert body["created"] == 2
        created = await _item_ids(session, ctx["company_id"]) - before
        assert len(created) == 2
        keys = (await session.execute(
            select(LedgerEntry.idempotency_key).where(
                LedgerEntry.company_id == uuid.UUID(ctx["company_id"]),
                LedgerEntry.entity_id.in_(created),
                LedgerEntry.event_type == EventType.ITEM_CREATED,
            )
        )).scalars().all()
        assert len(keys) == 2 and len(set(keys)) == 2
        (batch,) = [b for b in await _batch_rows(session, ctx["company_id"]) if str(b.id) == body["batch_id"]]
        assert set(batch.entity_ids) == created

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_exact_whole_import_retry_does_not_duplicate_either_row(self, client, session, ctx, transport):
        before = await _item_ids(session, ctx["company_id"])
        op = _Operation(client, ctx, transport, _csv(["Twin", "Twin"]))
        first = await _run_ok(op)
        assert first["created"] == 2
        after_first = await _snapshot(session, ctx["company_id"])

        second = await _run_ok(op)
        assert (second["created"], second["skipped"]) == (0, 2)
        assert await _snapshot(session, ctx["company_id"]) == after_first
        assert len(await _item_ids(session, ctx["company_id"]) - before) == 2


# ---------------------------------------------------------------------------
# Import and undo stay behind the canonical permission gate
# ---------------------------------------------------------------------------


_GATED = ["import_export_data", "edit_inventory"]


async def _revoke_from_manager(client, ctx, perm_key: str) -> None:
    """Hold the permission above the manager role, through the permission matrix."""
    await grant_permission(client, ctx["admin_h"], perm_key, "admin")


async def _restore_to_manager(client, ctx, perm_key: str) -> None:
    await grant_permission(client, ctx["admin_h"], perm_key, "manager")


class TestImportPermissions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("perm_key", _GATED)
    async def test_import_rows_without_permission_is_denied(self, client, session, ctx, perm_key):
        h = ctx["manager_h"]
        rows = _rows(_csv(["Gated A", "Gated B"]))
        body = {"rows": rows, "upsert": False, "idempotency_key": f"op-{uuid.uuid4().hex}"}

        await _revoke_from_manager(client, ctx, perm_key)
        before = await _snapshot(session, ctx["company_id"])
        denied = await client.post("/items/import/rows", headers=h, json=body)
        assert denied.status_code == 403, denied.text
        assert await _snapshot(session, ctx["company_id"]) == before

        await _restore_to_manager(client, ctx, perm_key)
        allowed = await client.post("/items/import/rows", headers=h, json=body)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["created"] == 2 and allowed.json()["batch_id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("perm_key", _GATED)
    async def test_file_import_commit_without_permission_is_denied(self, client, session, ctx, perm_key):
        h = ctx["manager_h"]
        file_id = _write_upload(ctx, _csv(["Filed A", "Filed B"]), role="manager")
        preview = await client.post("/items/import/preview", headers=h, json={"file_id": file_id})
        assert preview.status_code == 200 and preview.json()["errors"] == [], preview.text
        commit = {"file_id": file_id, "preview_hash": preview.json()["preview_hash"]}

        await _revoke_from_manager(client, ctx, perm_key)
        before = await _snapshot(session, ctx["company_id"])
        denied = await client.post("/items/import/commit", headers=h, json=commit)
        assert denied.status_code == 403, denied.text
        assert await _snapshot(session, ctx["company_id"]) == before

        await _restore_to_manager(client, ctx, perm_key)
        allowed = await client.post("/items/import/commit", headers=h, json=commit)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["created"] == 2 and allowed.json()["batch_id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("perm_key", _GATED)
    async def test_import_history_undo_without_permission_is_denied(self, client, session, ctx, perm_key):
        before_items = await _item_ids(session, ctx["company_id"])
        body = await _run_ok(_Operation(client, ctx, "rows", _csv(["Undo A", "Undo B"])))
        batch_id = body["batch_id"]
        h = ctx["manager_h"]

        await _revoke_from_manager(client, ctx, perm_key)
        before = await _snapshot(session, ctx["company_id"])
        denied = await client.post(f"/items/import/batches/{batch_id}/undo", headers=h)
        assert denied.status_code == 403, denied.text
        assert await _snapshot(session, ctx["company_id"]) == before

        await _restore_to_manager(client, ctx, perm_key)
        allowed = await client.post(f"/items/import/batches/{batch_id}/undo", headers=h)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["removed"] == 2
        assert await _item_ids(session, ctx["company_id"]) == before_items


# ---------------------------------------------------------------------------
# An import that has done more than create its items never becomes undoable again
# ---------------------------------------------------------------------------


async def _category_schema_keys(session, company_id: str, category: str) -> set[str]:
    from celerp.models.company import Company
    session.expire_all()
    company = await session.get(Company, uuid.UUID(company_id))
    return {f["key"] for f in ((company.settings or {}).get("category_schemas") or {}).get(category) or []}


@pytest.mark.asyncio
async def test_a_retry_that_adds_category_fields_makes_the_import_not_undoable(client, session, ctx):
    """A role without settings authority imports an item with a new attribute column: the
    import only created the item, so it can be undone. The same import retried with settings
    authority creates nothing new but adds the column to the category's fields, which Undo
    would leave behind; from then on the import cannot be undone."""
    rows = [{"name": "Ring", "sell_by": "piece", "quantity": "1", "category": "Rings", "band_metal": "gold"}]
    key = f"op-{uuid.uuid4().hex}"

    first = await _commit_rows(client, ctx["manager_h"], rows, upsert=False, key=key)
    assert (first["created"], first["reversible"]) == (1, True)
    assert "band_metal" not in await _category_schema_keys(session, ctx["company_id"], "Rings")

    retry = await _commit_rows(client, ctx["admin_h"], rows, upsert=False, key=key)
    assert (retry["created"], retry["updated"], retry["batch_id"]) == (0, 0, first["batch_id"])
    assert "band_metal" in await _category_schema_keys(session, ctx["company_id"], "Rings")
    assert retry["reversible"] is False
    assert {b["id"]: b["reversible"] for b in await _history(client, ctx["admin_h"])} == {first["batch_id"]: False}

    items = await _item_ids(session, ctx["company_id"])
    undo = await client.post(f"/items/import/batches/{first['batch_id']}/undo", headers=ctx["admin_h"])
    assert undo.status_code == 409 and undo.json()["detail"]["code"] == "import_not_reversible", undo.text
    assert await _item_ids(session, ctx["company_id"]) == items
