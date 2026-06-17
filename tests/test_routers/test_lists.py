# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""List router tests - CRUD, lifecycle, convert, duplicate, import, export, summary."""

from __future__ import annotations

import uuid

import pytest


async def _register(client, email: str | None = None) -> str:
    addr = email or f"admin-{uuid.uuid4().hex[:8]}@lists.test"
    r = await client.post("/auth/register", json={
        "company_name": "Lists Co", "email": addr, "name": "Admin", "password": "pw",
    })
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_list(client, token, **overrides) -> str:
    payload = {
        "list_type": "sale",
        "customer_name": "Test Customer",
        "line_items": [
            {"name": "Ruby Ring", "quantity": 2, "unit_price": 5000, "line_total": 10000},
            {"name": "Gold Chain", "quantity": 1, "unit_price": 3000, "line_total": 3000},
        ],
        "subtotal": 13000,
        "discount": 0,
        "discount_type": "flat",
        "tax": 0,
        "total": 13000,
        "currency": "THB",
    }
    payload.update(overrides)
    r = await client.post("/lists", headers=_h(token), json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestListCRUD:

    @pytest.mark.asyncio
    async def test_create_list_returns_entity_id_and_ref(self, client):
        token = await _register(client)
        r = await client.post("/lists", headers=_h(token), json={
            "list_type": "sale", "customer_name": "Alice",
            "line_items": [{"name": "A", "quantity": 1, "unit_price": 100, "line_total": 100}],
            "subtotal": 100, "total": 100,
        })
        assert r.status_code == 200
        body = r.json()
        assert "id" in body
        assert body["id"].startswith("list:LST-")
        assert "event_id" in body

    @pytest.mark.asyncio
    async def test_get_list_detail(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        r = await client.get(f"/lists/{eid}", headers=_h(token))
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == eid
        assert body["status"] == "draft"
        assert body["customer_name"] == "Test Customer"
        assert body["list_type"] == "sale"
        assert len(body["line_items"]) == 2

    @pytest.mark.asyncio
    async def test_get_missing_list_404(self, client):
        token = await _register(client)
        r = await client.get("/lists/list:NOPE", headers=_h(token))
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_list_lists_returns_dict_format(self, client):
        token = await _register(client)
        await _create_list(client, token)
        await _create_list(client, token, customer_name="Second")
        r = await client.get("/lists", headers=_h(token))
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
        assert "items" in body
        assert "total" in body
        assert body["total"] == 2
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_lists_filters(self, client):
        token = await _register(client)
        await _create_list(client, token, list_type="sale")
        await _create_list(client, token, list_type="consignment")

        # filter by list_type
        r = await client.get("/lists?list_type=consignment", headers=_h(token))
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["list_type"] == "consignment"

        # filter by status
        r = await client.get("/lists?status=draft", headers=_h(token))
        assert r.json()["total"] == 2

        r = await client.get("/lists?status=sent", headers=_h(token))
        assert r.json()["total"] == 0

        # exclude_status
        r = await client.get("/lists?exclude_status=draft", headers=_h(token))
        assert r.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_list_lists_search(self, client):
        token = await _register(client)
        eid = await _create_list(client, token, customer_name="Sakura Gems")
        await _create_list(client, token, customer_name="Atlas Mining")
        ref = (await client.get(f"/lists/{eid}", headers=_h(token))).json()["ref_id"]

        # search by customer name
        r = await client.get("/lists?q=sakura", headers=_h(token))
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["customer_name"] == "Sakura Gems"

        # search by ref_id
        r = await client.get(f"/lists?q={ref}", headers=_h(token))
        assert r.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_list_lists_pagination(self, client):
        token = await _register(client)
        for i in range(5):
            await _create_list(client, token, customer_name=f"C{i}")

        r = await client.get("/lists?limit=2&offset=0", headers=_h(token))
        body = r.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        r2 = await client.get("/lists?limit=2&offset=2", headers=_h(token))
        assert len(r2.json()["items"]) == 2

        r3 = await client.get("/lists?limit=2&offset=4", headers=_h(token))
        assert len(r3.json()["items"]) == 1

    @pytest.mark.asyncio
    async def test_patch_draft_list(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)

        r = await client.patch(f"/lists/{eid}", headers=_h(token), json={
            "fields_changed": {
                "customer_name": {"old": "Test Customer", "new": "Updated Customer"},
                "notes": {"old": None, "new": "Special handling"},
            },
        })
        assert r.status_code == 200

        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["customer_name"] == "Updated Customer"
        assert detail["notes"] == "Special handling"

    @pytest.mark.asyncio
    async def test_patch_non_draft_rejected(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))

        r = await client.patch(f"/lists/{eid}", headers=_h(token), json={
            "fields_changed": {"notes": {"old": None, "new": "x"}},
        })
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_create_with_custom_ref_id(self, client):
        token = await _register(client)
        r = await client.post("/lists", headers=_h(token), json={
            "ref_id": "CUSTOM-001", "list_type": "sale",
            "line_items": [], "subtotal": 0, "total": 0,
        })
        assert r.status_code == 200
        eid = r.json()["id"]
        assert eid == "list:CUSTOM-001"
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["ref_id"] == "CUSTOM-001"

    @pytest.mark.asyncio
    async def test_totals_recalculated_on_create(self, client):
        token = await _register(client)
        eid = await _create_list(client, token, line_items=[
            {"name": "A", "quantity": 2, "unit_price": 1000, "line_total": 2000},
            {"name": "B", "quantity": 3, "unit_price": 500, "line_total": 1500},
        ], discount=10, discount_type="percentage", tax=7)

        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["subtotal"] == 3500
        assert detail["discount_amount"] == 350  # 10% of 3500
        taxable = 3500 - 350  # 3150
        assert detail["tax_amount"] == taxable * 7 / 100  # 220.5
        assert detail["total"] == taxable + detail["tax_amount"]  # 3370.5


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestListLifecycle:

    @pytest.mark.asyncio
    async def test_full_lifecycle_draft_finalize_close(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)  # default list_type = quotation

        # Issue (draft -> finalized). Issue does NOT auto-send a quotation; sent_at is set later.
        r = await client.post(f"/lists/{eid}/finalize", headers=_h(token))
        assert r.status_code == 200
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["status"] == "finalized" and detail.get("finalized_at") and not detail.get("sent_at")

        # Mark as sent sets the milestone; status stays finalized; unmark clears it.
        assert (await client.post(f"/lists/{eid}/send", headers=_h(token), json={"sent_via": "manual"})).status_code == 200
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["status"] == "finalized" and detail.get("sent_at")
        assert (await client.post(f"/lists/{eid}/unmark-sent", headers=_h(token))).status_code == 200
        assert not (await client.get(f"/lists/{eid}", headers=_h(token))).json().get("sent_at")

        # convert (finalized -> closed, result=converted)
        r = await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "invoice"})
        assert r.status_code == 200
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["status"] == "closed" and detail["result"] == "converted"

    @pytest.mark.asyncio
    async def test_finalize_only_from_draft(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        assert (await client.post(f"/lists/{eid}/finalize", headers=_h(token))).status_code == 200
        # finalize again -> 409 (already finalized)
        assert (await client.post(f"/lists/{eid}/finalize", headers=_h(token))).status_code == 409

    @pytest.mark.asyncio
    async def test_revert_finalized_to_draft(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))
        r = await client.post(f"/lists/{eid}/revert-to-draft", headers=_h(token), json={})
        assert r.status_code == 200
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["status"] == "draft" and "sent_at" not in detail
        # revert from draft -> 409 (nothing to revert)
        assert (await client.post(f"/lists/{eid}/revert-to-draft", headers=_h(token), json={})).status_code == 409

    @pytest.mark.asyncio
    async def test_void_from_draft_and_finalized_not_closed(self, client):
        token = await _register(client)

        # void draft
        eid1 = await _create_list(client, token)
        r = await client.post(f"/lists/{eid1}/void", headers=_h(token), json={"reason": "cancelled"})
        assert r.status_code == 200
        detail = (await client.get(f"/lists/{eid1}", headers=_h(token))).json()
        assert detail["status"] == "void" and detail["void_reason"] == "cancelled"

        # void finalized
        eid2 = await _create_list(client, token)
        await client.post(f"/lists/{eid2}/finalize", headers=_h(token))
        assert (await client.post(f"/lists/{eid2}/void", headers=_h(token), json={})).status_code == 200

        # cannot void a closed list
        eid3 = await _create_list(client, token)
        await client.post(f"/lists/{eid3}/finalize", headers=_h(token))
        await client.post(f"/lists/{eid3}/convert", headers=_h(token), json={"target_type": "invoice"})
        assert (await client.post(f"/lists/{eid3}/void", headers=_h(token), json={})).status_code == 409

    @pytest.mark.asyncio
    async def test_void_already_voided_rejected(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/void", headers=_h(token), json={})
        r = await client.post(f"/lists/{eid}/void", headers=_h(token), json={})
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Convert
# ---------------------------------------------------------------------------

class TestListConvert:

    @pytest.mark.asyncio
    async def test_convert_to_invoice(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))  # terminal action needs finalize first

        r = await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "invoice"})
        assert r.status_code == 200
        body = r.json()
        assert body["target_doc_id"].startswith("doc:")

        # list closes with result=converted
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["status"] == "closed" and detail["result"] == "converted"
        assert detail["converted_to"] == body["target_doc_id"]
        assert detail["converted_to_type"] == "invoice"

        # target doc exists as a draft invoice
        doc = (await client.get(f"/docs/{body['target_doc_id']}", headers=_h(token))).json()
        assert doc["doc_type"] == "invoice"
        assert doc["status"] == "draft"
        assert doc["source_list_id"] == eid

    @pytest.mark.asyncio
    async def test_convert_to_memo(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))
        r = await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "memo"})
        assert r.status_code == 200
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["converted_to_type"] == "memo"

    @pytest.mark.asyncio
    async def test_convert_requires_finalize(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        # convert straight from draft is rejected — finalize (send) the quote first
        r = await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "invoice"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_convert_already_converted_rejected(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))
        await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "invoice"})
        # already closed -> 409
        r = await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "memo"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_convert_invalid_target_type(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))
        r = await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "purchase_order"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

