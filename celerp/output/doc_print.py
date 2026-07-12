# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Single document HTML renderer - the layout behind the Print button AND the
public share view.

Both surfaces render the same letterhead document from the same code so they
can never drift apart. The only per-surface differences are flags: the print
routes auto-trigger window.print(), and both surfaces pass the accept URL so
the footer carries the Import-into-Celerp link (print/PDF copies keep working
as an import entry point).

Also home to the currency/address display helpers this layout needs; the UI
re-exports them (ui.components.table) so there is one definition.
"""
from __future__ import annotations

from fasthtml.common import (
    A, Body, Div, Head, Html, Meta, NotStr, P, Script, Span, Strong, Style,
    Table, Tbody, Td, Th, Thead, Title, Tr, to_xml,
)

from celerp.services.line_measures import measure_sublines, qty_label
from celerp.services.units import DEFAULT_UNITS, build_unit_map

EMPTY = "--"

# Doc types that show pieces/weight measure sub-lines (the invoice layout).
INVOICE_LAYOUT_DOC_TYPES: frozenset[str] = frozenset({"invoice", "memo", "list"})

# Doc types a recipient can import on their own instance; only these get the
# Import-into-Celerp footer link (an import link on a memo would just 422).
IMPORTABLE_DOC_TYPES: frozenset[str] = frozenset({"invoice", "purchase_order", "quotation", "list"})

_BRAND_URL = "https://www.celerp.com"
_BRAND_LABEL = "Powered by Celerp - Downloadable ERP for Serious Businesses"

CURRENCY_SYMBOLS = {
    "AED": "AED ", "ARS": "AR$", "AUD": "A$", "BDT": "৳", "BRL": "R$",
    "CAD": "C$", "CHF": "CHF ", "CLP": "CL$", "CNY": "¥", "COP": "CO$",
    "CZK": "Kč ", "DKK": "kr ", "EGP": "E£", "EUR": "€", "GBP": "£",
    "HKD": "HK$", "HUF": "Ft ", "IDR": "Rp ", "ILS": "₪", "INR": "₹",
    "JPY": "¥", "KRW": "₩", "KWD": "KD ", "LKR": "₨ ", "MAD": "MAD ",
    "MXN": "MX$", "MYR": "RM ", "NGN": "₦",
    "NOK": "kr ", "NZD": "NZ$", "PEN": "S/", "PHP": "₱", "PKR": "₨ ",
    "PLN": "zł ", "QAR": "QR ", "RON": "lei ", "RUB": "₽", "SAR": "SR ",
    "SEK": "kr ", "SGD": "S$", "THB": "฿", "TRY": "₺", "TWD": "NT$",
    "UAH": "₴", "USD": "$", "VND": "₫", "ZAR": "R ",
}


def currency_symbol(currency: str | None) -> str:
    """Return the display symbol for an ISO 4217 currency code. Falls back to code + space."""
    if not currency:
        return ""
    return CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")


def fmt_money(value: str | float, currency: str | None = None) -> str:
    """Format a money AMOUNT (total, tax, etc.) at currency precision."""
    sym = currency_symbol(currency)
    try:
        return f"{sym}{float(value):,.2f}"
    except (ValueError, TypeError):
        return EMPTY


def fmt_rate(value: str | float, currency: str | None = None) -> str:
    """Format a unit price (a RATE): currency symbol + at least currency_dp decimals, up to rate_dp,
    with trailing zeros beyond currency precision trimmed (15.28, 15.285, 15.30 - never 15.2850)."""
    from celerp.services.money import currency_dp as _cdp, rate_dp as _rdp
    sym = currency_symbol(currency)
    try:
        v = float(value)
    except (ValueError, TypeError):
        return EMPTY
    lo, hi = _cdp(currency or "USD"), _rdp(currency or "USD")
    s = f"{v:,.{hi}f}"
    if "." in s:
        intp, frac = s.split(".")
        frac = frac.rstrip("0")
        if len(frac) < lo:
            frac = frac.ljust(lo, "0")
        s = f"{intp}.{frac}" if frac else intp
    return f"{sym}{s}"


def unwrap_address(raw) -> str:
    """Unwrap an address value that may be a dict (``{"text": "…"}``) or a plain string.

    Single source of truth for all address display - used by settings, documents, etc.
    """
    if not raw:
        return ""
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("line1") or ""
        for k in ("line2", "city", "state", "postal_code", "country"):
            v = raw.get(k) or ""
            if v:
                text = text + ("\n" if text else "") + v
        return text
    return str(raw)


def compose_address(a: dict) -> str:
    """One-line address from a contact address dict (for the letterhead)."""
    parts = [a.get("line1", ""), a.get("line2", ""), a.get("city", ""), a.get("state", ""),
             a.get("postal_code", ""), a.get("country", "")]
    return ", ".join(p for p in parts if p)


DOC_PRINT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, sans-serif; font-size: 10pt; color: #111; background: white; padding: 20mm; }
.dp-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8mm; padding-bottom: 4mm; border-bottom: 2px solid #111; }
.dp-company-name { font-size: 14pt; font-weight: 700; margin-bottom: 2mm; }
.dp-company-sub { font-size: 9pt; color: #555; line-height: 1.5; }
.dp-doc-title { font-size: 18pt; font-weight: 700; text-align: right; text-transform: uppercase; letter-spacing: 0.03em; }
.dp-doc-meta { font-size: 9pt; text-align: right; margin-top: 2mm; line-height: 1.6; color: #333; }
.dp-doc-meta strong { color: #111; }
.dp-parties { display: flex; gap: 10mm; margin-bottom: 6mm; }
.dp-party { flex: 1; }
.dp-party-label { font-size: 8pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #888; margin-bottom: 1mm; }
.dp-party-name { font-size: 10pt; font-weight: 600; }
.dp-party-sub { font-size: 9pt; color: #444; line-height: 1.5; }
.dp-lines { width: 100%; border-collapse: collapse; margin-bottom: 4mm; font-size: 9pt; }
.dp-lines thead th { background: #f5f5f5; font-weight: 700; text-align: left; padding: 1.5mm 2mm; border-bottom: 1px solid #999; }
.dp-lines thead th.r { text-align: right; }
.dp-lines tbody td { padding: 1.5mm 2mm; border-bottom: 1px solid #eee; vertical-align: top; }
.dp-lines tbody td.r { text-align: right; }
.dp-lines tbody td.mono { font-family: 'Courier New', monospace; font-size: 8.5pt; }
.dp-totals { display: flex; flex-direction: column; align-items: flex-end; margin-bottom: 6mm; }
.dp-totals table { border-collapse: collapse; min-width: 60mm; }
.dp-totals td { padding: 1mm 2mm; font-size: 9.5pt; }
.dp-totals td.label { text-align: left; color: #555; }
.dp-totals td.amount { text-align: right; font-weight: 600; }
.dp-totals tr.grand td { border-top: 2px solid #111; font-size: 11pt; font-weight: 700; padding-top: 2mm; }
.dp-notes { margin-top: 4mm; font-size: 9pt; color: #444; border-top: 1px solid #ddd; padding-top: 3mm; }
.dp-notes-label { font-weight: 700; color: #111; margin-bottom: 1mm; }
.dp-footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 3mm 20mm; border-top: 1px solid #ddd; font-size: 8pt; background: white; display: flex; justify-content: space-between; align-items: center; gap: 8mm; }
.dp-footer a { color: #aaa; text-decoration: none; }
.dp-footer a:hover { text-decoration: underline; }
.dp-footer__sep { color: #ddd; }
@page { margin: 0; size: A4 portrait; }
@media print { body { padding: 15mm; } }
"""


