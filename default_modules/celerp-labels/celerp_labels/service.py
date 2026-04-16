# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Label printing service.

Generates PDF label sheets using reportlab. Falls back to a minimal stub
PDF when reportlab is not installed (e.g. in stripped test environments).

Public API
----------
render_label_pdf(items, template) -> bytes
    Render a PDF byte stream for one or more items using the given template.

render_label_text(items, template) -> str
    Plain-text representation (for testing / no-reportlab fallback).
"""
from __future__ import annotations

import io
from typing import Any

# reportlab is optional — if absent, render_label_pdf returns a minimal stub PDF.
try:
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
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
    from barcode.writer import ImageWriter

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
    """Resolve a field key against item state, checking attributes sub-dict as fallback."""
    top = item.get(key)
    if top is not None and top != "":
        return str(top)
    return str((item.get("attributes") or {}).get(key, "") or "")


def render_label_text(items: list[dict[str, Any]], template: dict[str, Any]) -> str:
    """Plain-text label render — used in tests and as no-PDF fallback."""
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
                val = _item_val(item, key)
                lines.append(f"  {label}: {val}")
            lines.append("")
    return "\n".join(lines)


def _make_barcode_image(value: str) -> io.BytesIO | None:
    """Render a Code128 barcode to PNG bytes (bars only, no text - text rendered separately)."""
    if not _BARCODE:
        return None
    try:
        buf = io.BytesIO()
        code128 = barcode.get("code128", str(value or "0"), writer=ImageWriter())
        code128.write(buf, options={
            "module_height": 8,
            "font_size": 0,      # No text in barcode image
            "text_distance": 0,
            "quiet_zone": 1,
            "write_text": False,  # Bars only
        })
        buf.seek(0)
        return buf
    except Exception:
        return None


def _make_qr_image(value: str) -> io.BytesIO | None:
    """Render a QR code to PNG bytes. Returns None on failure."""
    if not _QRCODE:
        return None
    try:
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1,
        )
        qr.add_data(str(value or ""))
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


def render_label_pdf(items: list[dict[str, Any]], template: dict[str, Any]) -> bytes:
    """Render a PDF byte stream for labels.

    Falls back to a minimal stub PDF if reportlab is not installed.
    Each field is placed using x/y coordinates if provided; otherwise
    fields are stacked vertically from the top of the label.
    """
    if not _REPORTLAB:
        return _stub_pdf(items, template)

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
                # Special keys: 'qr' and 'barcode' use the item's barcode/SKU value
                if key in ("qr", "barcode"):
                    val = str(item.get("barcode", "") or item.get("sku", "") or "")
                else:
                    val = _item_val(item, key)
                font_size = float(field.get("fontSize") or default_font_size)
                line_h = font_size * mm * 1.4

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
                    img_buf = _make_barcode_image(val)
                    if img_buf:
                        img_w = max(20, min(w_mm - 4, 30)) * mm
                        img_h = max(6, min(8, h_mm / 4)) * mm
                        c.drawImage(
                            ImageReader(img_buf), x_pt, y_pt,
                            width=img_w, height=img_h,
                            preserveAspectRatio=False,
                        )
                        # Human-readable text below barcode (industry standard)
                        bc_text_size = max(4, font_size * 0.6)
                        c.setFont("Helvetica", bc_text_size)
                        c.drawString(x_pt, y_pt - bc_text_size * 0.4, val[:30])
                    else:
                        c.setFont("Helvetica", font_size)
                        c.drawString(x_pt, y_pt + line_h * 0.2, f"[BC:{val[:20]}]")
                elif ftype == "qr":
                    img_buf = _make_qr_image(val)
                    if img_buf:
                        # QR code: always exactly 10mm (minimum scannable size)
                        side = 10 * mm
                        c.drawImage(
                            ImageReader(img_buf), x_pt, y_pt - side + line_h,
                            width=side, height=side,
                            preserveAspectRatio=True,
                        )
                    else:
                        c.setFont("Helvetica", font_size)
                        c.drawString(x_pt, y_pt + line_h * 0.2, f"[QR:{val[:20]}]")
                else:
                    bold = field.get("bold", False)
                    c.setFont("Helvetica-Bold" if bold else "Helvetica", font_size)
                    field_label = str(field.get("label", "") or "").strip()
                    display_text = f"{field_label}: {val}" if field_label else val
                    c.drawString(x_pt, y_pt + line_h * 0.2, display_text[:50])

            c.showPage()

    c.save()
    return buf.getvalue()


def _stub_pdf(items: list[dict[str, Any]], template: dict[str, Any]) -> bytes:
    """Minimal valid PDF — returned when reportlab is absent."""
    text = render_label_text(items, template)
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
