# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Document and list exports are gated on import_export_data at the API, the list index
and summary on view_documents, the summary cards follow every narrowing filter the list
carries, and the sort travels to the API so the page and the export share one order."""

from __future__ import annotations

import uuid

import pytest


async def _reg(client) -> str:
    addr = f"guard-{uuid.uuid4().hex[:8]}@guards.test"
    r = await client.post("/auth/register", json={
        "company_name": "GuardCo", "email": addr, "name": "Admin", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@guards.test"
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


async def _revoke(client, admin_token: str, perm_key: str, role_key: str) -> None:
    r = await client.patch(
        "/companies/me/role-permissions",
        json={"perm_key": perm_key, "role_key": role_key, "granted": False},
        headers=_h(admin_token),
    )
    assert r.status_code == 200, r.text


async def _invoice(client, tok, total: float, contact_id: str = "c:1", contact_name: str = "Alice") -> str:
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": contact_id, "contact_name": contact_name,
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0, "total": total,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _list(client, tok) -> str:
    r = await client.post("/lists", headers=_h(tok), json={
        "list_type": "quotation", "customer_name": "Alice",
        "line_items": [{"name": "Ring", "quantity": 1, "unit_price": 10, "line_total": 10}],
        "subtotal": 10, "discount": 0, "discount_type": "flat", "tax": 0, "total": 10, "currency": "THB",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_exports_require_import_export_data(client, session):
    """A role that may view documents and inventory but not import or export data is
    refused on every CSV export; the owner, who holds it, is served."""
    admin = await _reg(client)
    viewer = await _user_with_role(client, session, admin, "viewer")
    await _invoice(client, admin, 42)
    await _list(client, admin)

    for path in ("/docs/export/csv", "/lists/export/csv", "/items/export/csv"):
        denied = await client.get(path, headers=_h(viewer))
        assert denied.status_code == 403, (path, denied.text)
        allowed = await client.get(path, headers=_h(admin))
        assert allowed.status_code == 200, (path, allowed.text)


@pytest.mark.asyncio
async def test_lists_index_and_summary_require_view_documents(client, session):
    """The list index and its summary are refused once view_documents is revoked for the
    caller's role, exactly like the document index."""
    admin = await _reg(client)
    viewer = await _user_with_role(client, session, admin, "viewer")
    await _list(client, admin)
    assert (await client.get("/lists", headers=_h(viewer))).status_code == 200
    await _revoke(client, admin, "view_documents", "viewer")
    assert (await client.get("/lists", headers=_h(viewer))).status_code == 403
    assert (await client.get("/lists/summary", headers=_h(viewer))).status_code == 403
    assert (await client.get("/lists", headers=_h(admin))).status_code == 200


@pytest.mark.asyncio
async def test_doc_summary_follows_contact_and_search_filters(client):
    """The status cards summarise the set the list shows: a contact filter or a search term
    narrows the summary the same way it narrows the rows."""
    tok = await _reg(client)
    await _invoice(client, tok, 42, contact_id="c:1", contact_name="Alice")
    await _invoice(client, tok, 7, contact_id="c:2", contact_name="Bob")

    everything = (await client.get("/docs/summary?doc_type=invoice", headers=_h(tok))).json()
    assert everything["draft_total"] == 49, everything

    by_contact = (await client.get("/docs/summary?doc_type=invoice&contact_id=c:2", headers=_h(tok))).json()
    assert by_contact["draft_total"] == 7, by_contact

    by_search = (await client.get("/docs/summary?doc_type=invoice&q=alice", headers=_h(tok))).json()
    assert by_search["draft_total"] == 42, by_search


@pytest.mark.asyncio
async def test_list_summary_follows_search_filter(client):
    """The list status cards follow the search term the index carries."""
    tok = await _reg(client)
    await _list(client, tok)
    r = await client.post("/lists", headers=_h(tok), json={
        "list_type": "quotation", "customer_name": "Zed",
        "line_items": [{"name": "Ring", "quantity": 1, "unit_price": 10, "line_total": 10}],
        "subtotal": 10, "discount": 0, "discount_type": "flat", "tax": 0, "total": 10, "currency": "THB",
    })
    assert r.status_code == 200, r.text
    total = (await client.get("/lists/summary?list_type=quotation", headers=_h(tok))).json()
    narrowed = (await client.get("/lists/summary?list_type=quotation&q=zed", headers=_h(tok))).json()
    assert total["draft_count"] == 2, total
    assert narrowed["draft_count"] == 1, narrowed


@pytest.mark.asyncio
async def test_doc_list_and_export_share_sort(client):
    """sort and dir order the document list at the API and the export follows the same
    order; an unknown sort or direction is refused with a message naming it."""
    tok = await _reg(client)
    for total in (50, 5, 20):
        await _invoice(client, tok, total)

    asc = (await client.get("/docs?doc_type=invoice&sort=total&dir=asc", headers=_h(tok))).json()
    assert [d["total"] for d in asc["items"]] == [5, 20, 50], asc
    desc = (await client.get("/docs?doc_type=invoice&sort=total&dir=desc", headers=_h(tok))).json()
    assert [d["total"] for d in desc["items"]] == [50, 20, 5], desc
    searched = (await client.get("/docs?doc_type=invoice&q=alice&sort=total&dir=asc", headers=_h(tok))).json()
    assert [d["total"] for d in searched["items"]] == [5, 20, 50], searched

    csv = await client.get("/docs/export/csv?doc_type=invoice&sort=total&dir=asc&cols=total", headers=_h(tok))
    assert csv.status_code == 200, csv.text
    assert csv.text.strip().splitlines() == ["total", "5.0", "20.0", "50.0"], csv.text

    bad_sort = await client.get("/docs?sort=sideways", headers=_h(tok))
    assert bad_sort.status_code == 422 and "sideways" in bad_sort.json()["detail"], bad_sort.text
    bad_dir = await client.get("/docs?sort=total&dir=up", headers=_h(tok))
    assert bad_dir.status_code == 422 and "asc or desc" in bad_dir.json()["detail"], bad_dir.text