class TestListDuplicate:

    @pytest.mark.asyncio
    async def test_duplicate_creates_new_draft(self, client):
        token = await _register(client)
        eid = await _create_list(client, token, customer_name="Original Corp")
        # finalize the original so it's not a draft
        await client.post(f"/lists/{eid}/finalize", headers=_h(token))

        r = await client.post(f"/lists/{eid}/duplicate", headers=_h(token))
        assert r.status_code == 200
        new_eid = r.json()["id"]
        assert new_eid != eid

        dup = (await client.get(f"/lists/{new_eid}", headers=_h(token))).json()
        assert dup["status"] == "draft"
        assert dup["customer_name"] == "Original Corp"
        assert dup["source_list_id"] == eid
        assert len(dup["line_items"]) == 2

    @pytest.mark.asyncio
    async def test_duplicate_gets_new_ref_id(self, client):
        token = await _register(client)
        eid = await _create_list(client, token)
        orig_ref = (await client.get(f"/lists/{eid}", headers=_h(token))).json()["ref_id"]

        new_eid = (await client.post(f"/lists/{eid}/duplicate", headers=_h(token))).json()["id"]
        new_ref = (await client.get(f"/lists/{new_eid}", headers=_h(token))).json()["ref_id"]
        assert new_ref != orig_ref


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class TestListImport:

    @pytest.mark.asyncio
    async def test_import_single_list(self, client):
        token = await _register(client)
        r = await client.post("/lists/import", headers=_h(token), json={
            "entity_id": "list:IMP-001",
            "event_type": "list.created",
            "data": {
                "ref_id": "IMP-001", "list_type": "sale", "customer_name": "Imported",
                "line_items": [{"name": "Gem", "quantity": 1, "unit_price": 500, "line_total": 500}],
                "subtotal": 500, "total": 500, "status": "draft",
            },
            "source": "old_system",
            "idempotency_key": "imp-key-001",
        })
        assert r.status_code == 200
        assert r.json()["id"] == "list:IMP-001"
        assert r.json()["idempotency_hit"] is False

        detail = (await client.get("/lists/list:IMP-001", headers=_h(token))).json()
        assert detail["customer_name"] == "Imported"

    @pytest.mark.asyncio
    async def test_import_idempotent(self, client):
        token = await _register(client)
        payload = {
            "entity_id": "list:IMP-002",
            "event_type": "list.created",
            "data": {"ref_id": "IMP-002", "status": "draft", "line_items": []},
            "source": "old_system",
            "idempotency_key": "imp-key-002",
        }
        r1 = await client.post("/lists/import", headers=_h(token), json=payload)
        assert r1.status_code == 200

        r2 = await client.post("/lists/import", headers=_h(token), json=payload)
        assert r2.status_code == 200
        assert r2.json()["idempotency_hit"] is True

    @pytest.mark.asyncio
    async def test_import_duplicate_entity_id_different_key_rejected(self, client):
        token = await _register(client)
        await client.post("/lists/import", headers=_h(token), json={
            "entity_id": "list:IMP-003", "event_type": "list.created",
            "data": {"ref_id": "IMP-003", "status": "draft", "line_items": []},
            "source": "old_system", "idempotency_key": "key-a",
        })
        r = await client.post("/lists/import", headers=_h(token), json={
            "entity_id": "list:IMP-003", "event_type": "list.created",
            "data": {"ref_id": "IMP-003", "status": "draft", "line_items": []},
            "source": "old_system", "idempotency_key": "key-b",
        })
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_import_lifecycle_event_on_existing(self, client):
        token = await _register(client)
        await client.post("/lists/import", headers=_h(token), json={
            "entity_id": "list:IMP-004", "event_type": "list.created",
            "data": {"ref_id": "IMP-004", "status": "draft", "line_items": []},
            "source": "old_system", "idempotency_key": "key-create-004",
        })
        r = await client.post("/lists/import", headers=_h(token), json={
            "entity_id": "list:IMP-004", "event_type": "list.finalized",
            "data": {"status": "finalized", "sent_at": "2026-06-17T00:00:00+00:00"},
            "source": "old_system", "idempotency_key": "key-finalize-004",
        })
        assert r.status_code == 200
        detail = (await client.get("/lists/list:IMP-004", headers=_h(token))).json()
        assert detail["status"] == "finalized"

    @pytest.mark.asyncio
    async def test_batch_import(self, client):
        token = await _register(client)
        records = [
            {
                "entity_id": f"list:BATCH-{i:03d}",
                "event_type": "list.created",
                "data": {"ref_id": f"BATCH-{i:03d}", "status": "draft", "line_items": [],
                         "customer_name": f"Customer {i}"},
                "source": "old_system",
                "idempotency_key": f"batch-key-{i:03d}",
            }
            for i in range(5)
        ]
        r = await client.post("/lists/import/batch", headers=_h(token), json={"records": records})
        assert r.status_code == 200
        body = r.json()
        assert body["created"] == 5
        assert body["skipped"] == 0
        assert body["errors"] == []

        # verify all exist
        listing = (await client.get("/lists", headers=_h(token))).json()
        assert listing["total"] == 5

    @pytest.mark.asyncio
    async def test_batch_import_skips_duplicates(self, client):
        token = await _register(client)
        record = {
            "entity_id": "list:BATCH-DUP",
            "event_type": "list.created",
            "data": {"ref_id": "BATCH-DUP", "status": "draft", "line_items": []},
            "source": "old_system",
            "idempotency_key": "batch-dup-key",
        }
        # first import
        await client.post("/lists/import/batch", headers=_h(token), json={"records": [record]})
        # second import with same data
        r = await client.post("/lists/import/batch", headers=_h(token), json={"records": [record]})
        body = r.json()
        assert body["created"] == 0
        assert body["skipped"] == 1


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestListSummary:

    @pytest.mark.asyncio
    async def test_summary_empty(self, client):
        token = await _register(client)
        r = await client.get("/lists/summary", headers=_h(token))
        assert r.status_code == 200
        body = r.json()
        assert body["total_count"] == 0
        assert body["draft_count"] == 0
        assert body["total_value"] == 0

    @pytest.mark.asyncio
    async def test_summary_with_data(self, client):
        token = await _register(client)
        eid1 = await _create_list(client, token)  # total=13000
        eid2 = await _create_list(client, token)  # total=13000
        # void one
        await client.post(f"/lists/{eid2}/void", headers=_h(token), json={})

        r = await client.get("/lists/summary", headers=_h(token))
        body = r.json()
        assert body["total_count"] == 2
        assert body["draft_count"] == 1
        assert body["count_by_status"]["draft"] == 1
        assert body["count_by_status"]["void"] == 1
        # voided lists excluded from total_value
        assert body["total_value"] == 13000


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

