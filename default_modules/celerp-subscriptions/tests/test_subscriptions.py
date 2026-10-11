# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tests for celerp-subscriptions module rebuild.

Subscription templates are docs with doc_type "subscription_invoice" or "subscription_po".
Lifecycle actions (pause/resume/cancel/generate) are at /subscriptions/<id>/<action>.
"""
from __future__ import annotations

import uuid

import pytest

from ui.i18n import t


async def _register(client, name: str | None = None) -> str:
    addr = f"sub-{uuid.uuid4().hex[:8]}@test.test"
    co = name or f"SubCo-{uuid.uuid4().hex[:6]}"
    r = await client.post("/auth/register", json={"company_name": co, "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_sub_template(client, headers, doc_type="subscription_invoice", **kwargs):
    data = {
        "doc_type": doc_type,
        "contact_id": "contact:test-001",
        "frequency": "monthly",
        "start_date": "2026-01-01",
        "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        **kwargs,
    }
    r = await client.post("/docs", json=data, headers=headers)
    assert r.status_code in {200, 201}, f"Failed to create template: {r.text}"
    created = r.json()
    ra = await client.post(f"/subscriptions/{created['id']}/activate", headers=headers)
    assert ra.status_code == 200, f"Failed to activate template: {ra.text}"
    return created


# ---------------------------------------------------------------------------
# 1. Create subscription template
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_subscription_template(client):
    tok = await _register(client)
    h = _h(tok)
    data = {
        "doc_type": "subscription_invoice",
        "contact_id": "contact:test-001",
        "frequency": "monthly",
        "start_date": "2026-01-01",
        "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    }
    r = await client.post("/docs", json=data, headers=h)
    assert r.status_code in {200, 201}, r.text
    body = r.json()
    eid = body.get("entity_id") or body.get("id") or ""
    assert eid
    assert (await client.get(f"/docs/{eid}", headers=h)).json()["status"] == "draft"


# ---------------------------------------------------------------------------
# 2. Subscription template stored as doc
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_template_stored_as_doc(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""
    r = await client.get(f"/docs/{eid}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body.get("doc_type") == "subscription_invoice"
    assert body.get("frequency") == "monthly"
    assert body.get("status") == "active"


# ---------------------------------------------------------------------------
# 3. List subscription templates - sales direction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_subscription_templates(client):
    tok = await _register(client)
    h = _h(tok)
    # Create one sales template
    await _create_sub_template(client, h, doc_type="subscription_invoice")
    # Create one purchasing template
    await _create_sub_template(client, h, doc_type="subscription_po")
    r = await client.get("/subscriptions?direction=sales", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(i.get("doc_type") == "subscription_invoice" for i in items)


# ---------------------------------------------------------------------------
# 4. List subscription templates - purchasing direction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_subscription_templates_purchasing(client):
    tok = await _register(client)
    h = _h(tok)
    await _create_sub_template(client, h, doc_type="subscription_invoice")
    await _create_sub_template(client, h, doc_type="subscription_po")
    r = await client.get("/subscriptions?direction=purchasing", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(i.get("doc_type") == "subscription_po" for i in items)


# ---------------------------------------------------------------------------
# 4b. List subscription templates - q filter (global search reuse)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_q_filters(client):
    """The list endpoint filters by q against the stored template fields, so the
    global search bar can reach subscriptions. Red at merge-base: the endpoint has
    no q param, FastAPI drops the unknown query value, and both templates return."""
    tok = await _register(client)
    h = _h(tok)
    await _create_sub_template(client, h, name="Alpha Retainer Plan")
    await _create_sub_template(client, h, name="Beta Cleaning Service")
    r = await client.get("/subscriptions?q=retainer", headers=h)
    assert r.status_code == 200
    names = [i.get("name") for i in r.json()["items"]]
    assert "Alpha Retainer Plan" in names
    assert "Beta Cleaning Service" not in names


# ---------------------------------------------------------------------------
# 5. Status filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_status_filter(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    # Should appear in active filter
    r_active = await client.get("/subscriptions?status=active", headers=h)
    assert r_active.status_code == 200
    ids_active = [i.get("id") for i in r_active.json()["items"]]
    assert eid in ids_active

    # Pause it
    await client.post(f"/subscriptions/{eid}/pause", headers=h)

    r_paused = await client.get("/subscriptions?status=paused", headers=h)
    assert r_paused.status_code == 200
    ids_paused = [i.get("id") for i in r_paused.json()["items"]]
    assert eid in ids_paused


# ---------------------------------------------------------------------------
# 6. Generate now creates invoice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_now_creates_invoice(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, name="My Sub Template")
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    doc_id = body.get("doc_id", "")
    assert doc_id.startswith("doc:")

    # Verify generated doc
    dr = await client.get(f"/docs/{doc_id}", headers=h)
    assert dr.status_code == 200
    doc = dr.json()
    assert doc.get("doc_type") == "invoice"
    assert doc.get("subscription_id") == eid
    assert "My Sub Template" in doc.get("notes", "")
    assert doc.get("terms_template") == "Standard Sales Terms"
    assert "seller until paid" in doc.get("terms_text", "")
    assert "company_name" not in doc


@pytest.mark.asyncio
async def test_generate_now_preserves_legacy_customer_terms(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, terms="Legacy subscription customer terms.")
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['doc_id']}", headers=h)).json()
    assert doc.get("terms_text") == "Legacy subscription customer terms."
    assert "terms" not in doc
    assert not doc.get("terms_template")


# ---------------------------------------------------------------------------
# 7. Generate now computes totals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_now_computes_totals(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(
        client, h,
        line_items=[
            {"description": "Item A", "quantity": 2, "unit_price": 50.0, "line_total": 100.0},
            {"description": "Item B", "quantity": 1, "unit_price": 30.0, "line_total": 30.0},
        ],
        discount=10.0,
        shipping=5.0,
    )
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]

    dr = await client.get(f"/docs/{doc_id}", headers=h)
    assert dr.status_code == 200
    doc = dr.json()
    # subtotal = 130, discount = 10, shipping = 5, tax = 0 -> total = 125
    assert float(doc.get("subtotal", 0)) == pytest.approx(130.0)
    assert float(doc.get("total", 0)) == pytest.approx(125.0)


# ---------------------------------------------------------------------------
# 8. Pause subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_subscription(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/pause", headers=h)
    assert r.status_code == 200

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.json().get("status") == "paused"


# ---------------------------------------------------------------------------
# 9. Resume subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_subscription(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    await client.post(f"/subscriptions/{eid}/pause", headers=h)
    r = await client.post(f"/subscriptions/{eid}/resume", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("next_run_date")

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.json().get("status") == "active"


# ---------------------------------------------------------------------------
# 10. Cancel subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_subscription(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    assert r.status_code == 200

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.json().get("status") == "cancelled"


# ---------------------------------------------------------------------------
# 11. Cancel already-cancelled is 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_already_cancelled_is_409(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    r2 = await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# 12. Pause already-paused or cancelled is 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_already_paused_is_409(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    await client.post(f"/subscriptions/{eid}/pause", headers=h)
    # Double-pause
    r2 = await client.post(f"/subscriptions/{eid}/pause", headers=h)
    assert r2.status_code == 409

    # Cancel then pause
    await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    r3 = await client.post(f"/subscriptions/{eid}/pause", headers=h)
    assert r3.status_code == 409


# ---------------------------------------------------------------------------
# 13. Contact ID is preserved when set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_contact_id_preserved(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, contact_id="contact:my-specific-contact")
    eid = sub.get("entity_id") or sub.get("id") or ""

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.status_code == 200
    assert dr.json().get("contact_id") == "contact:my-specific-contact"


# ---------------------------------------------------------------------------
# 14. Custom frequency uses 30-day default when no interval given
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_custom_frequency_default_interval(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, frequency="custom")
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200
    # 30-day default from today - just verify next_run_date is returned and not empty
    assert r.json().get("next_run_date")


# ---------------------------------------------------------------------------
# 15. Generated invoice appears in normal invoice list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generated_invoice_appears_in_normal_invoice_list(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]

    # Check via GET /docs?doc_type=invoice
    r2 = await client.get("/docs?doc_type=invoice", headers=h)
    assert r2.status_code == 200
    ids = [d.get("entity_id") or d.get("id") for d in r2.json().get("items", [])]
    assert doc_id in ids


@pytest.mark.asyncio
async def test_list_subscription_templates_deterministic_order(client):
    """Regression: the template list had NO ORDER BY, so OFFSET pagination could skip/duplicate
    rows. It now orders by entity_id (unique) — assert that deterministic order holds end to end."""
    tok = await _register(client)
    h = _h(tok)
    ids = [(await _create_sub_template(client, h, doc_type="subscription_invoice"))["id"] for _ in range(6)]
    myids = set(ids)

    items = (await client.get("/subscriptions?direction=sales&limit=100", headers=h)).json()["items"]
    order = [i["id"] for i in items if i["id"] in myids]
    assert order == sorted(ids, reverse=True), "subscription templates not deterministically ordered by entity_id"

    paged = []
    for off in range(0, 20, 2):
        page = (await client.get(f"/subscriptions?direction=sales&limit=2&offset={off}", headers=h)).json()["items"]
        if not page:
            break
        paged += [i["id"] for i in page]
    mine = [i for i in paged if i in myids]
    assert sorted(mine) == sorted(ids)
    assert len(mine) == len(set(mine))


# ---------------------------------------------------------------------------
# Generate finalizes like any document, once per request
# ---------------------------------------------------------------------------

async def _journal_entries(session, client, h, doc_id: str) -> list[dict]:
    """The lines each journal entry of the document was created with."""
    from sqlalchemy import select
    from celerp.models.ledger import LedgerEntry
    company_id = (await client.get("/companies/me", headers=h)).json()["id"]
    session.expire_all()
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == uuid.UUID(company_id),
        LedgerEntry.entity_type == "journal_entry",
        LedgerEntry.metadata_["doc_id"].as_string() == doc_id))).scalars().all()
    return [r.data for r in rows if r.event_type == "acc.journal_entry.created"]


def _balanced(entry: dict) -> bool:
    lines = entry.get("entries") or []
    return bool(lines) and sum(l.get("debit", 0) for l in lines) == pytest.approx(
        sum(l.get("credit", 0) for l in lines))


async def _generated(client, h, eid: str) -> list[str]:
    return (await client.get(f"/docs/{eid}", headers=h)).json().get("generated_doc_ids") or []


@pytest.mark.asyncio
async def test_generate_finalizes_the_invoice_with_its_journal_entry(client, session):
    h = _h(await _register(client))
    eid = (await _create_sub_template(client, h))["id"]

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['doc_id']}", headers=h)).json()
    assert doc["status"] == "final" and doc["ref_id"].startswith("INV-")
    assert doc["source_proforma_ref"].startswith("PF-")
    entries = await _journal_entries(session, client, h, r.json()["doc_id"])
    assert len(entries) == 1 and _balanced(entries[0])


@pytest.mark.asyncio
async def test_generate_on_a_purchase_order_subscription_creates_a_bill(client, session):
    h = _h(await _register(client))
    eid = (await _create_sub_template(client, h, doc_type="subscription_po"))["id"]

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['doc_id']}", headers=h)).json()
    assert doc["doc_type"] == "bill" and r.json()["ref_id"] == doc["ref_id"]
    assert doc["source_po_ref"].startswith("PO-")
    entries = await _journal_entries(session, client, h, r.json()["doc_id"])
    assert len(entries) == 1 and _balanced(entries[0])


@pytest.mark.asyncio
async def test_generate_retried_with_its_key_returns_the_first_result(client):
    h = _h(await _register(client))
    eid = (await _create_sub_template(client, h))["id"]

    first = await client.post(f"/subscriptions/{eid}/generate", json={"idempotency_key": "gen-1"}, headers=h)
    again = await client.post(f"/subscriptions/{eid}/generate", json={"idempotency_key": "gen-1"}, headers=h)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await _generated(client, h, eid) == [first.json()["doc_id"]]


@pytest.mark.asyncio
async def test_generate_with_a_new_key_creates_another_document(client):
    h = _h(await _register(client))
    eid = (await _create_sub_template(client, h))["id"]

    a = await client.post(f"/subscriptions/{eid}/generate", json={"idempotency_key": "gen-a"}, headers=h)
    b = await client.post(f"/subscriptions/{eid}/generate", json={"idempotency_key": "gen-b"}, headers=h)
    assert a.json()["doc_id"] != b.json()["doc_id"]
    assert await _generated(client, h, eid) == [a.json()["doc_id"], b.json()["doc_id"]]


@pytest.mark.asyncio
async def test_generate_key_used_on_another_subscription_is_refused(client):
    h = _h(await _register(client))
    one = (await _create_sub_template(client, h))["id"]
    two = (await _create_sub_template(client, h))["id"]

    assert (await client.post(f"/subscriptions/{one}/generate", json={"idempotency_key": "gen-x"},
                              headers=h)).status_code == 200
    r = await client.post(f"/subscriptions/{two}/generate", json={"idempotency_key": "gen-x"}, headers=h)
    assert r.status_code == 409, r.text
    assert await _generated(client, h, two) == []


@pytest.mark.asyncio
async def test_generate_key_used_to_edit_the_template_is_refused(client):
    h = _h(await _register(client))
    eid = (await client.post("/docs", json={"doc_type": "subscription_invoice", "frequency": "monthly",
                                            "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0}]},
                             headers=h)).json()["id"]
    edit = {"fields_changed": {"notes": {"old": None, "new": "x"}}, "idempotency_key": "gen-e"}
    assert (await client.patch(f"/docs/{eid}", json=edit, headers=h)).status_code == 200
    assert (await client.post(f"/subscriptions/{eid}/activate", headers=h)).status_code == 200
    r = await client.post(f"/subscriptions/{eid}/generate", json={"idempotency_key": "gen-e"}, headers=h)
    assert r.status_code == 409, r.text
    assert await _generated(client, h, eid) == []


@pytest.mark.asyncio
async def test_generate_in_a_locked_period_is_refused_and_writes_nothing(client, session):
    from datetime import date
    from celerp.services.company_lock import locked_company
    h = _h(await _register(client))
    eid = (await _create_sub_template(client, h))["id"]
    company = await locked_company(session, uuid.UUID((await client.get("/companies/me", headers=h)).json()["id"]))
    company.settings = {**company.settings, "lock_date": date.today().isoformat()}
    await session.commit()
    before = (await client.get("/docs?doc_type=invoice", headers=h)).json()["items"]

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("error.period_locked", "en", date=date.today().isoformat())
    assert (await client.get("/docs?doc_type=invoice", headers=h)).json()["items"] == before
    assert await _generated(client, h, eid) == []


# ---------------------------------------------------------------------------
# Doctor repairs documents generated before Generate finalized canonically
# ---------------------------------------------------------------------------

async def _generated_the_old_way(session, auth, doc_type: str, ref: str, **extra) -> str:
    """The two events Generate used to emit: a created document marked finalized, with no entry."""
    from celerp.events.engine import emit_event
    doc_id = f"doc:PF-OLD-{ref}"
    data = {"doc_type": doc_type, "ref_id": f"PF-OLD-{ref}", "contact_id": None,
            "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0,
                            "line_total": 100.0}],
            "subtotal": 100.0, "total": 100.0, "amount_outstanding": 100.0, "discount": 0.0,
            "shipping": 0.0, "tax": 0.0, "issue_date": "2026-09-01", "subscription_id": "doc:SUB", **extra}
    for event_type, event_data in (("doc.created", data),
                                   ("doc.finalized", {"ref_id": f"INV-OLD-{ref}", "source_proforma_ref": data["ref_id"]})):
        await emit_event(session, company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
                         event_type=event_type, data=event_data, actor_id=auth["user_id"], location_id=None,
                         source="subscription", idempotency_key=str(uuid.uuid4()))
    await session.commit()
    return doc_id


async def _missing_jes(client, auth, *, fix: bool) -> dict:
    r = await client.post(f"/admin/doctor?checks=missing_jes&fix={str(fix).lower()}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    (result,) = r.json()["results"]
    return result


@pytest.mark.asyncio
async def test_doctor_posts_the_missing_entry_of_an_invoice_generated_the_old_way(client, session, auth):
    doc_id = await _generated_the_old_way(session, auth, "invoice", "1")

    result = await _missing_jes(client, auth, fix=True)
    assert [d["doc_id"] for d in result["details"]] == [doc_id] and result["fixed"] == 1
    entries = await _journal_entries(session, client, auth["headers"], doc_id)
    assert len(entries) == 1 and _balanced(entries[0])
    again = await _missing_jes(client, auth, fix=True)
    assert (again["found"], again["fixed"]) == (0, 0)


@pytest.mark.asyncio
async def test_doctor_leaves_an_old_foreign_invoice_without_a_rate_for_review(client, session, auth):
    doc_id = await _generated_the_old_way(session, auth, "invoice", "2", currency="EUR")

    result = await _missing_jes(client, auth, fix=True)
    (finding,) = result["details"]
    assert finding["doc_id"] == doc_id and finding["blocked_reason"] and result["fixed"] == 0
    assert await _journal_entries(session, client, auth["headers"], doc_id) == []


@pytest.mark.asyncio
async def test_doctor_leaves_an_old_generated_purchase_order_as_it_is(client, session, auth):
    doc_id = await _generated_the_old_way(session, auth, "purchase_order", "3")

    result = await _missing_jes(client, auth, fix=True)
    assert (result["found"], result["fixed"]) == (0, 0)
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc["doc_type"] == "purchase_order"
    assert await _journal_entries(session, client, auth["headers"], doc_id) == []


@pytest.mark.asyncio
async def test_doctor_refuses_to_post_into_a_locked_period(client, session, auth):
    from celerp.services.company_lock import locked_company
    doc_id = await _generated_the_old_way(session, auth, "invoice", "4")
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "lock_date": "2099-12-31"}
    await session.commit()

    r = await client.post("/admin/doctor?checks=missing_jes&fix=true", headers=auth["headers"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("error.period_locked", "en", date="2099-12-31")
    await session.rollback()
    assert await _journal_entries(session, client, auth["headers"], doc_id) == []
