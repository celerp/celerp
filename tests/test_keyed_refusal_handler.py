# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A keyed refusal raised anywhere in a request answers 409 with its key and its text, never
a server error, and the refused request stores nothing."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

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


async def test_a_refusal_with_an_amount_or_a_date_is_a_json_body():
    """An amount and a date in a refusal's params travel as text, so answering it never fails
    to serialise (the route would answer 500 instead of the refusal)."""
    detail = Refused("error.stripe_amount", value=Decimal("1000.50"), code="CLP").detail
    body = json.loads(json.dumps(detail))
    assert body["params"] == {"value": "1000.50", "code": "CLP"}
    assert body["message"].startswith("1000.50 CLP ")
    assert json.loads(json.dumps(Refused("error.period_locked", date=date(2026, 1, 31),
                                         permission="x", place="y").detail))["params"]["date"] == "2026-01-31"


async def test_a_keyed_refusal_keeps_the_error_body_fields_beside_it():
    """A refusal answered with a machine code and version beside it reaches the UI with
    both: the scan bar and the company timezone notice branch on the code."""
    from celerp.accounting_roles import catalog_refusal
    from ui.api_client import _api_error

    detail = catalog_refusal("documents.err_scan_conflict")
    err = _api_error(409, {"detail": detail, "code": "scan_run_conflict", "version": 7}, "")
    assert err.data["code"] == "scan_run_conflict" and err.data["version"] == 7
    assert err.data["message_key"] == detail["message_key"]
    assert err.detail == detail["message"]
