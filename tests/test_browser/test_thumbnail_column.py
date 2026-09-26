# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: dropping an image on the inventory list's thumbnail cell
uploads it and the cell re-renders with the new thumbnail."""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

pytestmark = pytest.mark.browser


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (320, 200), (30, 120, 220)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_thumbnail_column_drop_upload(page, fresh_company, ui_server):
    api = fresh_company
    r = api.post("/items", json={"sku": "THUMBDROP1", "name": "Drop target", "quantity": 1, "sell_by": "piece"})
    assert r.status_code in (200, 201), r.text
    item_id = r.json()["id"]
    safe = item_id.replace(":", "-")

    page.goto(f"{ui_server}/inventory?cols=thumbnail,sku,name", wait_until="domcontentloaded")
    cell = page.locator(f"#img-cell-{safe}")
    cell.wait_for(timeout=15000)
    assert cell.locator(".cell-image-empty").count() == 1
    assert cell.locator("img.cell-thumbnail").count() == 0

    page.evaluate(
        """([sel, b64]) => {
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const file = new File([bytes], 'drop.png', {type: 'image/png'});
            const dt = new DataTransfer();
            dt.items.add(file);
            const cell = document.querySelector(sel);
            cell.dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true}));
            cell.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true}));
        }""",
        [f"#img-cell-{safe}", _png_b64()],
    )

    img = page.locator(f"#img-cell-{safe} img.cell-thumbnail")
    img.wait_for(timeout=15000)
    src = img.get_attribute("src")
    assert src.startswith(f"/items/{item_id}/files/") and src.endswith("/thumbnail")
    assert page.locator(f"#img-cell-{safe} .cell-image-empty").count() == 0

    files = api.get(f"/items/{item_id}").json()["files"]
    assert len(files) == 1 and files[0]["is_hero"] is True
    thumb = api.get(f"/items/{item_id}/files/{files[0]['id']}/thumbnail")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(thumb.content)) as im:
        assert max(im.size) == 160
