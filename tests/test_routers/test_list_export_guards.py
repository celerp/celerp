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


# ── query and sort equivalence ────────────────────────────────────────────────

import contextlib
import csv
import io
from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.engine import Engine


@contextlib.contextmanager
def _sql_spy():
    captured: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield captured
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


async def _company_id(client, tok: str) -> str:
    r = await client.get("/auth/my-companies", headers=_h(tok))
    return r.json()["items"][0]["company_id"]


async def _seed_docs(session, company_id: str, docs: dict[str, dict]) -> None:
    from celerp.models.projections import Projection

    now = datetime.now(timezone.utc)
    for eid, state in docs.items():
        session.add(Projection(company_id=company_id, entity_id=eid, entity_type="doc",
                               state={"doc_type": "invoice", "status": "issued", "total": 1.0} | state,
                               version=1, updated_at=now))
    await session.commit()


def _csv_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


@pytest.mark.asyncio
async def test_all_issued_list_pages_in_sql(client, session):
    """The default typed list (all_issued) is paged by the database like every other list: no
    request reads every document of the type into Python to slice one page. Drafts and voids
    stay off the list."""
    tok = await _reg(client)
    company_id = await _company_id(client, tok)
    await _seed_docs(session, company_id, {
        "doc:page-1": {}, "doc:page-2": {}, "doc:page-3": {},
        "doc:page-draft": {"status": "draft"}, "doc:page-void": {"status": "void"},
    })
    with _sql_spy() as captured:
        r = await client.get("/docs?doc_type=invoice&all_issued=1&limit=2&offset=1", headers=_h(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    selects = [s for s in captured if s.lower().lstrip().startswith("select") and "from projections" in s.lower()]
    assert selects
    unbounded = [s for s in selects if "limit" not in s.lower() and "count(" not in s.lower()]
    assert not unbounded, unbounded


@pytest.mark.asyncio
async def test_doc_sort_orders_by_the_values_the_list_displays(client, session):
    """Imported documents carry their number and dates under older keys (ref, ref_id,
    payment_due_date); the list shows those, so the sort has to order by them too. Same order
    on the SQL-paged path, the Python-filtered path and the export."""
    tok = await _reg(client)
    company_id = await _company_id(client, tok)
    await _seed_docs(session, company_id, {
        "doc:sort-a": {"doc_number": "INV-002", "due_date": "2026-03-01", "issue_date": "2026-01-02"},
        "doc:sort-b": {"ref_id": "INV-001", "payment_due_date": "2026-04-01", "issue_date": "2026-01-01"},
        "doc:sort-c": {"ref": "INV-003", "due_date": "2026-02-01", "issue_date": "2026-01-03"},
    })
    expect = {"number": ["doc:sort-b", "doc:sort-a", "doc:sort-c"],
              "due": ["doc:sort-c", "doc:sort-a", "doc:sort-b"]}
    for sort, order in expect.items():
        for extra in ("", "&unfulfilled_only=1"):
            r = await client.get(f"/docs?doc_type=invoice&sort={sort}&dir=asc{extra}", headers=_h(tok))
            assert r.status_code == 200, r.text
            assert [d["id"] for d in r.json()["items"]] == order, (sort, extra)
        r = await client.get(f"/docs/export/csv?doc_type=invoice&sort={sort}&dir=asc", headers=_h(tok))
        assert r.status_code == 200, r.text
        assert [row["entity_id"] for row in _csv_rows(r.text)] == order, sort
    rows = {row["entity_id"]: row for row in _csv_rows(r.text)}
    assert rows["doc:sort-b"]["doc_number"] == "INV-001"
    assert rows["doc:sort-b"]["due_date"] == "2026-04-01"


@pytest.mark.asyncio
async def test_doc_list_shows_and_sorts_by_older_amount_keys(client, session):
    """Imported documents may carry their amounts as total_amount and outstanding_balance. The
    list shows and sorts by those when the current keys are missing; a stored 0 still wins."""
    tok = await _reg(client)
    company_id = await _company_id(client, tok)
    await _seed_docs(session, company_id, {
        "doc:amt-a": {"total": 20.0, "amount_outstanding": 0, "outstanding_balance": 50.0},
        "doc:amt-b": {"total": None, "total_amount": 25.0, "outstanding_balance": 5.0},
        "doc:amt-c": {"total": 50.0, "amount_outstanding": 30.0},
    })
    expect = {"total": ["doc:amt-a", "doc:amt-b", "doc:amt-c"],
              "outstanding": ["doc:amt-a", "doc:amt-b", "doc:amt-c"]}
    for sort, order in expect.items():
        for extra in ("", "&unfulfilled_only=1"):
            r = await client.get(f"/docs?doc_type=invoice&sort={sort}&dir=asc{extra}", headers=_h(tok))
            assert r.status_code == 200, r.text
            assert [d["id"] for d in r.json()["items"]] == order, (sort, extra)
        r = await client.get(f"/docs/export/csv?doc_type=invoice&sort={sort}&dir=asc", headers=_h(tok))
        assert r.status_code == 200, r.text
        assert [row["entity_id"] for row in _csv_rows(r.text)] == order, sort
    items = {d["id"]: d for d in (await client.get("/docs?doc_type=invoice", headers=_h(tok))).json()["items"]}
    assert items["doc:amt-b"]["total"] == 25.0 and items["doc:amt-b"]["amount_outstanding"] == 5.0
    assert items["doc:amt-a"]["amount_outstanding"] == 0


@pytest.mark.asyncio
async def test_doc_export_carries_the_issue_date(client):
    tok = await _reg(client)
    doc_id = await _invoice(client, tok, 5.0)
    issue_date = (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()["issue_date"]
    r = await client.get("/docs/export/csv?doc_type=invoice", headers=_h(tok))
    assert r.status_code == 200, r.text
    rows = _csv_rows(r.text)
    assert rows and rows[0]["issue_date"] == issue_date


@pytest.mark.asyncio
async def test_doc_amount_sort_puts_text_amounts_with_missing_ones(client, session):
    """An amount stored as text such as "N/A" sorts as a missing amount instead of failing the
    list and its export."""
    tok = await _reg(client)
    company_id = await _company_id(client, tok)
    await _seed_docs(session, company_id, {
        "doc:txt-a": {"total": 10.0, "amount_outstanding": 10.0},
        "doc:txt-b": {"total": "N/A", "amount_outstanding": "n/a"},
        "doc:txt-c": {"total": 30.0, "amount_outstanding": 1.5e1},
    })
    for sort in ("total", "outstanding"):
        r = await client.get(f"/docs?doc_type=invoice&sort={sort}&dir=asc", headers=_h(tok))
        assert r.status_code == 200, r.text
        assert [d["id"] for d in r.json()["items"]] == ["doc:txt-b", "doc:txt-a", "doc:txt-c"], sort
        r = await client.get(f"/docs/export/csv?doc_type=invoice&sort={sort}&dir=desc", headers=_h(tok))
        assert r.status_code == 200, r.text
        assert [row["entity_id"] for row in _csv_rows(r.text)] == ["doc:txt-c", "doc:txt-a", "doc:txt-b"], sort


@pytest.mark.asyncio
async def test_list_summary_skips_a_text_total(client, session):
    """A list whose total is text is counted, and adds nothing to the value, instead of failing
    the lists page cards."""
    import uuid as _uuid
    from celerp.events.engine import emit_event

    tok = await _reg(client)
    company_id = await _company_id(client, tok)
    for ref, total in (("TXT-1", "N/A"), ("TXT-2", "12.5")):
        r = await client.post("/lists", headers=_h(tok), json={"list_type": "quotation", "ref_id": ref})
        assert r.status_code == 200, r.text
        await emit_event(
            session, company_id=company_id, entity_id=r.json()["id"], entity_type="list",
            event_type="list.patched", data={"total": total}, actor_id=None, location_id=None,
            source="test", idempotency_key=str(_uuid.uuid4()), metadata_={},
        )
    r = await client.get("/lists/summary", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["total_count"] == 2
    assert r.json()["total_value"] == 12.5
