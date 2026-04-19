# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Final coverage gap closers targeting:
- items.py  128-130, 134-136, 140-142  (valuation with total_cost/wholesale/retail via import)
- reports.py 166, 182, 184, 186        (AP aging buckets current, d30, d60, d90, d90plus)
- reports.py 448-455, 540              (purchases price_range group_by, expiring days_remaining>days)
- share.py   53                        (share_url with celerp_public_url set)
- share.py   129, 164                  (share token entity missing, list entity share view)
- share.py   197-212                   (import shared doc: HTTPStatusError 4xx, 5xx, 502 network)
- share.py   268, 270                  (bundle: token not found, entity not found)
- share.py   489-490                   (public list page discount row)
- health.py  27-28                     (readiness DB error path)
- crm.py     354-355, 638-640          (memo summary bad decimal, batch import memos error)
- lists.py   135, 137, 187-188, 399    (list filter date_from/to, csv q filter, import template)
- lists.py   438-439, 451-453          (batch skip existing entity, error capture)
- manufacturing.py 243, 303-305        (import template, batch error capture)
- manufacturing.py 328, 359, 372, 414  (order validations + step endpoint)
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client, name=None) -> str:
    addr = f"{uuid.uuid4().hex[:8]}@final.test"
    cname = name or f"FinalCo-{uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/auth/register",
        json={"company_name": cname, "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# items.py lines 128-130, 134-136, 140-142: valuation with actual field names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_valuation_with_total_cost_and_retail(client):
    """GET /items/valuation hits total_cost/wholesale_price/retail_price accumulation lines."""
    tok = await _reg(client)
    entity_id = f"item:{uuid.uuid4()}"
    r = await client.post("/items/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": entity_id,
            "event_type": "item.created",
            "source": "test",
            "idempotency_key": f"v-{uuid.uuid4().hex}",
            "data": {
                "sku": f"TC-{uuid.uuid4().hex[:6]}",
                "name": "ValuationItem",
                "quantity": 1,
                "total_cost": 100.0,
                "wholesale_price": 120.0,
                "retail_price": 150.0,
            },
        }
    ]})
    assert r.status_code == 200, r.text

    r2 = await client.get("/items/valuation", headers=_h(tok))
    assert r2.status_code == 200
    body = r2.json()
    assert body["cost_total"] >= 100.0
    assert body["wholesale_total"] >= 120.0
    assert body["retail_total"] >= 150.0