def _doc_footer(import_url: str | None):
    """One footer format everywhere: brand left, pipe, import link right.
    The import link is only rendered when the document's share link is live,
    so a printed or PDF'd copy never carries a URL that 404s."""
    brand = A(_BRAND_LABEL, href=_BRAND_URL, target="_blank", rel="noopener")
    if not import_url:
        return Div(brand, cls="dp-footer")
    return Div(
        brand,
        Span("|", cls="dp-footer__sep"),
        A("Import into Celerp", href=import_url),
        cls="dp-footer",
    )


def render_doc_print_html(doc: dict, *, import_url: str | None = None, auto_print: bool = False) -> str:
    """Render the letterhead document page as a standalone HTML string.

    ``doc`` must already carry company_* letterhead fields, resolved contact
    fields, and (for invoice-layout types) enriched line measures - both the
    UI print routes and the API share view prepare it the same way.
    """
    entity_id = doc.get("id") or doc.get("entity_id") or ""
    doc_type = doc.get("doc_type", "")
    doc_number = doc.get("doc_number") or doc.get("ref_id") or entity_id
    title = doc_type.replace("_", " ").title() if doc_type else "Document"
    issue_date = (doc.get("issue_date") or "")[:10]
    due_date = (doc.get("due_date") or "")[:10]
    currency = doc.get("currency") or "USD"

    company_name = doc.get("company_name") or ""
    company_address = doc.get("company_address") or ""
    company_tax_id = doc.get("company_tax_id") or ""
    company_email = doc.get("company_email") or ""
    company_phone = doc.get("company_phone") or ""

    contact_name = doc.get("contact_name") or doc.get("customer_name") or ""
    contact_company = doc.get("contact_company_name") or ""
    contact_address = doc.get("contact_billing_address") or doc.get("contact_address") or ""
    contact_tax_id = doc.get("contact_tax_id") or ""
    contact_email = doc.get("contact_email") or ""
    ship_to_address = doc.get("contact_shipping_address") or ""
    shipping_attn = doc.get("shipping_attn") or ""

    line_items = doc.get("line_items") or []

    def _money(v) -> str:
        try:
            return fmt_money(v, currency)
        except Exception:
            sym = currency_symbol(currency)
            return f"{sym}{float(v or 0):,.2f}"

    has_disc = any(li.get("discount_pct") for li in line_items)
    _print_umap = build_unit_map(DEFAULT_UNITS)
    rows = []
    for li in line_items:
        qty = li.get("quantity") or li.get("qty") or 0
        price = li.get("unit_price") or li.get("price") or 0
        disc = li.get("discount_pct") or 0
        line_total = li.get("line_total") or (float(qty) * float(price) * (1 - float(disc) / 100))
        # Description holds only the description (the SKU has its own column) plus
        # Pieces / Weight as labelled sub-lines when the line carries them.
        desc = li.get("description") or li.get("name") or ""
        sku = li.get("sku") or ""
        _ls = "margin:0;font-size:8.5pt;"
        _desc_parts = [P(f"- {desc}", style=_ls)]
        # Pieces/Weight sub-lines are sales (invoice-layout) only; keep vendor docs clean.
        if doc_type in INVOICE_LAYOUT_DOC_TYPES:
            _desc_parts += [P(_ln, style=_ls) for _ln in measure_sublines(li, unit_map=_print_umap)]
        rows.append(Tr(
            Td(sku, cls="mono"),
            Td(Div(*_desc_parts)),
            Td(qty_label(li), cls="r"),
            Td(fmt_rate(price, currency), cls="r"),
            *([] if not has_disc else [Td(f"{disc}%" if disc else "", cls="r")]),
            Td(_money(line_total), cls="r"),
        ))

    headers = Tr(
        Th("SKU"), Th("Description"), Th("Qty", cls="r"), Th("Unit Price", cls="r"),
        *([] if not has_disc else [Th("Disc%", cls="r")]),
        Th("Amount", cls="r"),
    )

    subtotal = doc.get("subtotal") or 0
    tax_total = doc.get("tax_total") or doc.get("tax") or 0
    grand_total = doc.get("grand_total") or doc.get("total") or 0
    notes_text = doc.get("notes") or doc.get("terms") or ""

    totals_rows = [Tr(Td("Subtotal", cls="label"), Td(_money(subtotal), cls="amount"))]
    # Header (whole-document) discount, when set. grand_total/total already reflects it.
    _disc_amt = float(doc.get("discount_amount") or 0)
    _disc_raw = float(doc.get("discount") or 0)
    _disc_type = doc.get("discount_type") or "flat"
    if not _disc_amt and _disc_raw:
        _disc_amt = subtotal * _disc_raw / 100 if _disc_type == "percentage" else _disc_raw
    if _disc_amt > 0.005:
        _dlabel = f"Discount ({_disc_raw:g}%)" if _disc_type == "percentage" and _disc_raw else "Discount"
        totals_rows.append(Tr(Td(_dlabel, cls="label"), Td(f"-{_money(_disc_amt)}", cls="amount")))
    if float(tax_total or 0):
        totals_rows.append(Tr(Td("Tax", cls="label"), Td(_money(tax_total), cls="amount")))
    totals_rows.append(Tr(Td("Total", cls="label"), Td(_money(grand_total), cls="amount"), cls="grand"))

    is_purchasing = doc_type in ("bill", "purchase_order", "consignment_in")

    if is_purchasing:
        # Vendor = the contact (supplier); Bill To = us (the company)
        vendor_box = Div(
            P("Vendor", cls="dp-party-label"),
            P(contact_name, cls="dp-party-name") if contact_name else None,
            Div(
                P(contact_company) if contact_company and contact_company != contact_name else None,
                P(contact_address) if contact_address else None,
                P(f"Tax ID: {contact_tax_id}") if contact_tax_id else None,
                P(contact_email) if contact_email else None,
                cls="dp-party-sub",
            ),
        ) if contact_name else None
        bill_to_box = Div(
            P("Bill To", cls="dp-party-label"),
            P(company_name, cls="dp-party-name") if company_name else None,
            Div(
                P(company_address) if company_address else None,
                P(f"Tax ID: {company_tax_id}") if company_tax_id else None,
                P(company_email) if company_email else None,
                cls="dp-party-sub",
            ),
        )
        ship_to_box = Div(
            P("Ship To", cls="dp-party-label"),
            P(shipping_attn, cls="dp-party-name") if shipping_attn else None,
            Div(
                P(ship_to_address) if ship_to_address else None,
                cls="dp-party-sub",
            ),
        ) if ship_to_address else None
        parties_section = Div(vendor_box, bill_to_box, ship_to_box, cls="dp-parties")
    else:
        # Sales docs: Bill To = the contact (customer)
        parties_section = Div(
            Div(
                P("Bill To", cls="dp-party-label"),
                P(contact_name, cls="dp-party-name") if contact_name else None,
                Div(
                    P(contact_company) if contact_company and contact_company != contact_name else None,
                    P(contact_address) if contact_address else None,
                    P(f"Tax ID: {contact_tax_id}") if contact_tax_id else None,
                    P(contact_email) if contact_email else None,
                    cls="dp-party-sub",
                ),
            ) if contact_name else None,
            Div(
                P("Ship To", cls="dp-party-label"),
                P(shipping_attn, cls="dp-party-name") if shipping_attn else None,
                Div(P(ship_to_address) if ship_to_address else None, cls="dp-party-sub"),
            ) if ship_to_address else None,
            cls="dp-parties",
        )

    page = Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(f"{title} {doc_number}"),
            Style(DOC_PRINT_CSS),
        ),
        Body(
            Div(
                Div(
                    P(company_name, cls="dp-company-name"),
                    Div(
                        P(company_address) if company_address else None,
                        P(f"Tax ID: {company_tax_id}") if company_tax_id else None,
                        P(company_email) if company_email else None,
                        P(company_phone) if company_phone else None,
                        cls="dp-company-sub",
                    ),
                ),
                Div(
                    P(title, cls="dp-doc-title"),
                    Div(
                        P(Strong("No.: "), doc_number),
                        P(Strong("Date: "), issue_date) if issue_date else None,
                        P(Strong("Due: "), due_date) if due_date else None,
                        cls="dp-doc-meta",
                    ),
                ),
                cls="dp-header",
            ),
            parties_section,
            Table(Thead(headers), Tbody(*rows), cls="dp-lines") if rows else P("No line items.", style="font-size:9pt;color:#888;margin-bottom:4mm;"),
            Div(Table(*totals_rows), cls="dp-totals"),
            Div(P("Notes", cls="dp-notes-label"), P(notes_text), cls="dp-notes") if notes_text else None,
            _doc_footer(import_url),
            Script("window.onload = function() { window.print(); }") if auto_print else None,
        ),
    )
    return to_xml(page)
