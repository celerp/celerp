# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
PDF generation for Celerp documents.

Produces a clean A4 PDF with discrete "Powered by Celerp" footer branding.
Attribution is non-removable per LICENSE.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Register DejaVu Sans (Unicode-capable - supports currency symbols like ฿, ₹, €, etc.)
_FONT = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
pdfmetrics.registerFont(TTFont(_FONT, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont(_FONT_BOLD, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))

_BRAND_URL = "https://www.celerp.com"
_BRAND_TEXT = "Powered by Celerp \u00b7 Opensource Business Software for AI Transformations"

_GREY = colors.HexColor("#6b7280")
_DARK = colors.HexColor("#111827")
_LIGHT_GREY = colors.HexColor("#f3f4f6")
_BORDER = colors.HexColor("#e5e7eb")

# Display labels per doc_type - matches sidebar/UI terminology
_DOC_TYPE_LABELS: dict[str, str] = {
    "invoice": "Invoice",
    "purchase_order": "Purchase Order",
    "bill": "Vendor Bill",
    "credit_note": "Credit Note",
    "memo": "Consignment Out",
    "consignment_in": "Consignment In",
    "receipt": "Receipt",
    "proforma": "Pro-Forma Invoice",
}

# Context-appropriate "Bill To" / "Vendor" / "Consignee" label
_CONTACT_LABELS: dict[str, str] = {
    "invoice": "Bill To",
    "purchase_order": "Vendor",
    "bill": "Vendor",
    "credit_note": "Credit To",
    "memo": "Consignee",
    "consignment_in": "Consignor",
    "receipt": "Received From",
}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Normal"], fontName=_FONT_BOLD, fontSize=18, textColor=_DARK, spaceAfter=2 * mm, leading=22),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontName=_FONT, fontSize=11, textColor=_GREY, spaceAfter=1 * mm),
        "label": ParagraphStyle("label", parent=base["Normal"], fontName=_FONT, fontSize=8, textColor=_GREY, spaceAfter=1 * mm),
        "value": ParagraphStyle("value", parent=base["Normal"], fontName=_FONT, fontSize=10, textColor=_DARK, spaceAfter=1 * mm),
        "th": ParagraphStyle("th", parent=base["Normal"], fontName=_FONT, fontSize=8, textColor=_GREY, leading=12, alignment=1),
        "td": ParagraphStyle("td", parent=base["Normal"], fontName=_FONT, fontSize=8.5, textColor=_DARK, leading=12),
        "td_num": ParagraphStyle("td_num", parent=base["Normal"], fontName=_FONT, fontSize=8.5, textColor=_DARK, alignment=2, leading=12),
        "total_label": ParagraphStyle("total_label", parent=base["Normal"], fontName=_FONT, fontSize=10, textColor=_GREY, alignment=2),
        "total_value": ParagraphStyle("total_value", parent=base["Normal"], fontName=_FONT_BOLD, fontSize=11, textColor=_DARK, alignment=2, spaceAfter=1 * mm),
        "brand": ParagraphStyle("brand", parent=base["Normal"], fontName=_FONT, fontSize=7, textColor=_GREY, alignment=1),
    }


_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥",
    "THB": "฿", "KRW": "₩", "INR": "₹", "RUB": "₽", "TRY": "₺",
    "BRL": "R$", "ZAR": "R", "CHF": "CHF", "AUD": "A$", "CAD": "C$",
    "SGD": "S$", "HKD": "HK$", "NZD": "NZ$", "SEK": "kr", "NOK": "kr",
    "DKK": "kr", "PLN": "zł", "CZK": "Kč", "HUF": "Ft", "MXN": "$",
    "AED": "د.إ", "SAR": "﷼", "MYR": "RM", "PHP": "₱", "IDR": "Rp",
    "VND": "₫", "TWD": "NT$", "ILS": "₪", "EGP": "E£",
}


def _fmt_money(value: Any, currency: str = "USD") -> str:
    try:
        symbol = _CURRENCY_SYMBOLS.get(currency, currency)
        return f"{symbol}\u00a0{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_qty(value: Any) -> str:
    """Format quantity: preserve meaningful decimals (3.52 ct), drop trailing zeros."""
    try:
        f = float(value)
        if f == int(f):
            return str(int(f))
        # Strip trailing zeros but keep meaningful decimals
        return f"{f:.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def _fmt_date(value: Any) -> str:
    if not value:
        return "-"
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return s


