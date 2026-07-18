# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Label printing service.

Generates PDF label sheets using reportlab. Falls back to a minimal stub
PDF when reportlab is not installed (e.g. in stripped test environments).

Public API
----------
render_label_pdf(items, template, unit_map) -> bytes
    Render a PDF byte stream for one or more items using the given template.

render_label_text(items, template, unit_map) -> str
    Plain-text representation (for testing / no-reportlab fallback).

resolve_field_value(item, key, unit_map) -> str
    Resolve a template field key against item state (shared with the HTML
    print path in ui_routes).
"""
from __future__ import annotations

import io
from typing import Any

from celerp.services.units import (
    DEFAULT_UNITS,
    build_unit_map,
    format_qty,
    is_pieces_unit,
    is_weight_unit,
)

# reportlab is optional — if absent, render_label_pdf returns a minimal stub PDF.
try:
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import createBarcodeDrawing
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas

    _REPORTLAB = True
except ImportError:
    _REPORTLAB = False

# qrcode is optional
try:
    import qrcode

    _QRCODE = True
except ImportError:
    _QRCODE = False

# barcode is optional
try:
    import barcode

    _BARCODE = True
except ImportError:
    _BARCODE = False


# ── Supported paper sizes ────────────────────────────────────────────────────

_SIZES: dict[str, tuple[float, float]] = {
    "A4": (210, 297),
    "A5": (148, 210),
    "letter": (216, 279),
    "24x24mm": (24, 24),
    "29x29mm": (29, 29),
    "34x34mm": (34, 34),
    "40x30mm": (40, 30),
    "62x29mm": (62, 29),
    "100x50mm": (100, 50),
}


def display_label(label: str) -> str:
    """The printed form of a field label: the leaf of a "Category > Field" path.

    The field picker lists category attributes as "Stones › Origin" so they can be
    told apart while choosing, and that whole path was then saved as the printed
    label. On a 25mm sticker the repeated category prefix costs roughly a third of
    the line and tells the reader nothing - they are holding the stone. Print the
    leaf; the picker keeps the full path. Applied at render time so labels saved
    before this still shorten, without anyone re-picking their fields.
    """
    if not label:
        return ""
    return str(label).split("›")[-1].strip()


def _parse_size(fmt: str) -> tuple[float, float]:
    """Return (width_mm, height_mm) for a format string. Public alias kept for compatibility."""
    if fmt in _SIZES:
        return _SIZES[fmt]
    if "x" in fmt:
        parts = fmt.lower().replace("mm", "").split("x")
        try:
            return float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            pass
    return _SIZES["40x30mm"]


def _resolve_size(template: dict[str, Any]) -> tuple[float, float]:
    """Return (width_mm, height_mm) respecting custom dimensions."""
    fmt = template.get("format", "40x30mm")
    if fmt == "custom":
        w = template.get("width_mm") or 50.0
        h = template.get("height_mm") or 30.0
        return float(w), float(h)
    return _parse_size(fmt)


def _item_val(item: dict[str, Any], key: str) -> str:
    """Raw field lookup against item state, checking attributes sub-dict as fallback."""
    top = item.get(key)
    if top is not None and top != "":
        return str(top)
    return str((item.get("attributes") or {}).get(key, "") or "")


def resolve_field_value(item: dict[str, Any], key: str, unit_map: dict[str, dict]) -> str:
    """Resolve a template field key against item state.

    For the measure the sell unit already IS, `quantity` is the single source of
    truth (the stored companion field is absent on freshly created items and can
    go stale after sales), so derive it, exactly like the inventory table, the
    split preview, and doc-line rendering do:

    - weight on a weight-sold item -> quantity + its unit (e.g. "38.60 carat")
    - pieces on a pieces-sold item -> quantity
    - unit -> the item's sell_by
    - qr / barcode / barcode_text -> the item's barcode, falling back to SKU
    """
    sell_by = item.get("sell_by")
    if key in ("qr", "barcode", "barcode_text"):
        return str(item.get("barcode", "") or item.get("sku", "") or "")
    if key == "weight" and is_weight_unit(sell_by, unit_map):
        qty = format_qty(item.get("quantity"), sell_by, unit_map)
        return f"{qty} {sell_by}" if qty else ""
    if key == "pieces" and is_pieces_unit(sell_by, unit_map):
        return format_qty(item.get("quantity"), sell_by, unit_map)
    if key == "unit":
        return _item_val(item, key) or str(sell_by or "")
    return _item_val(item, key)


def render_label_text(
    items: list[dict[str, Any]], template: dict[str, Any], unit_map: dict[str, dict] | None = None
) -> str:
    """Plain-text label render — used in tests and as no-PDF fallback."""
    unit_map = unit_map or build_unit_map(DEFAULT_UNITS)
    fields = template.get("fields") or [{"key": "name", "label": "Name", "type": "text"}]
    copies = int(template.get("copies", 1))
    lines = []
    for item in items:
        for _ in range(copies):
            lines.append(f"=== {template.get('name', 'Label')} ===")
            for field in fields:
                if isinstance(field, dict):
                    key = field.get("key", "")
                    label = str(field.get("label", "") or key).strip() or key
                else:
                    key = str(field)
                    label = key
                val = resolve_field_value(item, key, unit_map)
                lines.append(f"  {label}: {val}")
            lines.append("")
    return "\n".join(lines)


# Code128 X-dimension (width of one bar module). Widened from 0.2mm: a narrow
# module is both harder to scan once thermal ink spreads and rounds badly on the
# dot grid - 0.2mm is 2.36 dots at 300dpi, so snapping it lands on 2 dots
# (0.169mm), thinner than intended and below a comfortable scan width. 0.33mm
# snaps to a clean 4 dots at 300dpi and 3 dots at 203dpi, both well above the
# minimum, so the same value is correct on either printer.
_BC_MODULE_MM = 0.33
# Bar-width reduction: shave the black bar so the adjacent white gaps stay open
# against thermal ink bleed. Quantised to whole dots at render time (a dot printer
# cannot place a fraction of a dot), so this is a nominal figure.
_BC_BAR_SHRINK_MM = 0.05
_BC_QUIET_MM = 1.0   # quiet zone on either side of the bars

# Thermal label printers are dot-addressed: 300dpi = 0.0847mm per dot, 203dpi =
# 0.1251mm. A bar edge that falls between dots gets rounded by the rasterizer, so a
# nominally uniform X-dimension prints as a mix of (say) 3- and 4-dot bars - uneven
# bars are what makes a printed barcode scan badly, not vector rendering itself.
# 0.33mm is 3.90 dots at 300dpi, so we snap the geometry to whole dots: at 300dpi
# 4 dots = 0.3387mm (visually identical to 0.33), and every bar edge then lands
# exactly on a dot boundary. Quantising keeps one resolution-independent vector
# that is exact on 203dpi hardware too - a fixed 300dpi bitmap would be resampled
# there, which is worse than vector.
DEFAULT_PRINTER_DPI = 300
_BC_MIN_MODULE_DOTS = 2   # never let a module collapse below 2 dots (unscannable)

# Leading as a multiple of the font size. 1.25 is tight enough to fit a dense
# stone/lot label without lines touching; the old renderer effectively used ~3.97
# (see render_label_pdf), which is why so little fitted on a label.
_LINE_SPACING = 1.25


def dot_mm(dpi: int = DEFAULT_PRINTER_DPI) -> float:
    """Width of one printer dot in mm."""
    return 25.4 / float(max(1, int(dpi or DEFAULT_PRINTER_DPI)))


def snap_to_dots(value_mm: float, dpi: int = DEFAULT_PRINTER_DPI, minimum_dots: int = 0) -> float:
    """Round a millimetre length to a whole number of printer dots."""
    d = dot_mm(dpi)
    dots = max(int(minimum_dots), int(round(float(value_mm) / d)))
    return dots * d
QR_SIZE_MM = 10.0    # QR codes are a fixed 10mm square on labels (minimum scannable size)


def _svg_markup(inner: str, vb_w: float, vb_h: float, w_mm: float, h_mm: float, stretch: bool) -> str:
    """Wrap barcode/QR rects in an SVG root element.

    stretch=True fills the parent box (print sheet inlines the SVG in a sized
    div); otherwise the SVG carries its intrinsic mm size (designer preview
    loads it via <img>). preserveAspectRatio="none" lets both stretch freely,
    and shape-rendering="crispEdges" suppresses anti-aliased module edges.
    """
    size = 'width="100%" height="100%"' if stretch else f'width="{w_mm:.2f}mm" height="{h_mm:.2f}mm"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w:g} {vb_h:g}" '
        f'{size} preserveAspectRatio="none" shape-rendering="crispEdges">{inner}</svg>'
    )


def _make_barcode_svg(value: str, module_height: int = 8, stretch: bool = False,
                      dpi: int = DEFAULT_PRINTER_DPI) -> tuple[str, float, float] | None:
    """Render a Code128 barcode as vector SVG (bars only, no text).

    Vector output rasterizes sharply at the printer's own dot grid; a raster
    resampled by the print HTML thresholds into merged or dropped bars on
    low-DPI 1-bit label printers.

    Every horizontal length is snapped to a whole number of printer dots for `dpi`
    (see snap_to_dots), so each bar edge lands exactly on a dot boundary instead of
    being rounded unevenly by the rasterizer - that rounding, not vector rendering,
    is what makes printed bars come out uneven.

    module_height: bar height in mm (1-30). Default 8.
    Returns (svg_markup, natural_width_mm, height_mm), or None if the barcode
    library is missing or the value cannot be encoded.
    """
    if not _BARCODE:
        return None
    try:
        pattern = barcode.get("code128", str(value or "0")).build()[0]
    except Exception:
        return None
    h_mm = float(max(1, min(30, int(module_height))))
    # Snap the X-dimension, the bar shave and the quiet zone to whole dots.
    mod_mm = snap_to_dots(_BC_MODULE_MM, dpi, minimum_dots=_BC_MIN_MODULE_DOTS)
    shrink_mm = snap_to_dots(_BC_BAR_SHRINK_MM, dpi)          # 0 or a whole dot
    if shrink_mm >= mod_mm:                                    # never erase a module
        shrink_mm = 0.0
    quiet_mm = snap_to_dots(_BC_QUIET_MM, dpi)
    w_mm = len(pattern) * mod_mm + 2 * quiet_mm
    bars = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "1":
            j = i
            while j < len(pattern) and pattern[j] == "1":
                j += 1
            x = quiet_mm + i * mod_mm
            w = (j - i) * mod_mm
            # Bar-width reduction, dot-wise: a dot-addressed printer can only drop
            # WHOLE dots, so shave the trailing edge by one dot rather than half a
            # dot off each side. A symmetric shave would put the bar's left edge on
            # a half-dot boundary and hand the rasterizer the same rounding problem
            # this quantisation exists to remove. The following white gap widens by
            # exactly one dot, which is what compensates for thermal ink bleed.
            bw = max(dot_mm(dpi), w - shrink_mm)
            bars.append(f'<rect x="{x:.4f}" y="0" width="{bw:.4f}" height="{h_mm:g}"/>')
            i = j
        else:
            i += 1
    inner = f'<rect width="100%" height="100%" fill="#fff"/><g fill="#000">{"".join(bars)}</g>'
    return _svg_markup(inner, w_mm, h_mm, w_mm, h_mm, stretch), w_mm, h_mm


def _make_qr_svg(value: str, stretch: bool = False) -> str | None:
    """Render a QR code as vector SVG (fixed 10mm square). Returns None on failure.

    Same rationale as _make_barcode_svg: vector modules stay crisp at any
    printer resolution.
    """
    if not _QRCODE:
        return None
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=1)
        qr.add_data(str(value or ""))
        qr.make(fit=True)
        matrix = qr.get_matrix()
    except Exception:
        return None
    n = len(matrix)
    cells = []
    for r, row in enumerate(matrix):
        c0 = 0
        while c0 < n:
            if row[c0]:
                c1 = c0
                while c1 < n and row[c1]:
                    c1 += 1
                cells.append(f'<rect x="{c0}" y="{r}" width="{c1 - c0}" height="1"/>')
                c0 = c1
            else:
                c0 += 1
    inner = f'<rect width="100%" height="100%" fill="#fff"/><g fill="#000">{"".join(cells)}</g>'
    return _svg_markup(inner, n, n, QR_SIZE_MM, QR_SIZE_MM, stretch)


def render_label_pdf(
    items: list[dict[str, Any]], template: dict[str, Any], unit_map: dict[str, dict] | None = None
) -> bytes:
    """Render a PDF byte stream for labels.

    Falls back to a minimal stub PDF if reportlab is not installed.
    Each field is placed using x/y coordinates if provided; otherwise
    fields are stacked vertically from the top of the label.
    """
    unit_map = unit_map or build_unit_map(DEFAULT_UNITS)
    if not _REPORTLAB:
        return _stub_pdf(items, template, unit_map)

    fields = template.get("fields") or [{"key": "name", "label": "Name", "type": "text"}]
    copies = int(template.get("copies", 1))
    w_mm, h_mm = _resolve_size(template)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(w_mm * mm, h_mm * mm))

    default_font_size = min(8.0, max(5.0, (h_mm - 4) / max(len(fields), 1) * 0.7))
    default_line_h = default_font_size * mm * 1.4

    for item in items:
        for _ in range(copies):
            # Track auto-stack Y position (top-down)
            auto_y = h_mm * mm - 3 * mm

            for field in fields:
                if isinstance(field, str):
                    field = {"key": field, "label": field, "type": "text"}

                key = field.get("key", "")
                ftype = field.get("type", "text")
                val = resolve_field_value(item, key, unit_map)
                font_size = float(field.get("fontSize") or default_font_size)
                line_h = font_size * _LINE_SPACING

                # Resolve position: explicit x/y override auto-stack
                if field.get("x") is not None and field.get("y") is not None:
                    x_pt = float(field["x"]) * mm
                    # y in template is from top; reportlab uses bottom-origin
                    y_pt = h_mm * mm - float(field["y"]) * mm - line_h
                else:
                    x_pt = 2 * mm
                    y_pt = auto_y - line_h
                    auto_y -= line_h + 1 * mm

                if ftype == "barcode":
                    try:
                        # Vector barcode at a wide X-dimension so bars land cleanly on the
                        # printer's dot grid and the white gaps stay open. Natural width;
                        # squeeze only when the label is genuinely too narrow (never stretch up).
                        # Bar height honors the field's barcode_height setting (mm, 1-30), like the designer.
                        img_h = max(1, min(30, int(field.get("barcode_height") or 8))) * mm
                        # Snap the X-dimension to whole printer dots for the same reason the
                        # SVG path does: 0.33mm is 3.90 dots at 300dpi, so an unsnapped module
                        # rasterizes as a mix of 3- and 4-dot bars.
                        _bar_w = snap_to_dots(_BC_MODULE_MM, DEFAULT_PRINTER_DPI,
                                              minimum_dots=_BC_MIN_MODULE_DOTS)
                        bc = createBarcodeDrawing("Code128", value=str(val), barHeight=img_h,
                                                  humanReadable=False, barWidth=_bar_w * mm)
                        avail = (w_mm - 2) * mm - x_pt
                        img_w = min(avail, max(20 * mm, bc.width))
                        if img_w != bc.width:
                            bc = createBarcodeDrawing(
                                "Code128", value=str(val), barHeight=img_h,
                                humanReadable=False, width=img_w, height=img_h,
                            )
                        renderPDF.draw(bc, c, x_pt, y_pt)
                        # Human-readable text below barcode (industry standard)
                        bc_text_size = max(4, font_size * 0.6)
                        c.setFont("Helvetica", bc_text_size)
                        c.drawString(x_pt, y_pt - bc_text_size * 0.4, val[:30])
                    except Exception:
                        c.setFont("Helvetica", font_size)
                        c.drawString(x_pt, y_pt + line_h * 0.2, f"[BC:{val[:20]}]")
                elif ftype == "qr":
                    try:
                        side = QR_SIZE_MM * mm
                        qr_drawing = createBarcodeDrawing("QR", value=str(val), width=side, height=side)
                        renderPDF.draw(qr_drawing, c, x_pt, y_pt - side + line_h)
                    except Exception:
                        c.setFont("Helvetica", font_size)
                        c.drawString(x_pt, y_pt + line_h * 0.2, f"[QR:{val[:20]}]")
                else:
                    bold = field.get("bold", False)
                    c.setFont("Helvetica-Bold" if bold else "Helvetica", font_size)
                    field_label = display_label(field.get("label", ""))
                    display_text = f"{field_label}: {val}" if field_label else val
                    c.drawString(x_pt, y_pt + line_h * 0.2, display_text[:50])

            c.showPage()

    c.save()
    return buf.getvalue()


def _stub_pdf(
    items: list[dict[str, Any]], template: dict[str, Any], unit_map: dict[str, dict] | None = None
) -> bytes:
    """Minimal valid PDF — returned when reportlab is absent."""
    text = render_label_text(items, template, unit_map)
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = (
        "%PDF-1.4\n"
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>\nendobj\n"
        f"4 0 obj\n<< /Length {len(escaped) + 20} >>\nstream\nBT /F1 10 Tf 10 180 Td ({escaped}) Tj ET\nendstream\nendobj\n"
        "xref\n0 5\n0000000000 65535 f\n0000000009 00000 n\n0000000068 00000 n\n"
        "0000000125 00000 n\n0000000212 00000 n\n"
        "trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n312\n%%EOF"
    )
    return content.encode()
