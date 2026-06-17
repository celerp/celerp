# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Production documents & images upload area on the Manufacturing tab."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/worksheet")

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_manufacturing_tab_has_files_section_and_uploads(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    item = api.post("/items", json={"sku": "DOC-RING", "name": "Doc Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]

    page.goto(f"{ui_server}/inventory/{item}?tab=manufacturing", wait_until="domcontentloaded")
    # The production documents section renders on the Manufacturing tab.
    page.wait_for_selector("h3:has-text('Production documents & images')", timeout=10000)
    sid = item.replace(":", "-")
    page.wait_for_selector(f"#file-drop-input-{sid}", state="attached", timeout=8000)

    # Drive the real dropzone: set the hidden file input (its change handler uploads).
    page.set_input_files(f"#file-drop-input-{sid}", files=[{"name": "spec.png", "mimeType": "image/png", "buffer": _PNG}])

    # The file lands in the section list (and, being an image, in the gallery on reload).
    page.wait_for_selector("a.file-link:has-text('spec.png')", timeout=10000)
    assert len(api.get(f"/items/{item}").json().get("files") or []) == 1
    page.screenshot(path=str(SHOTS / "files.png"), full_page=True)

    # Remove it via the row delete button (accept the confirm dialog).
    page.on("dialog", lambda d: d.accept())
    page.locator(f"#files-section-{sid} button[title='Delete File']").first.click()
    page.wait_for_selector("a.file-link:has-text('spec.png')", state="detached", timeout=10000)
    assert (api.get(f"/items/{item}").json().get("files") or []) == []