@pytest.mark.asyncio
async def test_items_valuation_bad_decimal_values(client):
    """GET /items/valuation with non-numeric field values hits except Exception pass (lines 129, 135, 141)."""
    tok = await _reg(client)
    entity_id = f"item:{uuid.uuid4()}"
    r = await client.post("/items/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": entity_id,
            "event_type": "item.created",
            "source": "test",
            "idempotency_key": f"bd-{uuid.uuid4().hex}",
            "data": {
                "sku": f"BD-{uuid.uuid4().hex[:6]}",
                "name": "BadDecimalItem",
                "total_cost": "not-a-number",
                "wholesale_price": "invalid",
                "retail_price": "also-bad",
            },
        }
    ]})
    assert r.status_code == 200, r.text

    # Should not raise — bad decimal values are silently skipped
    r2 = await client.get("/items/valuation", headers=_h(tok))
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# reports.py lines 182, 184, 186, 166: AP aging d30/d60/d90/d90plus + skip zero
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reports_ap_aging_all_buckets(client):
    """AP aging: create POs with overdue dates spanning d30/d60/d90/d90plus buckets."""
    tok = await _reg(client)
    today = date.today()

    # days_overdue <= 30 → current, <=30 → d30, <=60 → d60, <=90 → d90, >90 → d90plus
    # Note: ap-aging reads from projection state; due_date must be in the stored state
    for days_ago, expected_bucket, supplier_id in [
        (20, "d30", "sup-20d"),    # 20 days overdue → d30
        (45, "d60", "sup-45d"),    # 45 days overdue → d60
        (75, "d90", "sup-75d"),    # 75 days overdue → d90
        (100, "d90plus", "sup-100d"),  # 100 days overdue → d90plus
    ]:
        due = (today - timedelta(days=days_ago)).isoformat()
        r = await client.post("/docs", headers=_h(tok), json={
            "doc_type": "purchase_order",
            "contact_id": supplier_id,
            "line_items": [],
            "subtotal": 100.0,
            "tax": 0,
            "total": 100.0,
            "due_date": due,
            "amount_outstanding": 100.0,
        })
        assert r.status_code == 200, r.text

    # Zero outstanding → skip (line 166)
    await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order",
        "contact_id": "sup-zero",
        "line_items": [],
        "subtotal": 0,
        "tax": 0,
        "total": 0,
    })

    r = await client.get("/reports/ap-aging", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    assert "lines" in body
    totals = {ln["supplier_id"]: ln for ln in body["lines"]}
    assert totals.get("sup-20d", {}).get("d30", 0) >= 100.0
    assert totals.get("sup-45d", {}).get("d60", 0) >= 100.0
    assert totals.get("sup-75d", {}).get("d90", 0) >= 100.0
    assert totals.get("sup-100d", {}).get("d90plus", 0) >= 100.0
    assert "sup-zero" not in totals


# ---------------------------------------------------------------------------
# reports.py line 540: expiring items - days_remaining > days skip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reports_expiring_days_remaining_skip(client):
    """Item expiring after `days` window is skipped (line 540)."""
    tok = await _reg(client)
    today = date.today()

    # Item expiring in 365 days — should NOT appear in 30-day window
    far_future = (today + timedelta(days=365)).isoformat()
    r = await client.post("/items/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "source": "test",
            "idempotency_key": f"exp-far-{uuid.uuid4().hex}",
            "data": {"sku": "EXP-FAR", "name": "Far Future", "expires_at": far_future},
        }
    ]})
    assert r.status_code == 200

    # Item expiring in 5 days — SHOULD appear
    near_future = (today + timedelta(days=5)).isoformat()
    r2 = await client.post("/items/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "source": "test",
            "idempotency_key": f"exp-near-{uuid.uuid4().hex}",
            "data": {"sku": "EXP-NEAR", "name": "Near Future", "expires_at": near_future},
        }
    ]})
    assert r2.status_code == 200

    r3 = await client.get("/reports/expiring?days=30", headers=_h(tok))
    assert r3.status_code == 200
    body = r3.json()
    skus = [l["sku"] for l in body["lines"]]
    assert "EXP-NEAR" in skus
    assert "EXP-FAR" not in skus


# ---------------------------------------------------------------------------
# reports.py lines 448-455: purchases price_range group_by
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reports_purchases_price_range(client):
    """GET /reports/purchases?group_by=price_range groups POs into price buckets."""
    tok = await _reg(client)

    for total in [500, 3000, 10000, 25000]:
        await client.post("/docs", headers=_h(tok), json={
            "doc_type": "purchase_order",
            "contact_id": "sup-pr",
            "line_items": [],
            "subtotal": total,
            "tax": 0,
            "total": total,
        })

    r = await client.get("/reports/purchases?group_by=price_range", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    assert "lines" in body
    price_ranges = {ln["price_range"] for ln in body["lines"]}
    assert "0-1000" in price_ranges
    assert "1001-5000" in price_ranges
    assert "5001-20000" in price_ranges
    assert "20000+" in price_ranges


# ---------------------------------------------------------------------------
# share.py line 53: _share_url with celerp_public_url set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_url_with_public_url(client):
    """Share URL includes src= param when CELERP_PUBLIC_URL is set (line 53)."""
    tok = await _reg(client)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c1",
        "line_items": [], "subtotal": 10, "tax": 0, "total": 10,
    })
    doc_id = r.json()["id"]

    with patch("celerp_docs.routes_share.settings") as mock_settings:
        mock_settings.celerp_public_url = "https://my.celerp.instance"
        r2 = await client.post(f"/docs/{doc_id}/share", headers=_h(tok))
        assert r2.status_code == 200
        # "url" key is the share link
        share_url = r2.json()["url"]
        assert "src=" in share_url


