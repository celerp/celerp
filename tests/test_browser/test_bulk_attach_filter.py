# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test for M5 — bulk-attach result matched/unmatched filter.

Uploads a ZIP with one SKU-matching file (→ ok) and one non-matching file
(→ unmatched), then exercises the client-side filter pills (All / Matched /
Unmatched) and asserts rows show/hide accordingly.
"""
from __future__ import annotations

import io
import zipfile

import pytest

pytestmark = pytest.mark.browser


def _make_zip(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            # minimal JPEG-ish bytes; store_upload only needs a readable blob
            zf.writestr(n, b"\xff\xd8\xff\xe0" + b"fake-image-bytes")
    return buf.getvalue()


def test_bulk_attach_result_status_filter(page, fresh_company, ui_server, tmp_path):
    api = fresh_company

    # Seed one item so a file matches by SKU.
    sku = "BULKFILT1"
    r = api.post("/items", json={"sku": sku, "name": "Filter Test", "quantity": 1, "sell_by": "piece"})
    assert r.status_code in (200, 201), f"item create failed: {r.text}"

    # ZIP: one matching (→ ok) + one non-matching (→ unmatched).
    zip_path = tmp_path / "attach.zip"
    zip_path.write_bytes(_make_zip([f"{sku}.jpg", "NOSUCHSKU.jpg"]))

    page.goto(f"{ui_server}/settings/inventory?tab=bulk-attach", wait_until="domcontentloaded")
    page.locator("input[type='file']").first.set_input_files(str(zip_path))
    page.locator("form:has(input[type='file']) button[type='submit']").first.click()

    # Result rows render after the HTMX swap.
    page.wait_for_selector("#bulk-attach-result tbody tr", timeout=15000)
    ok_row = page.locator("#bulk-attach-result tbody tr[data-status='ok']")
    un_row = page.locator("#bulk-attach-result tbody tr[data-status='unmatched']")
    assert ok_row.count() == 1, "expected one matched (ok) row"
    assert un_row.count() == 1, "expected one unmatched row"

    # Default: everything visible.
    assert ok_row.is_visible() and un_row.is_visible()

    # Filter → Unmatched: only the unmatched row remains.
    page.locator(".bulk-filter-btn[data-filter='unmatched']").click()
    assert un_row.is_visible()
    assert ok_row.is_hidden()

    # Filter → Matched: only the ok row remains.
    page.locator(".bulk-filter-btn[data-filter='ok']").click()
    assert ok_row.is_visible()
    assert un_row.is_hidden()

    # Filter → All: both visible again.
    page.locator(".bulk-filter-btn[data-filter='all']").click()
    assert ok_row.is_visible() and un_row.is_visible()