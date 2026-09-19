# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Route tests for the agent item importer: preview, commit, and the authz matrix.

The agent uploads a file, previews the suggested mapping and any row errors, then
commits by echoing the preview hash. These cover the company file guard, the
stale-hash refusal, validation refusal, idempotency, and that every import
transport is gated on ``import_export_data`` rather than plain view access.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from celerp.ai.files import upload_dir
from celerp.models.projections import Projection
from test_helpers import perm_setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def write_upload():
    """Factory writing an ai_uploads file pair, cleaned up afterwards.

    Returns a callable ``(company_id, text, filename) -> file_id`` matching the
    ``ai_up_<32 hex>`` id shape the routes validate before touching the disk.
    """
    created: list[str] = []
    d = upload_dir()

    def _write(company_id, text: str, *, filename: str = "items.csv") -> str:
        file_id = f"ai_up_{uuid.uuid4().hex}"
        (d / f"{file_id}.bin").write_bytes(text.encode("utf-8"))
        (d / f"{file_id}.meta").write_text(json.dumps({
            "filename": filename,
            "content_type": "text/csv",
            "company_id": str(company_id),
        }))
        created.append(file_id)
        return file_id

    yield _write

    for file_id in created:
        (d / f"{file_id}.bin").unlink(missing_ok=True)
        (d / f"{file_id}.meta").unlink(missing_ok=True)


async def _company_id(client, headers) -> str:
    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


_GOOD_CSV = "sku,name,sell_by,quantity,retail_price\nAGENT-1,Agent Widget,piece,3,10\n"
_BAD_CSV = "sku,name,sell_by,quantity\nBAD-1,Bad Item,,3\n"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preview_returns_mapping_and_hash(client, session, write_upload):
    s = await perm_setup(client, session)
    admin = s["admin_h"]
    company_id = await _company_id(client, admin)
    file_id = write_upload(company_id, _GOOD_CSV)

    r = await client.get("/items/import/preview", params={"file_id": file_id}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["sku", "name", "sell_by", "quantity", "retail_price"]
    assert body["mapping"]["retail_price"] == "retail_price"
    assert body["mapping"]["sell_by"] == "sell_by"
    assert body["unmapped_required"] == []
    assert body["row_count"] == 1
    assert len(body["sample"]) == 1
    assert body["errors"] == []
    assert len(body["preview_hash"]) == 64


@pytest.mark.asyncio
async def test_preview_other_companys_file_is_not_found(client, session, write_upload):
    s = await perm_setup(client, session)
    admin = s["admin_h"]
    other_company = uuid.uuid4()
    file_id = write_upload(other_company, _GOOD_CSV)

    r = await client.get("/items/import/preview", params={"file_id": file_id}, headers=admin)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_stale_hash_is_conflict(client, session, write_upload):
    s = await perm_setup(client, session)
    admin = s["admin_h"]
    company_id = await _company_id(client, admin)
    file_id = write_upload(company_id, _GOOD_CSV)

    r = await client.post("/items/import/commit", headers=admin, json={
        "file_id": file_id, "preview_hash": "0" * 64,
    })
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "preview_stale"


@pytest.mark.asyncio
async def test_commit_validation_errors_are_unprocessable(client, session, write_upload):
    s = await perm_setup(client, session)
    admin = s["admin_h"]
    company_id = await _company_id(client, admin)
    file_id = write_upload(company_id, _BAD_CSV)

    preview = await client.get("/items/import/preview", params={"file_id": file_id}, headers=admin)
    assert preview.status_code == 200, preview.text
    assert preview.json()["errors"], "the empty sell_by must be flagged"

    r = await client.post("/items/import/commit", headers=admin, json={
        "file_id": file_id, "preview_hash": preview.json()["preview_hash"],
    })
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "validation_failed"
    assert r.json()["detail"]["errors"]


@pytest.mark.asyncio
async def test_commit_idempotent_on_repeated_key(client, session, write_upload):
    s = await perm_setup(client, session)
    admin = s["admin_h"]
    company_id = await _company_id(client, admin)
    file_id = write_upload(company_id, _GOOD_CSV)

    preview = await client.get("/items/import/preview", params={"file_id": file_id}, headers=admin)
    preview_hash = preview.json()["preview_hash"]

    first = await client.post("/items/import/commit", headers=admin, json={
        "file_id": file_id, "preview_hash": preview_hash, "idempotency_key": "agent-batch-1",
    })
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 1

    second = await client.post("/items/import/commit", headers=admin, json={
        "file_id": file_id, "preview_hash": preview_hash, "idempotency_key": "agent-batch-1",
    })
    assert second.status_code == 200, second.text
    assert second.json()["created"] == 0
    assert second.json()["skipped"] == 1

    skus = [p.state.get("sku") for p in (await session.execute(
        select(Projection).where(
            Projection.company_id == uuid.UUID(company_id),
            Projection.entity_type == "item",
        )
    )).scalars().all()]
    # The repeated commit created no second copy: AGENT-1 exists exactly once.
    assert skus.count("AGENT-1") == 1


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_transports_require_import_export_data(client, session, write_upload):
    """view_inventory alone (operator) is refused on every import transport;
    import_export_data (owner) reaches them."""
    s = await perm_setup(client, session)
    admin = s["admin_h"]
    operator = s["operator_h"]  # holds view_inventory, not import_export_data
    company_id = await _company_id(client, admin)
    file_id = write_upload(company_id, _GOOD_CSV)

    # Every transport refuses the operator with 403.
    assert (await client.get("/items/import/preview", params={"file_id": file_id}, headers=operator)).status_code == 403
    assert (await client.post("/items/import/commit", headers=operator, json={
        "file_id": file_id, "preview_hash": "0" * 64,
    })).status_code == 403
    assert (await client.post("/items/import/rows", headers=operator, json={"rows": []})).status_code == 403
    assert (await client.post("/items/import/batch", headers=operator, json={"records": []})).status_code == 403
    assert (await client.get("/items/import/batches", headers=operator)).status_code == 403
    assert (await client.post(
        "/items/import/batches/batch:not-real/undo", headers=operator,
    )).status_code == 403

    # import_export_data (owner) reaches the read transports.
    assert (await client.get("/items/import/preview", params={"file_id": file_id}, headers=admin)).status_code == 200
    assert (await client.get("/items/import/batches", headers=admin)).status_code == 200