# ---------------------------------------------------------------------------
# share.py line 129: GET /share/{token} with doc projection missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_view_doc_projection_missing(client, session):
    """GET /share/{token} when projection was deleted returns 404 HTML (line 129)."""
    from celerp.models.projections import Projection
    from sqlalchemy import delete as sa_delete

    tok = await _reg(client)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c1",
        "line_items": [], "subtotal": 5, "tax": 0, "total": 5,
    })
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/share", headers=_h(tok))
    share_token = r2.json()["token"]

    # Delete the projection using the shared test session
    await session.execute(sa_delete(Projection).where(Projection.entity_id == doc_id))
    await session.commit()

    r3 = await client.get(f"/share/{share_token}")
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# share.py line 164: GET /share/{token} for a list entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_view_list_entity(client):
    """GET /share/{token} for a list entity returns the list HTML view (line 164)."""
    tok = await _reg(client)

    # Create a doc, then use the share endpoint directly with a list entity
    # Strategy: create a doc (we know that works), get share token
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c1",
        "line_items": [], "subtotal": 10, "tax": 0, "total": 10,
    })
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/share", headers=_h(tok))
    assert r2.status_code == 200

    # Now test list: we need the projection to have entity_type="list"
    # Use import/batch to create a list projection
    list_id = f"list:{uuid.uuid4()}"
    r3 = await client.post("/lists/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": list_id,
            "event_type": "list.created",
            "source": "test",
            "idempotency_key": f"list-share-{uuid.uuid4().hex}",
            "data": {"list_type": "price_list", "ref_id": "PL-001", "customer_name": "Test Customer", "total": 100},
        }
    ]})
    assert r3.status_code == 200, r3.text

    r4 = await client.post(f"/docs/{list_id}/share", headers=_h(tok))
    assert r4.status_code == 200, r4.text
    share_token = r4.json()["token"]

    r5 = await client.get(f"/share/{share_token}")
    assert r5.status_code == 200
    assert "text/html" in r5.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# share.py lines 197-212: GET /docs/import HTTP errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_import_doc_http_404(client):
    """GET /docs/import when sender returns 404 → 404 (lines 206-207)."""
    tok = await _reg(client)

    import httpx
    mock_response = MagicMock()
    mock_response.status_code = 404
    exc = httpx.HTTPStatusError("not found", request=MagicMock(), response=mock_response)

    with patch("celerp_docs.routes_share.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=exc)

        r = await client.get(
            "/docs/import?src=https://other.celerp.test&token=fake-token",
            headers=_h(tok),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_share_import_doc_http_502(client):
    """GET /docs/import when sender returns 5xx → 502 (lines 207-208)."""
    tok = await _reg(client)

    import httpx
    mock_response = MagicMock()
    mock_response.status_code = 503
    exc = httpx.HTTPStatusError("server error", request=MagicMock(), response=mock_response)

    with patch("celerp_docs.routes_share.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=exc)

        r = await client.get(
            "/docs/import?src=https://other.celerp.test&token=fake-token",
            headers=_h(tok),
        )
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_share_import_doc_network_error(client):
    """GET /docs/import when network fails → 502 (lines 209-210)."""
    tok = await _reg(client)

    with patch("celerp_docs.routes_share.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=ConnectionError("network fail"))

        r = await client.get(
            "/docs/import?src=https://other.celerp.test&token=fake-token",
            headers=_h(tok),
        )
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# share.py lines 337-338: GET /share/{token}/bundle token not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_bundle_token_not_found(client):
    """GET /share/{token}/bundle with nonexistent token → 404 (lines 337-338)."""
    r = await client.get("/share/nonexistent-token-xyz123/bundle")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# share.py lines 268, 270: GET /share/{token}/bundle entity missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_bundle_doc_missing(client, session):
    """GET /share/{token}/bundle when projection deleted → 404 (line 270)."""
    from celerp.models.projections import Projection
    from sqlalchemy import delete as sa_delete

    tok = await _reg(client)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c1",
        "line_items": [], "subtotal": 5, "tax": 0, "total": 5,
    })
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/share", headers=_h(tok))
    share_token = r2.json()["token"]

    await session.execute(sa_delete(Projection).where(Projection.entity_id == doc_id))
    await session.commit()

    r3 = await client.get(f"/share/{share_token}/bundle")
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# share.py lines 489-490: _public_list_page discount row rendered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_share_public_list_page_discount_row(client):
    """GET /share/{token} for list with discount > 0 renders discount HTML (lines 489-490)."""
    tok = await _reg(client)
    list_id = f"list:{uuid.uuid4()}"
    r = await client.post("/lists/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": list_id,
            "event_type": "list.created",
            "source": "test",
            "idempotency_key": f"disc-list-{uuid.uuid4().hex}",
            "data": {
                "list_type": "price_list",
                "ref_id": "DISC-001",
                "customer_name": "Customer A",
                "total": 90,
                "discount": 10,
                "discount_type": "flat",
                "items": [{"description": "Widget", "quantity": 1, "unit_price": 100}],
            },
        }
    ]})
    assert r.status_code == 200, r.text

    r2 = await client.post(f"/docs/{list_id}/share", headers=_h(tok))
    assert r2.status_code == 200, r2.text
    share_token = r2.json()["token"]

    r3 = await client.get(f"/share/{share_token}")
    assert r3.status_code == 200
    assert "Discount" in r3.text


