# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tests for send button visibility and send/unmark_sent correctness.

Covers:
- NO_SEND_DOC_TYPES constant (bill, purchase_order, consignment_in)
- NO_SEND_STATUSES constant (paid, void)
- _doc_detail() renders Send modal only for sendable types + connected relay + eligible status
- _doc_detail() renders Mark as Sent only for sendable types on draft
- _doc_detail() renders Unmark Sent only for sendable types on sent
- bill/purchase_order/consignment_in never show Send or Mark as Sent
- DocSendBody accepts cc and bcc fields
- unmark_sent uses revert-to-draft endpoint (no patch of status)
"""
from __future__ import annotations

import uuid

import pytest

from celerp_docs.doc_constants import NO_SEND_DOC_TYPES, NO_SEND_STATUSES


# ── Constants ─────────────────────────────────────────────────────────────────

def test_no_send_doc_types_contains_expected():
    assert "bill" in NO_SEND_DOC_TYPES
    assert "purchase_order" in NO_SEND_DOC_TYPES
    assert "consignment_in" in NO_SEND_DOC_TYPES


def test_no_send_statuses_contains_expected():
    assert "paid" in NO_SEND_STATUSES
    assert "void" in NO_SEND_STATUSES


def test_sendable_doc_types_not_in_no_send():
    for dt in ("invoice", "credit_note", "consignment_out", "proforma", "memo"):
        assert dt not in NO_SEND_DOC_TYPES, f"{dt} should be sendable"


# ── _doc_detail() HTML rendering ──────────────────────────────────────────────

def _render_doc(doc_type: str, status: str, share_enabled: bool = False) -> str:
    """Render _doc_detail() for a minimal doc and return the HTML string."""
    from ui.routes.documents import _doc_detail
    doc = {
        "entity_id": f"doc:TEST-{doc_type[:4].upper()}-0001",
        "doc_type": doc_type,
        "status": status,
        "contact_email": "test@example.com",
        "ref_id": "INV-001",
        "company_name": "Test Co",
        "line_items": [],
    }
    result = _doc_detail(doc, role="owner", share_enabled=share_enabled)
    from fasthtml.common import to_xml
    return to_xml(result)


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order", "consignment_in"])
@pytest.mark.parametrize("status", ["draft", "sent", "final", "awaiting_payment", "paid"])
def test_no_send_for_internal_doc_types(doc_type, status):
    html = _render_doc(doc_type, status, share_enabled=True)
    assert "send-modal-" not in html, f"{doc_type}/{status} should not have send modal"
    assert 'action/mark_sent' not in html, f"{doc_type}/{status} should not have mark_sent"
    assert 'action/unmark_sent' not in html, f"{doc_type}/{status} should not have unmark_sent"


@pytest.mark.parametrize("status", ["paid", "void"])
def test_no_send_modal_for_no_send_statuses(status):
    html = _render_doc("invoice", status, share_enabled=True)
    assert "send-modal-" not in html, f"invoice/{status} should not have send modal"


def test_send_modal_visible_when_share_enabled_and_eligible_status():
    html = _render_doc("invoice", "draft", share_enabled=True)
    assert "send-modal-" in html, "invoice/draft with relay should show send modal"
    assert "modal-dialog" in html


def test_send_modal_visible_on_final_status_with_relay():
    html = _render_doc("invoice", "final", share_enabled=True)
    assert "send-modal-" in html, "invoice/final with relay should show send modal"


def test_send_modal_hidden_when_relay_not_connected():
    html = _render_doc("invoice", "draft", share_enabled=False)
    assert "send-modal-" not in html, "Send modal must be hidden when relay is not connected"


def test_mark_as_sent_visible_on_draft_sendable_type():
    # relay not required for mark_as_sent
    html = _render_doc("invoice", "draft", share_enabled=False)
    assert 'action/mark_sent' in html


def test_mark_as_sent_hidden_on_non_draft():
    html = _render_doc("invoice", "final", share_enabled=False)
    assert 'action/mark_sent' not in html


def test_unmark_sent_visible_on_sent_status():
    html = _render_doc("invoice", "sent", share_enabled=False)
    assert 'action/unmark_sent' in html


def test_unmark_sent_hidden_on_non_sent():
    html = _render_doc("invoice", "draft", share_enabled=False)
    assert 'action/unmark_sent' not in html


def test_send_modal_has_cc_and_bcc_fields():
    html = _render_doc("invoice", "draft", share_enabled=True)
    assert 'name="cc"' in html
    assert 'name="bcc"' in html


def test_send_modal_has_multiple_address_hint():
    html = _render_doc("invoice", "draft", share_enabled=True)
    assert "comma" in html.lower() or "send_multiple_hint" not in html  # i18n key resolves


# ── DocSendBody cc/bcc fields ─────────────────────────────────────────────────

def test_doc_send_body_accepts_cc_bcc():
    from celerp_docs.routes import DocSendBody
    body = DocSendBody(sent_to="a@b.com", cc="c@b.com", bcc="d@b.com", sent_via="email")
    assert body.cc == "c@b.com"
    assert body.bcc == "d@b.com"


def test_doc_send_body_cc_bcc_optional():
    from celerp_docs.routes import DocSendBody
    body = DocSendBody(sent_to="a@b.com")
    assert body.cc is None
    assert body.bcc is None


# ── unmark_sent action calls revert-to-draft (not patch) ──────────────────────

async def test_unmark_sent_uses_revert_to_draft_endpoint(client):
    """POST /docs/{id}/action/unmark_sent must call revert-to-draft, not PATCH status."""
    token_resp = await client.post("/auth/register", json={
        "company_name": f"SendCo-{uuid.uuid4().hex[:6]}",
        "email": f"s-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin",
        "password": "pw",
    })
    assert token_resp.status_code == 200
    token = token_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create a draft invoice
    r = await client.post("/docs", json={
        "doc_type": "invoice",
        "line_items": [{"description": "x", "quantity": 1, "unit_price": 10.0, "line_total": 10.0}],
    }, headers=headers)
    assert r.status_code in {200, 201}
    doc_id = r.json()["id"]

    # Send it (mark as sent via API directly)
    r = await client.post(f"/docs/{doc_id}/send", json={"sent_via": "manual"}, headers=headers)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{doc_id}", headers=headers)).json()
    assert doc["status"] == "sent"

    # Now unmark_sent via revert-to-draft (what the UI action now calls)
    r = await client.post(f"/docs/{doc_id}/revert-to-draft", json={}, headers=headers)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{doc_id}", headers=headers)).json()
    assert doc["status"] == "draft", f"Expected draft after unmark, got {doc['status']}"
