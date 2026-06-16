# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Manufacturing-tab product image gallery (above the Cost summary)."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/worksheet")

# A minimal 1x1 PNG — enough for the gallery to render a real <img>.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_gallery_shows_hero_above_cost_summary(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    ring = api.post("/items", json={"sku": "GAL-RING", "name": "Gallery Ring", "quantity": 0, "sell_by": "piece"}).json()["id"]
    # Upload a product image (the first image auto-becomes the hero).
    r = api.post(f"/items/{ring}/files?document_tag=product_images",
                 files={"file": ("ring.png", _PNG, "image/png")})
    assert r.status_code == 200, r.text

    page.goto(f"{ui_server}/inventory/{ring}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#wf-gallery .wf-hero-img", timeout=10000)
    # Gallery renders in the right column, above the Cost summary card.
    gallery_box = page.locator("#wf-gallery").bounding_box()
    cost_box = page.locator("#recipe-cost-card").bounding_box()
    assert gallery_box and cost_box
    assert gallery_box["y"] < cost_box["y"], "gallery must sit above the cost summary"
    page.screenshot(path=str(SHOTS / "gallery.png"), full_page=True)


def test_gallery_empty_state(page, ui_server, api):
    bare = api.post("/items", json={"sku": "GAL-EMPTY", "name": "No Images", "quantity": 0, "sell_by": "piece"}).json()["id"]
    page.goto(f"{ui_server}/inventory/{bare}?tab=manufacturing", wait_until="domcontentloaded")
    page.wait_for_selector("#wf-gallery.wf-gallery--empty", timeout=10000)
    assert "No product images yet" in page.locator("#wf-gallery").inner_text()