class TestListExportCSV:

    @pytest.mark.asyncio
    async def test_export_csv_headers_and_content(self, client):
        token = await _register(client)
        await _create_list(client, token, customer_name="CSV Corp")
        r = await client.get("/lists/export/csv", headers=_h(token))
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        lines = r.text.strip().split("\n")
        assert len(lines) == 2  # header + 1 data row
        header = lines[0]
        assert "id" in header
        assert "customer_name" in header
        assert "CSV Corp" in lines[1]

    @pytest.mark.asyncio
    async def test_export_csv_filters(self, client):
        token = await _register(client)
        await _create_list(client, token, list_type="sale", customer_name="A")
        await _create_list(client, token, list_type="consignment", customer_name="B")

        r = await client.get("/lists/export/csv?list_type=consignment", headers=_h(token))
        lines = r.text.strip().split("\n")
        assert len(lines) == 2
        assert "B" in lines[1]


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

class TestListAuthGuards:

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, client):
        r = await client.get("/lists")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, client):
        r = await client.get("/lists", headers=_h("bad-token"))
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Sequence numbering
# ---------------------------------------------------------------------------

class TestListSequence:

    @pytest.mark.asyncio
    async def test_sequential_ref_ids(self, client):
        token = await _register(client)
        eid1 = await _create_list(client, token)
        eid2 = await _create_list(client, token)

        ref1 = (await client.get(f"/lists/{eid1}", headers=_h(token))).json()["ref_id"]
        ref2 = (await client.get(f"/lists/{eid2}", headers=_h(token))).json()["ref_id"]

        n1 = int(ref1.split("-")[-1])
        n2 = int(ref2.split("-")[-1])
        assert n2 == n1 + 1


