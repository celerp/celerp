# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every write of the docs module that has a preview (a request field ``dry_run``) confirms only
the preview shown: the preview answers with a ``binding``, and the write refuses one that differs
from what it would write now. The previewed writes are found from the router, so a new one with
no case here turns this red."""
from __future__ import annotations

import typing
import uuid

import pytest
from pydantic import BaseModel

from memo_draw_support import auth, key_of, memo_out, ok, state

pytestmark = pytest.mark.asyncio


def _previewed_writes() -> dict[str, type[BaseModel]]:
    """Each docs route whose request body has ``dry_run``, by path, with its body model."""
    from celerp_docs.routes import router

    found = {}
    for route in router.routes:
        for hint in typing.get_type_hints(getattr(route, "endpoint", None) or (lambda: None)).values():
            if isinstance(hint, type) and issubclass(hint, BaseModel) and "dry_run" in hint.model_fields:
                found[route.path] = hint
    return found


async def _draw_case(client, h):
    memo, lids, _ = await memo_out(client, h, [(4, 10.0), (3, 20.0)])
    shown = {"line_ids": [lids[0]], "quantities": {lids[0]: 2}}
    return f"/docs/{memo}/invoice-lines", memo, shown, {**shown, "quantities": {lids[0]: 3}}, "docs.draw_preview_differs"


async def _rate_case(client, h):
    memo, _, _ = await memo_out(client, h, [(1, 100.0), (1, 250.0)], tax=35.0)
    return f"/docs/{memo}/tax-rate", memo, {"tax_rate": 8}, {"tax_rate": 9}, "docs.memo_rate_preview_differs"


# Path of each previewed write: its case (url, memo, request previewed, request changed after, refusal).
_CASES = {"/{entity_id}/invoice-lines": _draw_case, "/{entity_id}/tax-rate": _rate_case}


async def test_every_previewed_write_refuses_a_binding_it_did_not_preview(client, session):
    writes = _previewed_writes()
    assert set(writes) == set(_CASES), "a docs write with a preview needs a case here and a binding its write checks"
    h = await auth(session)
    for path, case in _CASES.items():
        assert "binding" in writes[path].model_fields, path
        url, memo, shown, changed, refusal = await case(client, h)
        preview = await ok(await client.post(url, headers=h, json={**shown, "dry_run": True}))
        assert preview.get("binding"), (path, preview)
        before = await state(client, h, memo)
        for body in ({**changed, "binding": preview["binding"]}, {**shown, "binding": "0" * 64}):
            r = await client.post(url, headers=h, json={**body, "idempotency_key": uuid.uuid4().hex})
            assert r.status_code == 409 and key_of(r) == refusal, (path, body, r.text)
            assert await state(client, h, memo) == before, path
        await ok(await client.post(url, headers=h, json={**shown, "binding": preview["binding"],
                                                         "idempotency_key": uuid.uuid4().hex}))



async def test_every_previewed_write_replays_its_first_result(client, session):
    """A confirmed preview sent again under the same operation key (an answer lost, the request
    retried) answers what the first did and writes nothing more."""
    writes = _previewed_writes()
    h = await auth(session)
    for path, case in _CASES.items():
        assert "idempotency_key" in writes[path].model_fields, path
        url, memo, shown, _changed, _refusal = await case(client, h)
        preview = await ok(await client.post(url, headers=h, json={**shown, "dry_run": True}))
        body = {**shown, "binding": preview["binding"], "idempotency_key": uuid.uuid4().hex}
        first = await ok(await client.post(url, headers=h, json=body))
        after = await state(client, h, memo)
        again = await ok(await client.post(url, headers=h, json=body))
        assert again == first, (path, first, again)
        assert await state(client, h, memo) == after, path
