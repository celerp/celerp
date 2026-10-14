# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The document scan bar submits through the same proxy as the list scan bar: one batched
post to /docs/{id}/scan, the backend's own reply (failures, version, a run conflict)
passed straight back, and nothing sent for a blank field or a lost session."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio


async def _post(ui_client, data, *, scan, cookies=True):
    with patch("ui.api_client.scan_codes", new=scan):
        return await ui_client.post("/docs/doc:INV-1/scan", data=data,
                                    cookies=_authed() if cookies else {})


async def test_doc_scan_proxy_posts_batch(ui_client):
    results = [{"code": "A1", "line_id": "l1"}, {"code": "A1", "line_id": "l1"}, {"code": "B2", "line_id": "l2"}]
    scan = AsyncMock(return_value={"scanned": 3, "results": results, "failed": [], "version": 7})
    r = await _post(ui_client, {"barcode": "A1,A1,B2", "price_list": "Retail", "run_key": "run-1"}, scan=scan)
    assert r.status_code == 200, r.text
    assert r.json() == {"scanned": 3, "failed": [], "html": "", "version": 7, "lines": ["l1", "l2"]}
    assert scan.await_args.args[1:] == ("docs", "doc:INV-1", "A1,A1,B2", "Retail", "run-1")


async def test_doc_scan_proxy_preserves_run_conflict(ui_client):
    msg = "This scan run was already recorded against a different batch."
    err = APIError(409, msg, data={"code": "scan_run_conflict", "detail": msg, "version": 9})
    r = await _post(ui_client, {"barcode": "A1", "run_key": "run-1"}, scan=AsyncMock(side_effect=err))
    assert r.status_code == 409
    assert r.json() == {"code": "scan_run_conflict", "detail": msg, "version": 9}


async def test_doc_scan_proxy_blank_is_noop(ui_client):
    scan = AsyncMock()
    r = await _post(ui_client, {"barcode": "  ", "run_key": "run-1"}, scan=scan)
    assert r.status_code == 200
    assert r.json()["scanned"] == 0
    scan.assert_not_awaited()


async def test_doc_scan_proxy_requires_session(ui_client):
    scan = AsyncMock()
    r = await _post(ui_client, {"barcode": "A1"}, scan=scan, cookies=False)
    scan.assert_not_awaited()
    with patch("ui.api_client.scan_codes", new=AsyncMock()):
        expected = await ui_client.post("/lists/list:L1/scan", data={"barcode": "A1"})
    assert (r.status_code, r.headers.get("location")) == (expected.status_code, expected.headers.get("location"))
    assert r.status_code in (302, 303, 401)


async def test_doc_scan_failure_keeps_field(ui_client):
    r = await _post(ui_client, {"barcode": "A1", "run_key": "run-1"},
                    scan=AsyncMock(side_effect=APIError(503, "Service unavailable")))
    assert r.status_code == 503
    assert r.json() == {"detail": "Service unavailable"}


async def test_doc_page_scan_uses_batch_primitive():
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_detail

    doc = {"entity_id": "doc:mine", "id": "doc:mine", "doc_type": "invoice", "status": "draft", "line_items": []}
    html = to_xml(_doc_detail(doc))
    assert 'id="scan-bar-add"' in html
    assert "_CELERP_BASE + _CELERP_EID + '/scan'" in html


async def test_activity_hides_scan_runs():
    from ui.components.activity import _fields_changed_summary

    change = {"scan_runs": {"old": [], "new": [{"key": "run-1", "fp": "x", "scanned": 1, "failed": []}]}}
    assert _fields_changed_summary(change) == ""