# ---------------------------------------------------------------------------
# Currency immutability
# ---------------------------------------------------------------------------

class TestListCurrencyImmutability:

    @pytest.mark.asyncio
    async def test_currency_cannot_be_changed_via_patch(self, client):
        token = await _register(client)
        eid = await _create_list(client, token, currency="THB")

        await client.patch(f"/lists/{eid}", headers=_h(token), json={
            "fields_changed": {"currency": {"old": "THB", "new": "USD"}},
        })
        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["currency"] == "THB"


# ---------------------------------------------------------------------------
# Totals recalculation via patch
# ---------------------------------------------------------------------------

class TestListTotalsRecalc:

    @pytest.mark.asyncio
    async def test_patch_line_items_recalculates_totals(self, client):
        token = await _register(client)
        eid = await _create_list(client, token, line_items=[
            {"name": "A", "quantity": 1, "unit_price": 1000, "line_total": 1000},
        ], discount=0, tax=0)

        await client.patch(f"/lists/{eid}", headers=_h(token), json={
            "fields_changed": {
                "line_items": {
                    "old": [{"name": "A", "quantity": 1, "unit_price": 1000, "line_total": 1000}],
                    "new": [
                        {"name": "A", "quantity": 1, "unit_price": 1000, "line_total": 1000},
                        {"name": "B", "quantity": 2, "unit_price": 500, "line_total": 1000},
                    ],
                },
            },
        })

        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["subtotal"] == 2000
        assert detail["total"] == 2000

    @pytest.mark.asyncio
    async def test_patch_discount_recalculates_totals(self, client):
        token = await _register(client)
        eid = await _create_list(client, token, line_items=[
            {"name": "X", "quantity": 1, "unit_price": 10000, "line_total": 10000},
        ], discount=0, tax=0)

        await client.patch(f"/lists/{eid}", headers=_h(token), json={
            "fields_changed": {
                "discount": {"old": 0, "new": 1000},
            },
        })

        detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
        assert detail["subtotal"] == 10000
        assert detail["discount_amount"] == 1000
        assert detail["total"] == 9000


