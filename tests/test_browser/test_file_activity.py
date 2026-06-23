# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test for M6 — file events show the filename as a download link in
the item's Activity tab."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def test_item_activity_shows_file_link(page, fresh_company, ui_server):
    api = fresh_company

    r = api.post("/items", json={"sku": "FILEACT1", "name": "File Act", "quantity": 1, "sell_by": "piece"})
    assert r.status_code in (200, 201), f"item create failed: {r.text}"
    item_id = r.json()["id"]

    r = api.post(
        f"/items/{item_id}/files",
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0image-bytes", "image/jpeg")},
    )
    assert r.status_code == 200, f"attach failed: {r.text}"

    page.goto(f"{ui_server}/inventory/{item_id}?tab=activity", wait_until="domcontentloaded")
    page.wait_for_selector("text=File attached", timeout=15000)

    link = page.locator(f"a[href*='/items/{item_id}/files/'][href$='/download']")
    assert link.count() >= 1, "expected a file download link in the activity row"
    assert "photo.jpg" in link.first.inner_text()