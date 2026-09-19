# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Live end-to-end agent checks, gated OFF by default.

Normal CI never runs this and never spends: the fake-model suites replay the
recorded relay fixtures so the loop is proven without a live call. This module
drives a real, deployed instance connected to the relay, to prove the whole
chain works before a release, run by hand and its output attached to the review:

    CELERP_LIVE_AI=1 \
    CELERP_INSTANCE_URL=https://<test-instance> \
    CELERP_TEST_EMAIL=... CELERP_TEST_PASSWORD=... \
        python -m pytest default_modules/celerp-ai/tests/test_live_agent.py

The instance must already be connected to Celerp Connect (the API process holds
the session token from its gateway handshake, so an authenticated user request
is all the client sends). A full run is a handful of short prompts on the flash
model, well under one credit of the customer-facing quota.
"""

from __future__ import annotations

import base64
import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CELERP_LIVE_AI") != "1",
    reason="set CELERP_LIVE_AI=1 (with a deployed test instance) to run live agent checks",
)

# A one-page PDF with no meaningful content - enough to exercise the file part.
_TINY_PDF = base64.b64decode(
    "JVBERi0xLjQKJcTl8uXrp/Og0MTGCjEgMCBvYmoKPDwvVHlwZS9DYXRhbG9nPj4KZW5kb2JqCg=="
)
_CSV_CATALOG = b"sku,name,sell_by,quantity,retail_price\nLIVE-1,Live Catalog Widget,piece,4,12\n"


def _instance_url() -> str:
    url = os.environ.get("CELERP_INSTANCE_URL")
    if not url:
        pytest.skip("CELERP_INSTANCE_URL required for live agent checks")
    return url.rstrip("/")


@pytest.fixture
async def client():
    """Authenticated client against the deployed test instance."""
    email = os.environ.get("CELERP_TEST_EMAIL")
    password = os.environ.get("CELERP_TEST_PASSWORD")
    if not (email and password):
        pytest.skip("CELERP_TEST_EMAIL and CELERP_TEST_PASSWORD required")
    async with httpx.AsyncClient(base_url=_instance_url(), timeout=90.0) as c:
        r = await c.post("/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c


async def _conversation(client: httpx.AsyncClient) -> str:
    r = await client.post("/ai/conversations", json={"title": None})
    r.raise_for_status()
    return r.json()["id"]


async def _query(client: httpx.AsyncClient, conv_id: str, text: str, file_ids=None) -> dict:
    body: dict = {"query": text}
    if file_ids:
        body["file_ids"] = file_ids
    r = await client.post(f"/ai/conversations/{conv_id}/query", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _remaining(client: httpx.AsyncClient) -> int:
    r = await client.get("/ai/quota-status")
    r.raise_for_status()
    return r.json()["remaining"]


async def _upload(client: httpx.AsyncClient, filename: str, data: bytes, content_type: str) -> str:
    r = await client.post("/ai/upload", files={"files": (filename, data, content_type)})
    assert r.status_code == 201, r.text
    return r.json()["file_ids"][0]


async def test_live_text_answer(client):
    """A general question comes back as plain prose with no proposed write."""
    conv = await _conversation(client)
    body = await _query(client, conv, "In one sentence, what can you help me do here?")
    assert body["answer"].strip()
    assert body["pending_actions"] == []


async def test_live_read_tool_answers_with_real_data(client):
    """A stock question drives a real read capability and answers from the data."""
    conv = await _conversation(client)
    body = await _query(client, conv, "How many inventory items do I have in total?")
    assert body["tools_called"], "expected at least one read capability to run"
    assert body["answer"].strip()
    assert body["pending_actions"] == []


async def test_live_mutation_returns_pending_then_confirm_creates(client):
    """A write is proposed as a pending action and only lands on confirm."""
    conv = await _conversation(client)
    body = await _query(client, conv, "Add a supplier contact named Live Test Supplier.")
    assert len(body["pending_actions"]) == 1, body
    pa = body["pending_actions"][0]

    confirm = await client.post(
        f"/ai/conversations/{conv}/confirm",
        json={"message_id": pa["message_id"], "tool_call_id": pa["id"]},
    )
    assert confirm.status_code == 200, confirm.text
    result = confirm.json()
    assert result["ok"] is True, result
    assert result["data"]


async def test_live_continuation_charges_one_credit(client):
    """A read-then-answer turn is a single metered call: exactly one credit."""
    before = await _remaining(client)
    conv = await _conversation(client)
    await _query(client, conv, "How many inventory items do I have in total?")
    after = await _remaining(client)
    assert before - after == 1, (before, after)


async def test_live_pdf_question(client):
    """A PDF part reaches the model and produces an answer."""
    conv = await _conversation(client)
    file_id = await _upload(client, "doc.pdf", _TINY_PDF, "application/pdf")
    body = await _query(client, conv, "What does the attached document contain?", file_ids=[file_id])
    assert body["answer"].strip()


async def test_live_csv_import_preview(client):
    """A CSV catalog upload is composed into a preview then a confirmable import."""
    conv = await _conversation(client)
    file_id = await _upload(client, "catalog.csv", _CSV_CATALOG, "text/csv")
    body = await _query(client, conv, "Import this supplier catalog.", file_ids=[file_id])
    assert len(body["pending_actions"]) == 1, body
    assert "import" in body["pending_actions"][0]["name"]


async def test_live_timeout_is_honest(client):
    """A model call that outruns the client deadline surfaces as an honest timeout,
    never an instant fabricated answer. The request really reaches the model - an
    aggressively short read deadline proves the answer is generated live, not
    canned - and the client observes the timeout rather than false content."""
    conv = await _conversation(client)
    with pytest.raises(httpx.TimeoutException):
        await client.post(
            f"/ai/conversations/{conv}/query",
            json={"query": "Summarise every item in my catalogue in detail."},
            timeout=0.5,
        )