@pytest.mark.asyncio
async def test_list_note_added(client):
    token = await _register(client)
    eid = (await client.post("/lists", headers=_h(token), json={"list_type": "quotation", "line_items": [], "currency": "USD"})).json()["id"]

    # Add a note using new field name
    r = await client.post(f"/lists/{eid}/notes", headers=_h(token), json={"note": "List note 1"})
    assert r.status_code == 200
    assert r.json()["id"].startswith("note:")

    notes = (await client.get(f"/lists/{eid}/notes", headers=_h(token))).json()
    assert len(notes) == 1
    assert notes[0]["note"] == "List note 1"

    # Empty note rejected
    bad = await client.post(f"/lists/{eid}/notes", headers=_h(token), json={"note": ""})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_list_totals_correct_after_patch_with_per_line_tax(client):
    """Patching line items with per-line tax rates yields correct totals.

    Regression for the bug where save_list_lines sent tax as an amount and
    _recalc_list_totals incorrectly treated it as a percentage rate.
    """
    token = await _register(client)
    # Create list with one line: qty=2, unit_price=100, line_total=200, tax_rate=10%
    eid = (await client.post("/lists", headers=_h(token), json={
        "list_type": "quotation",
        "line_items": [{"name": "Widget", "quantity": 2, "unit_price": 100, "line_total": 200, "tax_rate": 10}],
        "subtotal": 200,
        "discount": 0,
        "discount_type": "flat",
        "tax": 0,
        "total": 220,
        "currency": "USD",
    })).json()["id"]

    detail = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
    assert detail["subtotal"] == 200, f"subtotal wrong: {detail}"
    # Per-line tax: 200 * 10% = 20, total = 220
    assert detail["total"] == 220, f"total wrong after create: {detail}"

    # Now patch (simulating what save_list_lines does)
    fields_changed = {
        "line_items": {"old": None, "new": [{"name": "Widget", "quantity": 2, "unit_price": 100, "line_total": 200, "tax_rate": 10}]},
        "subtotal": {"old": None, "new": 200},
        "tax": {"old": None, "new": 20},   # JS sends tax AMOUNT not rate - this was the bug
        "total": {"old": None, "new": 220},
    }
    r = await client.patch(f"/lists/{eid}", headers=_h(token), json={"fields_changed": fields_changed})
    assert r.status_code == 200

    detail2 = (await client.get(f"/lists/{eid}", headers=_h(token))).json()
    # Total must still be 220 - not inflated by treating 20 as a 20% rate
    assert detail2["total"] == 220, f"total wrong after patch: {detail2}"
    assert detail2["subtotal"] == 200, f"subtotal wrong after patch: {detail2}"


