# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Viewer role contract (the roles reference table in Settings > Users):

- a viewer can VIEW documents, contacts, and inventory;
- a viewer can EDIT nothing.

The API enforces the write side with a router-level guard (viewer_read_only in
celerp.services.auth) so every mutation endpoint, present and future, is
covered without per-endpoint checks. These tests pin the contract per module:
one representative mutation 403s for a viewer (and still works for an
operator), while the corresponding reads stay open.
"""
from __future__ import annotations

import uuid

import pytest


async def _admin(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@viewer.test"
    r = await client.post("/auth/register", json={
        "company_name": "ViewerCo", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@viewer.test"
    r = await client.post(
        "/companies/me/users",
        json={"name": role.title(), "email": addr, "password": "testpass123", "role": role},
        headers=_h(admin_token),
    )
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    r2 = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_viewer_reads_allowed_writes_forbidden_across_modules(client, session):
    """The core matrix: per module, GET 200 / mutation 403 for a viewer."""
    admin = await _admin(client)
    viewer = await _user_with_role(client, session, admin, "viewer")

    # Seed one of everything as admin.
    item = (await client.post("/items", headers=_h(admin), json={
        "sku": "VR-1", "name": "Widget", "quantity": 5, "sell_by": "piece"})).json()["id"]
    doc = (await client.post("/docs", headers=_h(admin), json={
        "doc_type": "invoice", "line_items": [
            {"entity_id": item, "sku": "VR-1", "name": "Widget", "quantity": 1,
             "unit_price": 10, "sell_by": "piece"}], "total": 10})).json()["id"]
    contact = (await client.post("/crm/contacts", headers=_h(admin), json={
        "name": "Cust", "contact_type": "customer"})).json()["id"]

    # READS the table promises: all 200 for the viewer.
    assert (await client.get("/items", headers=_h(viewer))).status_code == 200
    assert (await client.get(f"/items/{item}", headers=_h(viewer))).status_code == 200
    assert (await client.get("/docs", headers=_h(viewer))).status_code == 200
    assert (await client.get(f"/docs/{doc}", headers=_h(viewer))).status_code == 200
    assert (await client.get("/crm/contacts", headers=_h(viewer))).status_code == 200
    assert (await client.get(f"/crm/contacts/{contact}", headers=_h(viewer))).status_code == 200

    # WRITES: representative mutation per module, all 403 for the viewer.
    denied = [
        client.post("/items", headers=_h(viewer), json={
            "sku": "VR-X", "name": "Nope", "quantity": 1, "sell_by": "piece"}),
        client.patch(f"/items/{item}", headers=_h(viewer), json={
            "fields_changed": {"name": {"old": "Widget", "new": "Hacked"}}}),
        client.post("/docs", headers=_h(viewer), json={"doc_type": "invoice", "line_items": []}),
        client.patch(f"/docs/{doc}", headers=_h(viewer), json={
            "fields_changed": {"notes": {"old": None, "new": "edited"}}}),
        client.post("/crm/contacts", headers=_h(viewer), json={
            "name": "Nope", "contact_type": "customer"}),
        client.post("/crm/contacts/bulk/delete", headers=_h(viewer), json={
            "contact_ids": [contact]}),
    ]
    for coro in denied:
        r = await coro
        assert r.status_code == 403, f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text}"

    # The item is untouched by the attempted edit.
    assert (await client.get(f"/items/{item}", headers=_h(viewer))).json()["name"] == "Widget"


@pytest.mark.asyncio
async def test_operator_writes_still_allowed(client, session):
    """The guard must not over-block: an operator can still create and edit."""
    admin = await _admin(client)
    operator = await _user_with_role(client, session, admin, "operator")

    r_item = await client.post("/items", headers=_h(operator), json={
        "sku": "OP-1", "name": "OpWidget", "quantity": 2, "sell_by": "piece"})
    assert r_item.status_code == 200, r_item.text
    r_doc = await client.post("/docs", headers=_h(operator), json={
        "doc_type": "invoice", "line_items": []})
    assert r_doc.status_code == 200, r_doc.text
    r_contact = await client.post("/crm/contacts", headers=_h(operator), json={
        "name": "OpCust", "contact_type": "customer"})
    assert r_contact.status_code == 200, r_contact.text


@pytest.mark.asyncio
async def test_viewer_blocked_on_lists_and_label_templates(client, session):
    """Scan lists mutate stock records and label templates are shared config -
    both are writes; label PRINTING is a read (a POST that only renders a PDF)
    and stays open to viewers."""
    admin = await _admin(client)
    viewer = await _user_with_role(client, session, admin, "viewer")

    r = await client.post("/lists", headers=_h(viewer), json={"list_type": "quotation"})
    assert r.status_code == 403

    r = await client.post("/api/labels/templates", headers=_h(viewer), json={"name": "Nope"})
    assert r.status_code == 403

    # Printing stays available: it renders, it does not change anything.
    r = await client.post("/api/labels/print/some-item", headers=_h(viewer))
    assert r.status_code == 200, r.text


def test_nav_lets_viewers_see_documents_and_contacts():
    """The sidebar must show document and contact sections to viewers - the roles
    table promises 'View documents & contacts' at viewer level. (Payments stays
    manager; it is a Finance entry, not a document list.)"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    docs = (root / "default_modules/celerp-docs/__init__.py").read_text()
    for line in docs.splitlines():
        if '"group": "Sales Documents"' in line or '"group": "Purchasing Documents"' in line:
            assert '"min_role": "viewer"' in line, line
    contacts = (root / "default_modules/celerp-contacts/__init__.py").read_text()
    for line in contacts.splitlines():
        if '"group": "Contacts"' in line:
            assert '"min_role": "viewer"' in line, line
