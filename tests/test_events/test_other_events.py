# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest

from celerp_accounting.projections import apply_accounting_event
from celerp_docs.doc_projections import apply_documents_event
from celerp.projections.handlers.marketplace import apply_marketplace_event
from celerp.projections.handlers.scanning import apply_scanning_event
from celerp.projections.handlers.system import apply_system_event


def test_documents_flow() -> None:
    state = apply_documents_event({}, "doc.created", {"title": "T"})
    assert state["status"] == "draft"

    state = apply_documents_event(state, "doc.updated", {"fields_changed": {"title": {"old": "T", "new": "T2"}}})
    assert state["title"] == "T2"

    state = apply_documents_event(state, "doc.linked", {"entity_id": "item:1", "entity_type": "item"})
    assert state["linked"][0]["entity_id"] == "item:1"

    state = apply_documents_event(state, "doc.finalized", {})
    assert state["status"] == "final"

    state = apply_documents_event(state, "doc.voided", {"reason": "x"})
    assert state["status"] == "void"


def test_marketplace_flow() -> None:
    state = apply_marketplace_event({}, "mp.listing.created", {"sku": "S"})
    assert state["status"] == "draft"

    state = apply_marketplace_event(state, "mp.listing.updated", {"fields_changed": {"sku": {"old": "S", "new": "S2"}}})
    assert state["sku"] == "S2"

    state = apply_marketplace_event(state, "mp.listing.published", {})
    assert state["status"] == "published" and state["is_on_marketplace"] is True

    state = apply_marketplace_event(state, "mp.listing.unpublished", {"reason": "x"})
    assert state["is_on_marketplace"] is False

    state = apply_marketplace_event({}, "mp.order.received", {"order_ref": "o1", "items": []})
    assert state["status"] == "received"

    state = apply_marketplace_event(state, "mp.order.fulfilled", {"fulfillment_ref": "f"})
    assert state["status"] == "fulfilled"

    state = apply_marketplace_event(state, "mp.order.cancelled", {"reason": "x"})
    assert state["status"] == "cancelled"


def test_accounting_flow() -> None:
    state = apply_accounting_event({}, "acc.journal_entry.created", {"memo": "m", "lines": []})
    assert state["status"] == "posted"

    state = apply_accounting_event(state, "acc.journal_entry.posted", {})
    assert state["status"] == "posted"

    state = apply_accounting_event(state, "acc.journal_entry.voided", {"reason": "x"})
    assert state["status"] == "void"

    state = apply_accounting_event({}, "acc.period.closed", {"period": "2025-01"})
    assert state["status"] == "closed"

    state = apply_accounting_event({}, "acc.period.reopened", {"period": "2025-01"})
    assert state["status"] == "open"


def test_scanning_flow() -> None:
    state = apply_scanning_event({}, "scan.barcode", {"code": "c", "raw": {}})
    assert state["last_code"] == "c"

    state = apply_scanning_event(state, "scan.resolved", {"code": "c", "entity_id": "item:1", "entity_type": "item"})
    assert state["resolved_entity_id"] == "item:1"


def test_system_flow() -> None:
    state = apply_system_event({}, "sys.company.created", {"name": "A", "slug": "a"})
    assert state["slug"] == "a"

    state = apply_system_event({}, "sys.user.created", {"email": "e"})
    assert state["is_active"] is True

    state = apply_system_event({}, "sys.user.deactivated", {"reason": "x"})
    assert state["is_active"] is False

    state = apply_system_event({}, "sys.api_key.created", {"api_key_id": "k"})
    assert state["status"] == "active"

    state = apply_system_event({}, "sys.api_key.revoked", {"api_key_id": "k"})
    assert state["status"] == "revoked"

    state = apply_system_event({}, "sys.backup.created", {"backup_id": "b"})
    assert state["backup_id"] == "b"

    state = apply_system_event({}, "sys.migration.applied", {"revision": "0001"})
    assert state["revision"] == "0001"


@pytest.mark.parametrize(
    "fn,event",
    [
        (apply_documents_event, "doc.nope"),
        (apply_marketplace_event, "mp.nope"),
        (apply_accounting_event, "acc.nope"),
        (apply_scanning_event, "scan.nope"),
        (apply_system_event, "sys.nope"),
    ],
)
def test_other_handlers_unknown_raise(fn, event):
    with pytest.raises(ValueError):
        fn({}, event, {})