# ---------------------------------------------------------------------------
# Revert sent list to draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_revert_finalized_to_draft(client):
    """A finalized list can be reverted to draft."""
    token = await _register(client)
    eid = await _create_list(client, token)
    r = await client.post(f"/lists/{eid}/finalize", headers=_h(token))
    assert r.status_code == 200
    assert (await client.get(f"/lists/{eid}", headers=_h(token))).json()["status"] == "finalized"
    r = await client.post(f"/lists/{eid}/revert-to-draft", headers=_h(token), json={"reason": "test"})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/lists/{eid}", headers=_h(token))).json()["status"] == "draft"


@pytest.mark.asyncio
async def test_list_revert_only_from_finalized(client):
    """Reverting a draft or a closed list returns 409."""
    token = await _register(client)
    eid = await _create_list(client, token)
    # Draft - cannot revert
    assert (await client.post(f"/lists/{eid}/revert-to-draft", headers=_h(token), json={})).status_code == 409
    # Close it (finalize -> convert), then revert is rejected (terminal action already ran)
    await client.post(f"/lists/{eid}/finalize", headers=_h(token))
    await client.post(f"/lists/{eid}/convert", headers=_h(token), json={"target_type": "invoice"})
    assert (await client.post(f"/lists/{eid}/revert-to-draft", headers=_h(token), json={})).status_code == 409


