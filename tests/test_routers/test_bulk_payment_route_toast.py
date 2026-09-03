# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The bulk-payment UI route must surface skipped documents to the user.

bulk_payment returns which docs it paid and which it skipped (concurrently closed or
already paid). A partial run that reports success with no word of the skipped docs leaves
the user believing every selected doc was paid. The route must fire an unmissable toast
naming each skipped doc and its reason when the API response carries a non-empty
``skipped`` list, and keep the plain 204 redirect only when nothing was skipped.

This drives the real ``bulk_payment_route`` closure with a stub API client, so it needs no
database. It is red against a route that discards the API result and blind-redirects."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import ui.routes.documents as documents


def _build_client(monkeypatch, *, api_result):
    """Register the documents routes on a throwaway app, stub the API and auth, and return a
    TestClient. ``api_result`` is what the stubbed ``api.bulk_payment`` returns."""
    from fasthtml.common import FastHTML

    app = FastHTML()
    documents.setup_routes(app)

    async def _fake_bulk_payment(token, doc_ids, amount, payment_date=None, method=None,
                                 bank_account=None, reference=None):
        return api_result

    monkeypatch.setattr(documents, "_token", lambda request: "test-token")
    monkeypatch.setattr(documents.api, "bulk_payment", _fake_bulk_payment)
    return TestClient(app)


def test_bulk_payment_route_surfaces_skipped_docs(monkeypatch):
    """A non-empty ``skipped`` list produces a toast naming the skipped doc, not a blind
    204 redirect."""
    result = {
        "allocations": [{"doc_id": "doc:paid-1", "amount": 50.0}],
        "skipped": [{"doc_id": "doc:skip-9", "reason": "Cannot record payment in current status"}],
        "total_allocated": 50.0,
        "remaining": 50.0,
    }
    client = _build_client(monkeypatch, api_result=result)

    resp = client.post("/docs/bulk-payment",
                       data={"doc_ids": "doc:paid-1,doc:skip-9", "amount": "100",
                             "payment_date": "2026-07-03", "bank_account": "1111",
                             "doc_type": "invoice"})

    # The route must NOT blind-redirect on a partial run: no HX-Redirect, and the skipped
    # doc id must be named to the user (via the toast the _action_error pattern renders).
    assert "HX-Redirect" not in resp.headers, (
        "a partial bulk run must not blind-redirect as if it fully succeeded; "
        f"headers={dict(resp.headers)!r}")
    body_and_headers = resp.text + " " + " ".join(f"{k}:{v}" for k, v in resp.headers.items())
    assert "doc:skip-9" in body_and_headers, (
        "the skipped doc id must be surfaced to the user in the response; "
        f"status={resp.status_code}, headers={dict(resp.headers)!r}, body={resp.text!r}")


def test_bulk_payment_route_redirects_when_nothing_skipped(monkeypatch):
    """With no skips, the route keeps its plain 204 HX-Redirect refresh, unchanged."""
    result = {
        "allocations": [{"doc_id": "doc:paid-1", "amount": 100.0}],
        "skipped": [],
        "total_allocated": 100.0,
        "remaining": 0.0,
    }
    client = _build_client(monkeypatch, api_result=result)

    resp = client.post("/docs/bulk-payment",
                       data={"doc_ids": "doc:paid-1", "amount": "100",
                             "payment_date": "2026-07-03", "bank_account": "1111",
                             "doc_type": "invoice"},
                       follow_redirects=False)

    assert resp.status_code == 204
    assert resp.headers.get("HX-Redirect") == "/docs?type=invoice"
