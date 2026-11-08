# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A keyed refusal raised anywhere in a request answers 409 with its key and its text, never
a server error, and the refused request stores nothing."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import Depends
from sqlalchemy import select

from celerp.accounting_roles import KeyedRefusal, Refused
from celerp.db import get_session
from celerp.inventory_codes import BarcodeConflictError
from celerp.main import app
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


class _Plain(KeyedRefusal):
    """A keyed refusal with no handler of its own."""


@pytest.fixture
def refusing_route():
    """A route that stores a row, then raises the refusal the request names."""
    path = f"/_test/refuse/{uuid.uuid4().hex}"
    entity_id = f"test:{uuid.uuid4().hex}"
    raising = {"refused": lambda: Refused("error.cogs_line_unknown", doc="INV-1", items="STN-1"),
               "plain": lambda: _Plain("error.server_error"),
               "barcode": lambda: BarcodeConflictError("BC-1")}

    async def route(kind: str, company_id: str, session=Depends(get_session)):
        session.add(Projection(company_id=uuid.UUID(company_id), entity_id=entity_id,
                               entity_type="test", state={}, version=1, updated_at=datetime.now(timezone.utc)))
        await session.flush()
        raise raising[kind]()

    app.add_api_route(path + "/{kind}", route, methods=["POST"])
    try:
        yield path, entity_id
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != path + "/{kind}"]


@pytest.mark.parametrize("kind", ["refused", "plain", "barcode"])
async def test_keyed_refusal_never_answers_500(client, session, auth, refusing_route, kind):
    path, entity_id = refusing_route
    r = await client.post(f"{path}/{kind}", params={"company_id": str(auth["company_id"])}, headers=auth["headers"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == {"refused": "error.cogs_line_unknown", "plain": "error.server_error",
                                     "barcode": "inventory.err_barcode_taken"}[kind]
    assert detail["message"]
    session.expire_all()
    assert (await session.execute(select(Projection).where(Projection.entity_id == entity_id))).first() is None