@pytest.mark.asyncio
async def test_transfer_move_relocates_items_and_stays_finalized(client):
    """A finalized transfer's 'Move all to <location>' relocates every line item (via the inventory
    item.transferred event) and stays finalized so it can be moved again (repeatable, no close)."""
    token = await _register(client)
    h = _h(token)
    loc_a = (await client.post("/companies/me/locations", headers=h, json={"name": "A", "type": "warehouse"})).json()["id"]
    loc_b = (await client.post("/companies/me/locations", headers=h, json={"name": "B", "type": "warehouse"})).json()["id"]
    it = (await client.post("/items", headers=h, json={
        "sku": "MV1", "name": "Mover", "sell_by": "piece", "quantity": 5, "location_id": loc_a})).json()["id"]
    eid = (await client.post("/lists", headers=h, json={
        "list_type": "transfer", "line_items": [{"item_id": it, "sku": "MV1", "name": "Mover", "quantity": 5}]})).json()["id"]

    # Move before Issue -> 409 (must finalize first).
    assert (await client.post(f"/lists/{eid}/move", headers=h, json={"to_location_id": loc_b})).status_code == 409
    await client.post(f"/lists/{eid}/finalize", headers=h)
    r = await client.post(f"/lists/{eid}/move", headers=h, json={"to_location_id": loc_b})
    assert r.status_code == 200 and r.json()["moved"] == 1
    assert (await client.get(f"/items/{it}", headers=h)).json()["location_id"] == loc_b
    # Repeatable: still finalized, can move again.
    assert (await client.get(f"/lists/{eid}", headers=h)).json()["status"] == "finalized"
    assert (await client.post(f"/lists/{eid}/move", headers=h, json={"to_location_id": loc_a})).status_code == 200
    assert (await client.get(f"/items/{it}", headers=h)).json()["location_id"] == loc_a


@pytest.mark.asyncio
async def test_only_transfer_can_move(client):
    token = await _register(client)
    h = _h(token)
    loc = (await client.post("/companies/me/locations", headers=h, json={"name": "A", "type": "warehouse"})).json()["id"]
    eid = await _create_list(client, token, list_type="quotation")
    await client.post(f"/lists/{eid}/finalize", headers=h)
    assert (await client.post(f"/lists/{eid}/move", headers=h, json={"to_location_id": loc})).status_code == 409


@pytest.mark.asyncio
async def test_change_type_while_issued(client):
    """A list's type can be changed while draft or issued (finalized); the change is recorded and the
    status is preserved. Switching a finalized list to audit re-freezes the on-hand baseline. Closed
    lists are terminal and reject the change."""
    token = await _register(client)
    h = _h(token)
    loc = (await client.post("/companies/me/locations", headers=h, json={"name": "WH", "type": "warehouse"})).json()["id"]
    it = (await client.post("/items", headers=h, json={"sku": "CT1", "name": "W", "sell_by": "piece", "quantity": 12, "location_id": loc})).json()["id"]

    eid = (await client.post("/lists", headers=h, json={
        "list_type": "transfer", "line_items": [{"item_id": it, "sku": "CT1", "name": "W", "quantity": 3}]})).json()["id"]
    await client.post(f"/lists/{eid}/finalize", headers=h)

    # Issued transfer -> quotation: type changes, status stays finalized.
    r = await client.post(f"/lists/{eid}/change-type", headers=h, json={"list_type": "quotation"})
    assert r.status_code == 200
    st = (await client.get(f"/lists/{eid}", headers=h)).json()
    assert st["list_type"] == "quotation" and st["status"] == "finalized"

    # Issued -> audit re-freezes on-hand from live qty (12), so Adjust/variance have a baseline.
    r = await client.post(f"/lists/{eid}/change-type", headers=h, json={"list_type": "audit"})
    assert r.status_code == 200
    st = (await client.get(f"/lists/{eid}", headers=h)).json()
    assert st["list_type"] == "audit" and st["line_items"][0]["on_hand"] == 12.0

    # Unknown type -> 422.
    assert (await client.post(f"/lists/{eid}/change-type", headers=h, json={"list_type": "nope"})).status_code == 422

    # Closed list -> 409 (terminal).
    q = await _create_list(client, token, list_type="quotation")
    await client.post(f"/lists/{q}/finalize", headers=h)
    await client.post(f"/lists/{q}/convert", headers=h, json={"target_type": "invoice"})
    assert (await client.post(f"/lists/{q}/change-type", headers=h, json={"list_type": "transfer"})).status_code == 409
