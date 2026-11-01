# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Saving a document's lines never overwrites a change someone else saved in the meantime.

Saving the lines replaces the document's whole line list, so a save made from an older copy would
drop whatever was saved after that copy was read. A line save therefore carries the version it was
made from: an older version is refused, and a save with no version at all is refused too, both
with code stale_version so the page keeps the user's rows and offers Try again. Saves of single
fields carry no line list and need no version.
"""
from __future__ import annotations

import asyncio
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from test_helpers import remove_company
from test_money_stock_and_contact_invariants import _seed_company, _seed_user
from test_ui import _authed, ui_client  # noqa: F401

_LINE = {"description": "Fitting", "quantity": 1, "unit_price": 100.0}


async def _invoice(client, auth) -> dict:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": "customer:1", "line_items": [dict(_LINE)]})
    assert r.status_code == 200, r.text
    return await _get(client, auth, r.json()["id"])


async def _get(client, auth, doc_id: str) -> dict:
    r = await client.get(f"/docs/{doc_id}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _save_lines(client, auth, doc_id: str, lines: list[dict], version=None):
    body: dict = {"fields_changed": {"line_items": {"new": lines}}}
    if version is not None:
        body["expected_version"] = version
    return await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json=body)


def _stale(r, key: str) -> None:
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert (detail["code"], detail["message_key"]) == ("stale_version", key), detail


@pytest.mark.asyncio
async def test_a_line_save_without_a_version_is_refused_and_writes_nothing(client, auth):
    doc = await _invoice(client, auth)
    r = await _save_lines(client, auth, doc["id"], [{**_LINE, "quantity": 7}])
    _stale(r, "documents.err_doc_stale")
    after = await _get(client, auth, doc["id"])
    assert (after["version"], after["line_items"][0]["quantity"]) == (doc["version"], 1)


@pytest.mark.asyncio
async def test_a_save_from_an_older_copy_is_refused_and_the_first_save_stays(client, auth):
    doc = await _invoice(client, auth)
    first = await _save_lines(client, auth, doc["id"], [{**_LINE, "quantity": 5}], doc["version"])
    assert first.status_code == 200, first.text
    second = await _save_lines(client, auth, doc["id"], [{**_LINE, "unit_price": 120.0}], doc["version"])
    _stale(second, "documents.err_doc_changed")
    line = (await _get(client, auth, doc["id"]))["line_items"][0]
    assert (line["quantity"], line["unit_price"]) == (5, 100.0)


@pytest.mark.asyncio
async def test_a_single_field_save_needs_no_version(client, auth):
    doc = await _invoice(client, auth)
    r = await client.patch(f"/docs/{doc['id']}", headers=auth["headers"],
                           json={"fields_changed": {"customer_note": {"new": "Thanks"}}})
    assert r.status_code == 200, r.text
    assert (await _get(client, auth, doc["id"]))["customer_note"] == "Thanks"


@pytest.mark.asyncio
async def test_two_saves_from_the_same_copy_at_once_write_only_one(_db_engine):
    """Two line saves from the same version in separate transactions: the second waits for the
    first's row lock, then finds the version moved and is refused, so the first's lines stay."""
    from celerp_docs.routes import DocPatch, patch_doc

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    doc_id = f"doc:INV-RACE-{uuid.uuid4().hex[:6]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "status": "draft", "ref_id": doc_id[4:], "currency": "USD",
                  "line_items": [dict(_LINE)], "subtotal": 100.0, "total": 100.0},
            actor_id=user_id, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={})
        await s.commit()
        version = (await s.get(Projection, {"company_id": company_id, "entity_id": doc_id})).version

    def save(**change) -> DocPatch:
        return DocPatch(fields_changed={"line_items": {"new": [{**_LINE, **change}]}}, expected_version=version)

    first, second = factory(), factory()
    release = asyncio.Event()
    commit = first.commit

    async def held_commit() -> None:
        await release.wait()
        await commit()

    first.commit = held_commit
    user = types.SimpleNamespace(id=user_id)
    kw = dict(company_id=company_id, _=None, role="owner", settings={}, user=user)
    try:
        a = asyncio.create_task(patch_doc(doc_id, save(quantity=5), session=first, **kw))
        await asyncio.sleep(0.3)
        b = asyncio.create_task(patch_doc(doc_id, save(unit_price=120.0), session=second, **kw))
        await asyncio.sleep(0.3)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(a, b, return_exceptions=True), timeout=10)
        assert isinstance(results[0], dict), results
        assert isinstance(results[1], HTTPException) and results[1].status_code == 409, results
        assert results[1].detail["code"] == "stale_version"
        async with factory() as s:
            line = (await s.get(Projection, {"company_id": company_id, "entity_id": doc_id})).state["line_items"][0]
            assert (line["quantity"], line["unit_price"]) == (5, 100.0)
    finally:
        await first.close()
        await second.close()
        await remove_company(factory, company_id, user_id)


# The editor's save route: forwards the version the page read and keeps the conflict's status and code.

_SAVE = {"line_items": [dict(_LINE)], "offset": 0, "original_count": 1}


@pytest.mark.asyncio
async def test_the_editor_save_forwards_the_version_the_page_read(ui_client):
    patch_doc = AsyncMock(return_value={"event_id": "e1", "version": 10})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.patch_doc", new=patch_doc), \
            patch("ui.api_client.get_doc", new=AsyncMock(return_value={"currency": "USD", "status": "draft"})):
        r = await ui_client.post("/docs/doc:INV-1/lines", json={**_SAVE, "expected_version": 9})
    assert r.status_code == 200, r.text
    assert patch_doc.call_args.kwargs.get("expected_version") == 9, patch_doc.call_args


@pytest.mark.asyncio
async def test_the_editor_save_returns_a_stale_version_as_409_with_its_code(ui_client):
    from ui.api_client import APIError
    from ui.i18n import t
    detail = {"message": "changed", "message_key": "documents.err_doc_changed", "params": {}, "code": "stale_version"}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.patch_doc", new=AsyncMock(side_effect=APIError(409, "changed", data=detail))):
        r = await ui_client.post("/docs/doc:INV-1/lines", json={**_SAVE, "expected_version": 9})
    assert r.status_code == 409
    assert r.json() == {"code": "stale_version", "error": t("documents.err_doc_changed")}


@pytest.mark.asyncio
async def test_any_other_409_from_the_editor_save_is_not_called_stale(ui_client):
    from ui.api_client import APIError
    detail = {"message": "Locked", "message_key": "docs.contact_credit_fixed", "params": {}}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.patch_doc", new=AsyncMock(side_effect=APIError(409, "Locked", data=detail))):
        r = await ui_client.post("/docs/doc:INV-1/lines", json={**_SAVE, "expected_version": 9})
    assert "code" not in r.json(), r.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["9", 9.5, True, [9]])
async def test_the_editor_save_refuses_a_version_that_is_not_a_whole_number(ui_client, version):
    patch_doc = AsyncMock()
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.patch_doc", new=patch_doc):
        r = await ui_client.post("/docs/doc:INV-1/lines", json={**_SAVE, "expected_version": version})
    assert r.status_code == 400, r.text
    assert patch_doc.await_count == 0