def generate_document_pdf(doc: dict[str, Any], company: dict[str, Any] | None = None) -> bytes:
    """
    Generate a PDF for a Celerp document (invoice, PO, quotation, etc.).
    Returns raw PDF bytes.
    """
    buf = io.BytesIO()
    page_w, page_h = A4
    margin = 18 * mm

    pdf = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=20 * mm,
        title=_doc_title(doc),
    )

    s = _styles()
    story = []
    currency = doc.get("currency") or (company or {}).get("currency", "USD")
    raw_doc_type = doc.get("doc_type") or "document"

    # --- Header: company name + doc type/ref ---
    company_name = (company or {}).get("name", "") or "Your Company"
    doc_type_label = _DOC_TYPE_LABELS.get(raw_doc_type, raw_doc_type.replace("_", " ").title())
    doc_ref = doc.get("ref_id") or doc.get("doc_number") or doc.get("ref") or doc.get("entity_id", "")

    # Build company detail lines (address, tax ID, phone, email)
    _co = company or {}
    co_detail_parts: list[str] = []
    co_address = _co.get("address", "")
    if co_address:
        co_detail_parts.append(str(co_address))
    co_tax_id = _co.get("tax_id", "")
    if co_tax_id:
        co_detail_parts.append(f"Tax ID: {co_tax_id}")
    co_phone = _co.get("phone", "")
    if co_phone:
        co_detail_parts.append(f"Tel: {co_phone}")
    co_email = _co.get("email", "")
    if co_email:
        co_detail_parts.append(co_email)
    co_detail_text = "<br/>".join(co_detail_parts)

    header_data = [
        [Paragraph(company_name, s["title"]), Paragraph(doc_type_label, s["title"])],
        [Paragraph(co_detail_text, s["label"]),
         Paragraph(doc_ref, s["subtitle"])],
    ]
    header_table = Table(header_data, colWidths=[page_w * 0.55 - margin, page_w * 0.45 - margin])
    header_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(header_table)
    story.append(HRFlowable(width="100%", thickness=1, color=_BORDER, spaceAfter=4 * mm))

    # --- Meta: contact + dates ---
    contact_label = _CONTACT_LABELS.get(raw_doc_type, "Bill To")
    contact = doc.get("contact_name") or doc.get("contact_id") or "-"
    issue_date = _fmt_date(doc.get("issue_date") or doc.get("created_at"))
    due_date = _fmt_date(doc.get("due_date") or doc.get("payment_due_date"))
    status = (doc.get("status") or "").replace("_", " ").title()

    meta_data = [
        [Paragraph(contact_label, s["label"]), Paragraph("Date", s["label"]),
         Paragraph("Due Date", s["label"]), Paragraph("Status", s["label"])],
        [Paragraph(contact, s["value"]), Paragraph(issue_date, s["value"]),
         Paragraph(due_date, s["value"]), Paragraph(status, s["value"])],
    ]
    col_w = (page_w - 2 * margin) / 4
    meta_table = Table(meta_data, colWidths=[col_w] * 4)
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
    ]))
    story.append(meta_table)

    # --- Customer details (billing/shipping address, phone, email, tax ID) ---
    billing_addr = doc.get("contact_billing_address") or doc.get("contact_address") or ""
    shipping_addr = doc.get("contact_shipping_address") or ""
    contact_phone = doc.get("contact_phone") or ""
    contact_email = doc.get("contact_email") or ""
    contact_tax_id = doc.get("contact_tax_id") or ""
    contact_company = doc.get("contact_company_name") or ""

    has_customer_details = any([billing_addr, shipping_addr, contact_phone, contact_email, contact_tax_id, contact_company])
    if has_customer_details:
        # Build left column: billing details
        left_parts: list[str] = []
        if contact_company:
            left_parts.append(f"<b>{contact_company}</b>")
        if billing_addr:
            left_parts.append(str(billing_addr))
        if contact_tax_id:
            left_parts.append(f"Tax ID: {contact_tax_id}")
        if contact_phone:
            left_parts.append(f"Tel: {contact_phone}")
        if contact_email:
            left_parts.append(contact_email)
        left_text = "<br/>".join(left_parts)

        # Build right column: shipping address (only if different from billing)
        right_parts: list[str] = []
        if shipping_addr and shipping_addr != billing_addr:
            right_parts.append(str(shipping_addr))
        right_text = "<br/>".join(right_parts) if right_parts else ""

        half_w = (page_w - 2 * margin) / 2
        cust_header = [[
            Paragraph(f"{contact_label} Details", s["label"]),
            Paragraph("Ship To", s["label"]) if right_text else Paragraph("", s["label"]),
        ]]
        cust_body = [[
            Paragraph(left_text, s["value"]),
            Paragraph(right_text, s["value"]) if right_text else Paragraph("", s["value"]),
        ]]
        cust_data = cust_header + cust_body
        cust_table = Table(cust_data, colWidths=[half_w, half_w])
        cust_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
        ]))
        story.append(cust_table)

    story.append(Spacer(1, 5 * mm))

    # --- Line items ---
    usable = page_w - 2 * margin
    line_items = doc.get("line_items") or []

    # Show discount column only if any line has a non-zero discount
    has_discount = any(float(li.get("discount_pct") or 0) != 0 for li in line_items)

    if has_discount:
        col_widths = [
            usable * 0.20,  # Description
            usable * 0.13,  # SKU
            usable * 0.06,  # Qty
            usable * 0.05,  # Unit
            usable * 0.14,  # Unit Price
            usable * 0.10,  # Discount %
            usable * 0.10,  # Tax %
            usable * 0.22,  # Total
        ]
        rows = [[
            Paragraph("Description", s["th"]),
            Paragraph("SKU", s["th"]),
            Paragraph("Qty", s["th"]),
            Paragraph("Unit", s["th"]),
            Paragraph("Unit Price", s["th"]),
            Paragraph("Discount", s["th"]),
            Paragraph("Tax %", s["th"]),
            Paragraph("Total", s["th"]),
        ]]
    else:
        col_widths = [
            usable * 0.22,  # Description
            usable * 0.15,  # SKU
            usable * 0.06,  # Qty
            usable * 0.05,  # Unit
            usable * 0.16,  # Unit Price
            usable * 0.12,  # Tax %
            usable * 0.24,  # Total
        ]
        rows = [[
            Paragraph("Description", s["th"]),
            Paragraph("SKU", s["th"]),
            Paragraph("Qty", s["th"]),
            Paragraph("Unit", s["th"]),
            Paragraph("Unit Price", s["th"]),
            Paragraph("Tax %", s["th"]),
            Paragraph("Total", s["th"]),
        ]]

    for li in line_items:
        qty = float(li.get("quantity") or 0)
        price = float(li.get("unit_price") or 0)
        discount_pct = float(li.get("discount_pct") or 0)
        discounted = qty * price * (1 - discount_pct / 100) if discount_pct else qty * price
        line_total = float(li.get("line_total") or 0) or discounted
        unit = str(li.get("unit") or li.get("unit_name") or li.get("uom") or "-")
        # Render multi-tax codes if present, else fall back to legacy tax_rate
        li_taxes = li.get("taxes") or []
        if li_taxes:
            tax_str = " + ".join(
                f"{t.get('label') or t.get('code') or 'Tax'} {float(t.get('rate', 0)):.1f}%"
                for item in li_taxes
            )
        else:
            tax_str = f"{float(li.get('tax_rate') or 0):.1f}%"
        rows.append([
            Paragraph(str(li.get("description") or li.get("name") or "-"), s["td"]),
            Paragraph(str(li.get("sku") or "-"), s["td"]),
            Paragraph(_fmt_qty(qty), s["td_num"]),
            Paragraph(unit, s["td"]),
            Paragraph(_fmt_money(price, currency), s["td_num"]),
            *([Paragraph(f"{discount_pct:.1f}%", s["td_num"])] if has_discount else []),
            Paragraph(tax_str, s["td_num"]),
            Paragraph(_fmt_money(line_total, currency), s["td_num"]),
        ])

    _empty_cols = 8 if has_discount else 7
    if not line_items:
        rows.append([Paragraph("No line items.", s["td"])] + [""] * (_empty_cols - 1))

    lines_table = Table(rows, colWidths=col_widths, repeatRows=1)
    lines_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _LIGHT_GREY),
        ("TEXTCOLOR", (0, 0), (-1, 0), _GREY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GREY]),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _BORDER),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, _BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(lines_table)
    story.append(Spacer(1, 4 * mm))

    # --- Totals ---
    subtotal = float(doc.get("subtotal") or 0)
    tax = float(doc.get("tax_amount") or doc.get("tax") or 0)
    total = float(doc.get("total_amount") or doc.get("total") or 0) or subtotal + tax
    outstanding = float(doc.get("outstanding_balance") or doc.get("amount_outstanding") or 0)

    totals_data = [
        [Paragraph("Subtotal", s["total_label"]), Paragraph(_fmt_money(subtotal, currency), s["total_value"])],
    ]
    doc_taxes = doc.get("doc_taxes") or []
    if doc_taxes:
        for dt in doc_taxes:
            label = dt.get("label") or f"{dt.get('code', 'Tax')} ({float(dt.get('rate', 0)):.1f}%)"
            totals_data.append([
                Paragraph(label, s["total_label"]),
                Paragraph(_fmt_money(dt.get("amount", 0), currency), s["total_value"]),
            ])
    else:
        totals_data.append([Paragraph("Tax", s["total_label"]), Paragraph(_fmt_money(tax, currency), s["total_value"])])
    totals_data.append([Paragraph("Total", s["total_label"]), Paragraph(_fmt_money(total, currency), s["total_value"])])
    if outstanding > 0:
        totals_data.append([
            Paragraph("Outstanding", s["total_label"]),
            Paragraph(_fmt_money(outstanding, currency), ParagraphStyle("owed", parent=s["total_value"], textColor=colors.HexColor("#dc2626"))),
        ])

    # Wider totals section to prevent currency wrapping on large amounts
    right_w = (page_w - 2 * margin) * 0.45
    totals_table = Table(totals_data, colWidths=[right_w * 0.40, right_w * 0.60],
                         hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, _DARK),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
    ]))
    story.append(totals_table)

    # Terms & Conditions
    terms_text = doc.get("terms_text") or ""
    if terms_text:
        story.append(Spacer(1, 6 * mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_BORDER, spaceAfter=3 * mm))
        story.append(Paragraph("Terms &amp; Conditions", s["label"]))
        story.append(Paragraph(str(terms_text), s["value"]))

    # Note to Customer
    customer_note = doc.get("customer_note") or ""
    if customer_note:
        story.append(Spacer(1, 4 * mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_BORDER, spaceAfter=3 * mm))
        story.append(Paragraph("Note to Customer", s["label"]))
        story.append(Paragraph(str(customer_note), s["value"]))

    # Internal notes (legacy field - not customer-facing but kept for backward compat)
    notes = doc.get("notes") or ""
    if notes:
        story.append(Spacer(1, 4 * mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_BORDER, spaceAfter=3 * mm))
        story.append(Paragraph("Notes", s["label"]))
        story.append(Paragraph(str(notes), s["value"]))

    # --- Footer with branding (added via onFirstPage/onLaterPages) ---
    _fulfillment_stamp = doc.get("fulfillment_status") == "fulfilled"

    def _add_footer(canvas, doc_tmpl):
        canvas.saveState()
        canvas.setFont(_FONT, 7)
        canvas.setFillColor(_GREY)
        brand = f"{_BRAND_TEXT} \u00b7 {_BRAND_URL}"
        canvas.drawCentredString(page_w / 2, 10 * mm, brand)
        page_num = canvas.getPageNumber()
        canvas.drawRightString(page_w - margin, 10 * mm, f"Page {page_num}")

        # Fulfilled stamp: small green badge in top-right corner
        if _fulfillment_stamp:
            canvas.setFont(_FONT_BOLD, 10)
            canvas.setFillColor(colors.HexColor("#16a34a"))
            canvas.drawRightString(page_w - margin, page_h - margin + 4 * mm, "\u2713 Fulfilled")

        canvas.restoreState()

    pdf.build(story, onFirstPage=_add_footer, onLaterPages=_add_footer)
    return buf.getvalue()


def _doc_title(doc: dict) -> str:
    raw = doc.get("doc_type") or "Document"
    label = _DOC_TYPE_LABELS.get(raw, raw.replace("_", " ").title())
    ref = doc.get("ref_id") or doc.get("doc_number") or doc.get("entity_id", "")
    return f"{label} {ref} - Celerp"
