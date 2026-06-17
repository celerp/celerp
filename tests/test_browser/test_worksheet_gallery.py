# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Manufacturing-tab product image gallery: hero above the files area; thumbnail sets hero."""
from __future__ import annotations

import base64
import time as _t
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/worksheet")

# A minimal 1x1 PNG — enough for the gallery to render a real <img>.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _fid(resp):
    j = resp.json()
    return j.get("file_id") or j.get("id")


def _hero_id(api, item):
    fs = api.get(f"/items/{item}").json().get("files") or []
    return next((f["id"] for f in fs if f.get("is_hero")), None)


def test_gallery_above_files_and_thumbnail_sets_hero(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    ring = api.post("/items", json={"sku": "GAL-RING", "name": "Gallery Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    # Two product images — the first auto-becomes hero.
    id1 = _fid(api.post(f"/items/{ring}/files?document_tag=product_images", files={"file": ("a.png", _PNG, "image/png")}))
    id2 = _fid(api.post(f"/items/{ring}/files?document_tag=product_images", files={"file": ("b.png", _PNG, "image/png")}))
    assert _hero_id(api, ring) == id1

    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#wf-gallery .wf-hero-img", timeout=10000)

    # The gallery sits above the Production documents (files) area in the right column.
    sid = ring.replace(":", "-")
    gallery_box = page.locator("#wf-gallery").bounding_box()
    files_box = page.locator(f"#files-section-{sid}").bounding_box()
    assert gallery_box and files_box and gallery_box["y"] < files_box["y"]
    page.screenshot(path=str(SHOTS / "gallery.png"), full_page=True)

    # Clicking the non-hero thumbnail makes it the hero (it does not open a new tab).
    page.locator("#wf-gallery .wf-thumb:not(.wf-thumb--active)").first.click()
    deadline = _t.time() + 8
    while _t.time() < deadline and _hero_id(api, ring) != id2:
        _t.sleep(0.25)
    assert _hero_id(api, ring) == id2, "clicking a thumbnail should set it as the hero image"


def test_gallery_empty_state(page, ui_server, api):
    bare = api.post("/items", json={"sku": "GAL-EMPTY", "name": "No Images", "quantity": 0, "sell_by": "piece"}).json()["id"]
    page.goto(f"{ui_server}/inventory/{bare}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#wf-gallery.wf-gallery--empty", timeout=10000)
    assert "No product images yet" in page.locator("#wf-gallery").inner_text()
