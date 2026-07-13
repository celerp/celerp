# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression test for issue #217 — printed labels must honor the designer's field positions.

The label designer saves each field with an (x, y) mm coordinate and the on-screen preview
renders from those coordinates. The print page (`_printable_label_sheet` in
`default_modules/celerp-labels/celerp_labels/ui_routes.py`) must place each field at the same
coordinate. Before the fix it lays the fields out in normal top-to-bottom document flow and never
reads x/y — so a field designed far to the RIGHT prints at the same left edge as a field designed
on the LEFT.

We build a template with two text fields deliberately placed in opposite corners (one at the far
left, one at the far right), print it, and measure the rendered field boxes. If positions are
honored the RIGHT field is rendered clearly to the right of the LEFT one; on the buggy stacked
renderer both sit at the same left edge, so the horizontal-offset assertion fails.
"""
import uuid

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def positioned_template(api):
    """40x30mm template with two text fields at opposite corners.

    LEFT  field -> top-left     (x = 1 mm)
    RIGHT field -> bottom-right  (x = 28 mm)  — a large, unambiguous horizontal offset.
    Distinct labels ("LBLLEFT" / "LBLRIGHT") make each field locatable by its printed text.
    """
    r = api.post("/api/labels/templates", json={
        "name": f"pos-{uuid.uuid4().hex[:6]}",
        "format": "40x30mm",
        "fields": [
            {"key": "sku",  "label": "LBLLEFT",  "type": "text", "x": 1.0,  "y": 1.0},
            {"key": "name", "label": "LBLRIGHT", "type": "text", "x": 28.0, "y": 20.0},
        ],
    })
    assert r.status_code in {200, 201}, f"template create failed: {r.text}"
    return r.json()["id"]


@pytest.fixture(scope="module")
def labelled_item(api):
    """An inventory item to render on the label (its sku/name fill the two text fields)."""
    r = api.post("/items", json={
        "sku": f"LBL-{uuid.uuid4().hex[:6]}",
        "name": "PositionProbe",
        "quantity": 1,
        "sell_by": "piece",
    })
    assert r.status_code in {200, 201}, f"item create failed: {r.text}"
    return r.json()["id"]


def test_print_honors_designed_field_positions(page, ui_server, positioned_template, labelled_item):
    """#217: a field designed at x=28mm must render to the RIGHT of a field designed at x=1mm.

    Pre-fix the print renderer stacks both fields left-aligned at the same x (dx ~ 0), so the
    horizontal-offset assertion below fails — pinning the regression. Post-fix each field is placed
    at its stored coordinate, so the RIGHT field is clearly to the right of the LEFT one.
    """
    page.goto(
        f"{ui_server}/labels/print/{labelled_item}?template_id={positioned_template}",
        wait_until="domcontentloaded",
    )
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"print page errored: {body!r}"

    left = page.locator("div.label-field", has_text="LBLLEFT").first
    right = page.locator("div.label-field", has_text="LBLRIGHT").first
    assert left.count() > 0 and right.count() > 0, f"label fields not rendered; body={body!r}"

    left_box = left.bounding_box()
    right_box = right.bounding_box()
    assert left_box and right_box, "could not measure field bounding boxes"

    # 27 mm of designed horizontal separation (~100 px at ~3.78 px/mm). Require a clear rightward
    # offset; the buggy stacked layout leaves both at the same left edge (dx ~ 0).
    dx = right_box["x"] - left_box["x"]
    assert dx > 30, (
        f"RIGHT field not placed to the right of LEFT field (dx={dx:.1f}px): "
        "the print renderer is ignoring the designer's x positions (issue #217)"
    )
