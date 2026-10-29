# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Work a request starts in the background (the delivery receipt of an emailed
document, the free email quota refresh) is held until it finishes, so it is never
dropped mid-run and can be waited for."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from celerp.services import background
from test_helpers import settle_background


def _started_since(before: set) -> set:
    return set(background._BG_TASKS) - before


async def test_an_emailed_document_receipt_is_held_until_it_is_written(monkeypatch):
    from celerp.notifications import service as notif_service
    from celerp_docs import routes

    receipts = []

    async def _refused(**kwargs):
        return False, "refused"

    async def _create(session, company_id, kind, title, body, **kwargs):
        receipts.append(title)

    class _Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    monkeypatch.setattr("celerp.services.email.send_email", _refused)
    monkeypatch.setattr(notif_service, "create", _create)
    monkeypatch.setattr("celerp.db.SessionLocal", _session_local)

    before = set(background._BG_TASKS)
    routes._email_with_receipt(uuid.uuid4(), "Invoice INV-1", "buyer@example.com", "/docs/1",
                               to="buyer@example.com", subject="Invoice", body="Hello")
    assert _started_since(before)
    await settle_background()
    assert receipts == ["Email to buyer@example.com failed"]


async def test_the_free_email_quota_refresh_is_held_until_it_lands(monkeypatch):
    from ui.routes import documents

    async def _methods(token):
        return {"free_email_quota": 4}

    monkeypatch.setattr("ui.api_client.account_methods", _methods)
    monkeypatch.setattr(documents, "_free_send_quota_cache",
                        {"value": 0, "fetched_at": None, "pending": False})

    before = set(background._BG_TASKS)
    documents._free_send_quota("tok")
    assert _started_since(before)
    await settle_background()
    assert documents._free_send_quota_cache["value"] == 4
    assert documents._free_send_quota_cache["pending"] is False
