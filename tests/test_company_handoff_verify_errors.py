# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company copy upload that names its link is checked with the cloud. When the cloud
cannot be reached the user is told so and to try again; only a copy the cloud refuses is
called a mismatch."""

from __future__ import annotations

import uuid

import httpx
import pytest

from company_backup_support import download
from migration_support import real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir, _fresh_install  # noqa: F401

pytestmark = pytest.mark.asyncio

LINK = {"handoff_copy_id": str(uuid.uuid4()), "handoff_proof": "ab" * 32}
_REAL_CLIENT = httpx.AsyncClient


async def _copy(client) -> bytes:
    registered = await client.post("/auth/register", json={
        "company_name": "Demo Trading", "email": "demo-owner@example.test",
        "name": "Demo Owner", "password": "demopw1234"})
    assert registered.status_code == 200, registered.text
    return await download(client, registered.json()["access_token"])


def _cloud(monkeypatch, answer):
    from celerp.services import company_handoff_transfer as transfer
    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(
        transport=httpx.MockTransport(answer), **kw))


async def _read(client, data: bytes):
    return await client.post("/company-backups/bootstrap/read",
                             files={"file": ("demo.celerp-company", data)}, data=LINK)


async def test_an_unreachable_cloud_is_not_called_a_mismatch(real_client, real_engine, monkeypatch):
    data = await _copy(real_client)
    await _fresh_install(real_engine)

    def offline(request):
        raise httpx.ConnectError("offline")
    _cloud(monkeypatch, offline)
    refused = await _read(real_client, data)
    assert refused.status_code == 503
    assert refused.json()["detail"] == (
        "Celerp could not reach the cloud to check this company copy. Nothing was restored. "
        "Check your internet connection and try again.")

    _cloud(monkeypatch, lambda request: httpx.Response(503))
    assert (await _read(real_client, data)).status_code == 503


async def test_a_copy_the_cloud_refuses_is_a_mismatch(real_client, real_engine, monkeypatch):
    data = await _copy(real_client)
    await _fresh_install(real_engine)
    _cloud(monkeypatch, lambda request: httpx.Response(404))
    refused = await _read(real_client, data)
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "This file does not match the company copy its link names. Nothing was restored. "
        "Open the company copy from its link again.")