# ---------------------------------------------------------------------------
# health.py lines 27-28: readiness endpoint DB error path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_readiness_db_error(client):
    """GET /health/ready when DB execute raises → 503 (lines 27-28)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    # Patch the dependency override in the app directly
    from celerp.main import app
    from celerp.db import get_session

    async def _failing_session():
        mock = AsyncMock(spec=AsyncSession)
        mock.execute = AsyncMock(side_effect=Exception("DB down"))
        yield mock

    app.dependency_overrides[get_session] = _failing_session
    try:
        r = await client.get("/health/ready")
        assert r.status_code == 503
        assert "DB not reachable" in r.json()["detail"]
    finally:
        del app.dependency_overrides[get_session]


# ---------------------------------------------------------------------------
# crm.py lines 354-355: memo summary with bad decimal total
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_memo_summary_bad_decimal(client):
    """GET /crm/memos/summary with non-numeric total hits except Exception pass (lines 354-355)."""
    tok = await _reg(client)

    # Import a memo with a non-numeric total
    memo_id = f"crm:memo:{uuid.uuid4()}"
    r = await client.post("/crm/memos/import", headers=_h(tok), json={
        "entity_id": memo_id,
        "event_type": "crm.memo.created",
        "source": "test",
        "idempotency_key": f"bad-total-{uuid.uuid4().hex}",
        "data": {"contact_id": "c1", "title": "Bad Total Memo", "total": "not-a-number", "status": "out"},
    })
    assert r.status_code == 200, r.text

    # Should not raise — bad decimal is silently skipped
    r2 = await client.get("/crm/memos/summary", headers=_h(tok))
    assert r2.status_code == 200
    body = r2.json()
    assert "all_total" in body
    assert body["memo_count"] >= 1


# ---------------------------------------------------------------------------
# crm.py lines 638-640: batch import memos error capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_batch_import_memos_error_capture(client):
    """CRM memos batch import captures per-record errors (lines 638-640)."""
    tok = await _reg(client)
    r = await client.post("/crm/memos/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": f"crm:memo:{uuid.uuid4()}",
            "event_type": "crm.memo.bad_type",  # unknown → ValueError
            "data": {},
            "source": "test",
            "idempotency_key": f"memo-err-{uuid.uuid4().hex}",
        },
    ]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) >= 1


# ---------------------------------------------------------------------------
# lists.py lines 135, 137: GET /lists with date_from and date_to filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_date_filter(client):
    """GET /lists?date_from=...&date_to=... filters by created_at/date (lines 135, 137)."""
    tok = await _reg(client)
    today_str = date.today().isoformat()
    list_id = f"list:{uuid.uuid4()}"
    await client.post("/lists/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": list_id,
            "event_type": "list.created",
            "source": "test",
            "idempotency_key": f"date-flt-{uuid.uuid4().hex}",
            "data": {
                "list_type": "price_list",
                "ref_id": "DATEFLT-001",
                "customer_name": "DateFilterCo",
                "total": 50,
                "date": today_str,
            },
        }
    ]})

    # Wide range — should include the list
    r1 = await client.get(f"/lists?date_from=2020-01-01&date_to=2099-12-31", headers=_h(tok))
    assert r1.status_code == 200
    assert len(r1.json()["items"]) >= 1

    # date_to in the past — should return no matches
    r2 = await client.get("/lists?date_from=2020-01-01&date_to=2020-12-31", headers=_h(tok))
    assert r2.status_code == 200
    assert r2.json()["items"] == []


# ---------------------------------------------------------------------------
# lists.py lines 187-188: GET /lists/export/csv with q filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_export_csv_q_filter(client):
    """GET /lists/export/csv?q=... filters by ref_id or customer_name (lines 187-188)."""
    tok = await _reg(client)
    list_id = f"list:{uuid.uuid4()}"
    await client.post("/lists/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": list_id,
            "event_type": "list.created",
            "source": "test",
            "idempotency_key": f"csv-q-{uuid.uuid4().hex}",
            "data": {"list_type": "price_list", "ref_id": "UNIQUE-CSV-Q", "customer_name": "BigCo", "total": 50},
        }
    ]})

    r = await client.get("/lists/export/csv?q=unique-csv-q", headers=_h(tok))
    assert r.status_code == 200
    assert "UNIQUE-CSV-Q" in r.text

    r2 = await client.get("/lists/export/csv?q=xnotfoundzzz", headers=_h(tok))
    assert r2.status_code == 200
    lines = [line for line in r2.text.strip().split("\n") if line]
    assert len(lines) == 1  # header only


# ---------------------------------------------------------------------------
# lists.py line 399: GET /lists/import/template
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_import_template(client):
    """GET /lists/import/template returns CSV template (line 399)."""
    tok = await _reg(client)
    r = await client.get("/lists/import/template", headers=_h(tok))
    assert r.status_code == 200
    assert "entity_id" in r.text


# ---------------------------------------------------------------------------
# lists.py lines 438-439, 451-453: batch import skip existing entity + error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_batch_import_skip_existing_and_error(client):
    """lists batch import: skip existing entity and capture error (lines 438-439, 451-453)."""
    tok = await _reg(client)
    entity_id = f"list:{uuid.uuid4()}"

    # First import
    r1 = await client.post("/lists/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": entity_id,
            "event_type": "list.created",
            "data": {"list_type": "price_list", "ref_id": "PL-BATCH"},
            "source": "test",
            "idempotency_key": f"bl-ok-{uuid.uuid4().hex}",
        },
    ]})
    assert r1.status_code == 200
    assert r1.json()["created"] == 1

    # Second import: same entity_id different key → skip; plus error record
    r2 = await client.post("/lists/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": entity_id,
            "event_type": "list.created",
            "data": {"list_type": "price_list", "ref_id": "PL-BATCH-DUP"},
            "source": "test",
            "idempotency_key": f"bl-dup-{uuid.uuid4().hex}",
        },
        {
            "entity_id": f"list:{uuid.uuid4()}",
            "event_type": "list.bad_event_type",
            "data": {},
            "source": "test",
            "idempotency_key": f"bl-err-{uuid.uuid4().hex}",
        },
    ]})
    assert r2.status_code == 200
    body = r2.json()
    assert body["skipped"] == 1
    assert len(body["errors"]) >= 1


# ---------------------------------------------------------------------------
# manufacturing.py line 243: GET /manufacturing/import/template
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_import_template(client):
    """GET /manufacturing/import/template returns CSV template (line 243)."""
    tok = await _reg(client)
    r = await client.get("/manufacturing/import/template", headers=_h(tok))
    assert r.status_code == 200
    assert "entity_id" in r.text


# ---------------------------------------------------------------------------
# manufacturing.py lines 303-305: batch import error capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_batch_import_error(client):
    """manufacturing batch import captures errors (lines 303-305)."""
    tok = await _reg(client)
    r = await client.post("/manufacturing/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": f"bom:{uuid.uuid4()}",
            "event_type": "bom.bad_event_type",
            "data": {},
            "source": "test",
            "idempotency_key": f"mfg-err-{uuid.uuid4().hex}",
        },
    ]})
    assert r.status_code == 200
    assert len(r.json()["errors"]) >= 1


# ---------------------------------------------------------------------------
# manufacturing.py line 328: create order empty description → 422
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_create_order_empty_description(client):
    """POST /manufacturing with whitespace-only description → 422 (line 328)."""
    tok = await _reg(client)
    r = await client.post("/manufacturing", headers=_h(tok), json={
        "description": "   ",
        "inputs": [{"item_id": "item:x", "quantity": 1}],
        "outputs": [{"sku": "OUT", "name": "Output", "quantity": 1}],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# manufacturing.py line 359: start order already completed → 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_start_order_already_completed(client):
    """POST /manufacturing/{id}/start on completed order → 409 (line 359)."""
    tok = await _reg(client)

    item_r = await client.post("/items", headers=_h(tok), json={
        "sku": f"MFG-IN-{uuid.uuid4().hex[:4]}", "sell_by": "piece", "name": "Input Item", "quantity": 100,
    })
    item_id = item_r.json()["id"]

    r = await client.post("/manufacturing", headers=_h(tok), json={
        "description": "Test Order",
        "inputs": [{"item_id": item_id, "quantity": 1}],
        "outputs": [{"sku": "OUT-01", "name": "Output", "quantity": 1}],
    })
    assert r.status_code == 200
    order_id = r.json()["id"]

    # Mark as completed via batch import
    await client.post("/manufacturing/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": order_id,
            "event_type": "mfg.order.completed",
            "data": {"completed_by": str(uuid.UUID(int=0))},
            "source": "test",
            "idempotency_key": f"cmp-{uuid.uuid4().hex}",
        },
    ]})

    r2 = await client.post(f"/manufacturing/{order_id}/start", headers=_h(tok))
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# manufacturing.py line 372: consume on closed order → 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_consume_closed_order(client):
    """POST /manufacturing/{id}/consume on completed order → 409 (line 372)."""
    tok = await _reg(client)

    item_r = await client.post("/items", headers=_h(tok), json={
        "sku": f"MFG-CN-{uuid.uuid4().hex[:4]}", "sell_by": "piece", "name": "Consume Input", "quantity": 100,
    })
    item_id = item_r.json()["id"]

    r = await client.post("/manufacturing", headers=_h(tok), json={
        "description": "Consume Test",
        "inputs": [{"item_id": item_id, "quantity": 1}],
        "outputs": [{"sku": "COUT-01", "name": "Output", "quantity": 1}],
    })
    order_id = r.json()["id"]

    await client.post("/manufacturing/import/batch", headers=_h(tok), json={"records": [
        {
            "entity_id": order_id,
            "event_type": "mfg.order.completed",
            "data": {"completed_by": str(uuid.UUID(int=0))},
            "source": "test",
            "idempotency_key": f"cmp2-{uuid.uuid4().hex}",
        },
    ]})

    r2 = await client.post(f"/manufacturing/{order_id}/consume", headers=_h(tok), json={
        "item_id": item_id,
        "quantity": 1,
    })
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# manufacturing.py line 414: POST /manufacturing/{id}/step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_complete_step(client):
    """POST /manufacturing/{id}/step records a step completion (line 414)."""
    tok = await _reg(client)

    item_r = await client.post("/items", headers=_h(tok), json={
        "sku": f"MFG-ST-{uuid.uuid4().hex[:4]}", "sell_by": "piece", "name": "Step Input", "quantity": 100,
    })
    item_id = item_r.json()["id"]

    r = await client.post("/manufacturing", headers=_h(tok), json={
        "description": "Step Test",
        "inputs": [{"item_id": item_id, "quantity": 1}],
        "outputs": [{"sku": "SOUT-01", "name": "Output", "quantity": 1}],
    })
    order_id = r.json()["id"]

    r2 = await client.post(f"/manufacturing/{order_id}/step", headers=_h(tok), json={
        "step_id": "mixing",
        "notes": "Mixed well",
    })
    assert r2.status_code == 200
    assert "event_id" in r2.json()
