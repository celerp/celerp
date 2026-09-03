# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Projection folds for the memo Close/Reopen events.

apply_documents_event must fold doc.closed (status -> closed, stash the prior
status) and doc.reopened (restore the prior status, drop the stash). An
unfolded event type raises ValueError, so the fold is mandatory.
"""
from __future__ import annotations

from celerp_docs.doc_projections import apply_documents_event


def test_doc_closed_reopened_projection():
    base = {"entity_type": "doc", "doc_type": "memo", "status": "final", "fulfillment_status": "partial"}

    closed = apply_documents_event(base, "doc.closed", {"pre_close_status": "final", "reason": "settled"})
    assert closed["status"] == "closed"
    assert closed["pre_close_status"] == "final"
    # Orthogonal fulfillment audit trail is untouched.
    assert closed["fulfillment_status"] == "partial"

    reopened = apply_documents_event(closed, "doc.reopened", {"restored_status": "final"})
    assert reopened["status"] == "final"
    assert "pre_close_status" not in reopened
    assert reopened["fulfillment_status"] == "partial"
