# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Feature test (issue #221) — the label designer must let the user set a per-field font size,
and that size must be applied when the label is printed.

Two checks:
  1. Designer: a text field's row shows an editable font-size box, pre-filled from the saved value.
  2. Print: a field with a saved fontSize renders at that size (a large size is clearly bigger
     than the default), so what the user picks in the designer is what comes out on the label.
"""
import uuid

import pytest

pytestmark = pytest.mark.browser


def _make_template(api, font_size, ftype="text", key="name", label="Name"):
    r = api.post("/api/labels/templates", json={
        "name": f"fs-{uuid.uuid4().hex[:6]}",
        "format": "40x30mm",
        "fields": [{"key": key, "label": label, "type": ftype, "x": 2.0, "y": 2.0, "fontSize": font_size}],
    })
    assert r.status_code in {200, 201}, f"template create failed: {r.text}"
    return r.json()["id"]


def test_designer_shows_editable_font_size(page, ui_server, api):
    """The designer renders a visible, editable font-size box for a text field, pre-filled with
    the template's saved value."""
    tid = _make_template(api, 22)
    page.goto(f"{ui_server}/settings/labels/{tid}", wait_until="domcontentloaded")

    fs_box = page.locator(".field-row-compact .fld-fs").first
    assert fs_box.count() > 0, "no font-size box rendered in the field row"
    assert fs_box.is_visible(), "font-size box should be visible for a text field"
    assert fs_box.input_value() == "22", f"font-size box should show saved value; got {fs_box.input_value()!r}"


def test_print_applies_designed_font_size(page, ui_server, api):
    """A field saved with a large font size prints noticeably larger than the default."""
    tid = _make_template(api, 24)  # 24pt ~= 32px at 96dpi
    r = api.post("/items", json={
        "sku": f"FS-{uuid.uuid4().hex[:6]}",
        "name": "FontProbe",
        "quantity": 1,
        "sell_by": "piece",
    })
    assert r.status_code in {200, 201}, f"item create failed: {r.text}"
    eid = r.json()["id"]

    page.goto(f"{ui_server}/labels/print/{eid}?template_id={tid}", wait_until="domcontentloaded")
    body = page.locator("body").inner_text()
    assert "Internal Server Error" not in body, f"print page errored: {body!r}"

    fld = page.locator("div.label-field", has_text="Name:").first
    assert fld.count() > 0, f"text field not rendered; body={body!r}"
    fs_px = fld.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    # 24pt is ~32px; the un-styled default is ~11px. Require clearly larger than default.
    assert fs_px > 24, f"printed font size not applied (got {fs_px:.1f}px, expected ~32px)"


def test_designer_shows_font_size_for_barcode_number(page, ui_server, api):
    """The barcode-number field (barcode_text) also gets an editable font-size box."""
    tid = _make_template(api, 18, ftype="barcode_text", key="barcode_text", label="No")
    page.goto(f"{ui_server}/settings/labels/{tid}", wait_until="domcontentloaded")

    fs_box = page.locator(".field-row-compact .fld-fs").first
    assert fs_box.count() > 0, "no font-size box rendered for the barcode-number field"
    assert fs_box.is_visible(), "font-size box should be visible for a barcode-number field"
    assert fs_box.input_value() == "18", f"font-size box should show saved value; got {fs_box.input_value()!r}"


def test_print_applies_font_size_to_barcode_number(page, ui_server, api):
    """A barcode-number field saved with a large font size prints noticeably larger than default."""
    tid = _make_template(api, 24, ftype="barcode_text", key="barcode_text", label="No")
    r = api.post("/items", json={
        "sku": f"BN-{uuid.uuid4().hex[:6]}",
        "name": "BarNumProbe",
        "barcode": "9876543210",
        "quantity": 1,
        "sell_by": "piece",
    })
    assert r.status_code in {200, 201}, f"item create failed: {r.text}"
    eid = r.json()["id"]

    page.goto(f"{ui_server}/labels/print/{eid}?template_id={tid}", wait_until="domcontentloaded")
    span = page.locator("span.bc-human").first
    assert span.count() > 0, "barcode-number text not rendered"
    fs_px = span.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    # 24pt is ~32px; the un-styled .bc-human default is 9px. Require clearly larger than default.
    assert fs_px > 20, f"barcode-number font size not applied (got {fs_px:.1f}px, expected ~32px)"