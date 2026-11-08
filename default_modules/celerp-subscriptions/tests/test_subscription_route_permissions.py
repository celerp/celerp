# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every subscriptions endpoint's required permission, declared once and asserted.

Anything that decides whether a period bills answers to finalize_documents, the permission
billing itself needs: skipping a period, opening it again, placing an earlier document as a
period's bill, setting one aside and withdrawing either. Otherwise a role that may not bill
could still stop a period from billing or make it bill again. A new endpoint with no entry
below fails, so its permission is a decision, not an oversight.
"""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import agreement, freeze_day, member, register, revoke, template
from celerp_subscriptions.routes import _build_router

VIEW = "view_subscriptions"
EDIT = "edit_documents"
BILL = "finalize_documents"

EXPECTED: dict[tuple[str, str], str] = {
    ("GET", ""): VIEW,
    ("GET", "/due"): VIEW,
    ("GET", "/catch-up"): VIEW,
    ("GET", "/summary"): VIEW,
    ("POST", "/summary/ack"): VIEW,
    ("GET", "/{entity_id}/periods"): VIEW,
    ("POST", "/generate-due"): BILL,
    ("POST", "/catch-up/run"): BILL,
    ("POST", "/{entity_id}/generate-due"): BILL,
    ("POST", "/{entity_id}/bill-early"): BILL,
    ("POST", "/{entity_id}/periods/{k}/skip"): BILL,
    ("POST", "/{entity_id}/periods/{k}/unskip"): BILL,
    ("POST", "/{entity_id}/periods/{k}/reopen"): BILL,
    ("POST", "/{entity_id}/history/{doc_id}/confirm"): BILL,
    ("POST", "/{entity_id}/history/{doc_id}/set-aside"): BILL,
    ("POST", "/{entity_id}/history/{doc_id}/withdraw"): BILL,
    ("POST", "/{entity_id}/periods/{k}/send"): EDIT,
    ("PATCH", "/{entity_id}/schedule"): EDIT,
    ("PATCH", "/{entity_id}/lines"): EDIT,
    ("POST", "/{entity_id}/pause"): EDIT,
    ("POST", "/{entity_id}/resume"): EDIT,
    ("POST", "/{entity_id}/cancel"): EDIT,
    ("POST", "/{entity_id}/activate"): EDIT,
}


def _declared_permissions(route) -> frozenset[str]:
    """The permissions a route requires, read off its dependency callables."""
    seen = set()
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        dep = stack.pop()
        val = getattr(getattr(dep, "call", None), "required_permission", None)
        if isinstance(val, str):
            seen.add(val)
        stack.extend(getattr(dep, "dependencies", []) or [])
    return frozenset(seen)


def test_every_endpoint_requires_exactly_the_permission_declared_for_it():
    found = {}
    for route in _build_router().routes:
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            found[(method, route.path)] = _declared_permissions(route)
    assert sorted(found) == sorted(EXPECTED), "an endpoint was added or removed without a permission decision"
    wrong = {k: sorted(got) for k, got in found.items() if got != frozenset({EXPECTED[k]})}
    assert not wrong, f"endpoints whose permission differs from the table: {wrong}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path, body", [
    ("POST", "/periods/0/skip", {"reason": "No charge"}),
    ("POST", "/periods/0/unskip", None),
    ("POST", "/periods/0/reopen", None),
    ("POST", "/history/{doc}/confirm", {"period_index": 0}),
    ("POST", "/history/{doc}/set-aside", None),
    ("POST", "/history/{doc}/withdraw", None),
])
async def test_a_role_that_may_not_bill_cannot_decide_whether_a_period_bills(
        client, session, monkeypatch, method, path, body):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-07-01")
    await revoke(client, hd, BILL, "operator")
    op, _ = await member(client, session, hd, "operator")
    url = f"/subscriptions/{eid}" + path.format(doc=f"doc:{uuid.uuid4().hex}")
    r = await client.request(method, url, headers=op, **({"json": body} if body is not None else {}))
    assert r.status_code == 403, r.text
    assert not (await template(client, hd, eid)).get("periods")
