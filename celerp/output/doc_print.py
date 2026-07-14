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
    A, Body, Div, Head, Html, Meta, P, Script, Span, Strong, Style,
    Table, Tbody, Td, Th, Thead, Title, Tr, to_xml,
)

from celerp.services.line_measures import measure_sublines, qty_label
from celerp.services.shipping import REASON_EXPORT_LABELS, SHIPPING_LIST_TYPE
from celerp.services.units import DEFAULT_UNITS, build_unit_map

EMPTY = "--"

# Doc types that show pieces/weight measure sub-lines (the invoice layout).
INVOICE_LAYOUT_DOC_TYPES: frozenset[str] = frozenset({"invoice", "memo", "list"})

# Doc types a recipient can import on their own instance; only these get the
# Import-into-Celerp footer link (an import link on a memo would just 422).
IMPORTABLE_DOC_TYPES: frozenset[str] = frozenset({"invoice", "purchase_order", "quotation", "list"})

_BRAND_URL = "https://www.celerp.com"
_BRAND_LABEL = "Powered by Celerp.com - Downloadable ERP for Serious Businesses"

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
.dp-lines thead { display: table-header-group; }
.dp-lines tr { break-inside: avoid; }
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
/* Shipment paperwork (delivery note / commercial invoice) */
.dp-shipmeta { display: flex; flex-wrap: wrap; gap: 3mm 8mm; margin-bottom: 6mm; padding: 3mm 4mm; border: 1px solid #ccc; }
.dp-shipmeta-cell { min-width: 28mm; }
.dp-shipmeta-lbl { font-size: 7.5pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #888; }
.dp-shipmeta-val { font-size: 9.5pt; font-weight: 600; min-height: 4mm; }
/* FedEx-style bordered header grid + totals band for the commercial invoice. */
.dp-fx { width: 100%; border-collapse: collapse; margin-bottom: 5mm; font-size: 9pt; table-layout: fixed; }
.dp-fx > tbody > tr > td { border: 1px solid #999; padding: 1.5mm 2mm; vertical-align: top; }
.dp-fx .lbl { display: block; font-size: 7.5pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: #666; margin-bottom: 0.5mm; }
.dp-fx .val { font-size: 9.5pt; font-weight: 600; min-height: 4mm; line-height: 1.4; }
.dp-fx .val p { margin: 0; font-weight: inherit; }
.dp-fx-nest { padding: 0 !important; }
.dp-fx--inner { margin-bottom: 0; height: 100%; }
.dp-fx--inner > tbody > tr > td { border-width: 0 0 1px 0; }
.dp-fx--inner > tbody > tr:last-child > td { border-bottom: 0; }
.dp-fx--totals { margin-top: 2mm; }
.dp-declaration { margin-top: 6mm; font-size: 9pt; color: #111; border-top: 1px solid #ddd; padding-top: 3mm; }
/* Vertical signature: Name / Signature / Date, each on its own write-on line. */
.dp-sign-v { margin-top: 4mm; max-width: 90mm; }
.dp-sign-v > div { display: flex; align-items: flex-end; gap: 3mm; margin-top: 5mm; font-size: 8.5pt; color: #555; }
.dp-sign-v > div > span:first-child { flex: 0 0 18mm; }
.dp-sign-v .line { flex: 1; border-bottom: 1px solid #111; min-height: 5mm; }
.dp-footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 3mm 20mm; border-top: 1px solid #ddd; font-size: 8pt; background: white; display: flex; justify-content: space-between; align-items: center; gap: 8mm; }
.dp-footer a { color: #aaa; text-decoration: none; }
.dp-footer a:hover { text-decoration: underline; }
.dp-footer__sep { color: #ddd; }
/* Screen-only online-payment bar: paper carries no buttons. */
.dp-paybar { display: flex; justify-content: space-between; align-items: center; gap: 8mm; margin-bottom: 8mm; padding: 4mm 6mm; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; font-size: 10pt; color: #14532d; }
.dp-paybar__btn { display: inline-block; padding: 3mm 8mm; background: #16a34a; color: #fff; border-radius: 6px; text-decoration: none; font-weight: 700; }
.dp-paybar__btn:hover { background: #15803d; }
@media print { .dp-paybar { display: none; } }
@page { margin: 0; size: A4 portrait; }
/* Reserve room for the fixed footer, which repeats on every printed page. */
@media print { body { padding: 15mm; padding-bottom: 24mm; } }
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


def _pay_bar(pay_url: str | None, doc: dict, currency: str):
    """Screen-only bar above the letterhead: amount due + a Pay button.
    Rendered only while something is outstanding, so a paid invoice's share
    link reverts to a plain view. Hidden in print CSS - paper carries no
    buttons."""
    if not pay_url:
        return None
    try:
        outstanding = float(doc.get("amount_outstanding", doc.get("total", 0)) or 0)
    except (TypeError, ValueError):
        outstanding = 0.0
    if outstanding <= 0:
        return None
    amount = fmt_money(outstanding, currency)
    return Div(
        Span("Amount due ", Strong(amount)),
        A(f"Pay {amount} now", href=pay_url, cls="dp-paybar__btn"),
        cls="dp-paybar",
    )


def render_doc_print_html(doc: dict, *, import_url: str | None = None,
                          pay_url: str | None = None, auto_print: bool = False,
                          layout: str | None = None) -> str:
    """Render the letterhead document page as a standalone HTML string.

    ``doc`` must already carry company_* letterhead fields, resolved contact
    fields, and (for invoice-layout types) enriched line measures - both the
    UI print routes and the API share view prepare it the same way.
    ``pay_url`` adds the screen-only payment bar (public share view of a
    payable doc on a Stripe-connected instance).

    A shipping document (list_type="shipping_doc") is one shipment record with
    two paper renderings, chosen via ``layout``: "delivery_note" (the default -
    quantities and logistics, structurally no prices) or "commercial_invoice"
    (customs values, HS codes, origin, incoterms, declaration). ``layout`` is
    only meaningful for shipping documents; passing it for anything else raises.
    """
    entity_id = doc.get("id") or doc.get("entity_id") or ""
    doc_type = doc.get("doc_type", "")
    is_shipping = doc_type == "list" and doc.get("list_type") == SHIPPING_LIST_TYPE
    if layout is not None and not is_shipping:
        raise ValueError("layout is only valid for shipping documents")
    if layout is not None and layout not in ("delivery_note", "commercial_invoice"):
        raise ValueError(f"Unknown shipping layout: {layout}")
    ship_layout = (layout or "delivery_note") if is_shipping else None
    doc_number = doc.get("doc_number") or doc.get("ref_id") or entity_id
    _src = str(doc.get("source_doc") or "")
    source_doc_ref = _src.split(":", 1)[1] if ":" in _src else _src
    if ship_layout:
        title = "Delivery Note" if ship_layout == "delivery_note" else "Commercial Invoice"
    else:
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

    def _line_weight(li: dict) -> str:
        w = li.get("weight")
        if w in (None, "", 0):
            return ""
        return f"{float(w):g} {li.get('weight_unit') or ''}".strip()

    def _line_gross(li: dict) -> str:
        w = li.get("gross_weight")
        if w in (None, "", 0):
            return ""
        return f"{float(w):g} {li.get('gross_weight_unit') or ''}".strip()

    def _weight_total(key: str, unit_key: str) -> str:
        """Sum a per-line weight - but only when EVERY line carries it in one shared
        unit. A partial sum would understate the shipment on customs paper, so
        incomplete data prints blank instead."""
        if not line_items or any(li.get(key) in (None, "", 0) for li in line_items):
            return ""
        units = {li.get(unit_key) or "" for li in line_items}
        if len(units) != 1:
            return ""
        total = sum(float(li.get(key) or 0) for li in line_items)
        return f"{total:g} {next(iter(units))}".strip() if total else ""

    rows = []
    if ship_layout:
        # Shipment paperwork: customs columns per line. Missing HS code / origin /
        # value print BLANK - never a fabricated placeholder on customs paper.
        # Net weight = the goods (the line's own weight); gross adds the item's
        # packaging (from the catalog's per-item gross weight).
        for li in line_items:
            qty = li.get("quantity") or li.get("qty") or 0
            price = li.get("unit_price") or li.get("price") or 0
            line_total = li.get("line_total") or (float(qty) * float(price))
            base = [
                Td(li.get("sku") or "", cls="mono"),
                Td(li.get("description") or li.get("name") or ""),
            ]
            if ship_layout == "delivery_note":
                # Receiver's checklist: goods and quantities only. Customs columns
                # (HS code, origin, values) belong to the commercial invoice.
                rows.append(Tr(*base, Td(_line_weight(li), cls="r"), Td(qty_label(li), cls="r")))
            else:
                rows.append(Tr(
                    *base,
                    Td(li.get("hs_code") or "", cls="mono"),
                    Td(li.get("country_of_origin") or ""),
                    Td(qty_label(li), cls="r"),
                    Td(_line_weight(li), cls="r"),
                    Td(_line_gross(li), cls="r"),
                    Td(fmt_rate(price, currency) if float(price or 0) else "", cls="r"),
                    Td(_money(line_total) if float(price or 0) else "", cls="r"),
                ))
        if ship_layout == "delivery_note":
            headers = Tr(Th("SKU"), Th("Description"), Th("Net Weight", cls="r"), Th("Qty", cls="r"))
        else:
            headers = Tr(Th("SKU"), Th("Description"), Th("HS Code"), Th("Country of Origin"),
                         Th("Qty", cls="r"), Th("Net Wt", cls="r"),
                         Th("Gross Wt", cls="r"), Th("Unit Value", cls="r"), Th("Total Value", cls="r"))
    else:
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

    if ship_layout == "delivery_note":
        totals_section = None  # no money on the paper that travels with the goods
    elif ship_layout == "commercial_invoice":
        # FedEx-style totals band: packages, net + gross weight, declared value.
        # Gross = the FINAL weighed package (typed on the shipment, box included);
        # fallback = sum of per-line gross weights when they share a unit.
        declared = sum(
            float(li.get("line_total") or 0)
            or float(li.get("quantity") or 0) * float(li.get("unit_price") or 0)
            for li in line_items
        )
        _total_gross = str(doc.get("gross_weight") or "").strip() or _weight_total("gross_weight", "gross_weight_unit")
        _totals_cells = [
            ("Total Packages", str(doc.get("package_count") or "")),
            ("Total Net Weight", _weight_total("weight", "weight_unit")),
            ("Total Gross Weight", _total_gross),
            (f"Total Declared Value ({currency})", _money(declared)),
        ]
        totals_section = Table(Tr(*[
            Td(Span(lbl, cls="lbl"), Div(val or " ", cls="val"))
            for lbl, val in _totals_cells
        ]), cls="dp-fx dp-fx--totals")
    else:
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
        totals_section = Div(Table(*totals_rows), cls="dp-totals")

    is_purchasing = doc_type in ("bill", "purchase_order", "consignment_in")

    # Shipment paperwork blocks. The commercial invoice follows the layout of the
    # standard carrier form (FedEx-style): a bordered header grid (date of export /
    # references / shipper / recipient / countries / importer / AWB / currency), the
    # goods table, a totals band, then the declaration with a vertical signature.
    shipmeta_section = None
    declaration_section = None
    signature_section = None

    def _sign_block(intro: str):
        """Vertical signature: Name / Signature / Date, each its own write-on line."""
        return Div(
            P(intro),
            Div(
                *[Div(Span(_lbl + ":"), Span(cls="line"))
                  for _lbl in ("Name", "Signature", "Date")],
                cls="dp-sign-v",
            ),
            cls="dp-declaration",
        )

    if ship_layout == "commercial_invoice":
        def _fx(label: str, value):
            return Td(Span(label, cls="lbl"), Div(value if value else " ", cls="val"))

        _exporter = Div(
            P(company_name, style="font-weight:600;"),
            P(company_address) if company_address else None,
            P(f"Tax ID: {company_tax_id}") if company_tax_id else None,
            P(company_phone) if company_phone else None,
        )
        _deliver_addr = ship_to_address or contact_address
        _recipient = Div(
            P(contact_name, style="font-weight:600;") if contact_name else None,
            P(contact_company) if contact_company and contact_company != contact_name else None,
            P(shipping_attn) if shipping_attn else None,
            P(_deliver_addr) if _deliver_addr else None,
            P(f"Tax ID: {contact_tax_id}") if contact_tax_id else None,
        )
        # Country of manufacture: one uniform line origin reads directly; mixed
        # origins defer to the per-line column (never a guessed single country).
        _origins = {li.get("country_of_origin") for li in line_items if li.get("country_of_origin")}
        _manufacture = next(iter(_origins)) if len(_origins) == 1 else ("See line items" if _origins else "")
        _refs = " / ".join(x for x in (doc_number, source_doc_ref) if x)
        _carrier_awb = " / ".join(x for x in (doc.get("carrier") or "", doc.get("tracking") or "") if x)
        shipmeta_section = Table(
            Tr(_fx("Date of Export", issue_date), _fx("Export References (order no., invoice no.)", _refs)),
            Tr(_fx("Shipper / Exporter", _exporter), _fx("Recipient / Consignee", _recipient)),
            Tr(Td(Table(
                    Tr(_fx("Country of Export", doc.get("country_of_export") or "")),
                    Tr(_fx("Country of Manufacture", _manufacture)),
                    Tr(_fx("Country of Ultimate Destination", doc.get("country_of_destination") or "")),
                    cls="dp-fx dp-fx--inner"),
                  cls="dp-fx-nest"),
               _fx("Importer - if other than recipient", doc.get("importer") or "")),
            Tr(_fx("Carrier / Air Waybill No.", _carrier_awb), _fx("Currency", currency)),
            Tr(_fx("Incoterms", doc.get("incoterms") or ""),
               _fx("Reason for Export", REASON_EXPORT_LABELS.get(doc.get("reason_for_export") or "", ""))),
            cls="dp-fx",
        )
        parties_section = None  # the grid IS the parties block
        declaration_section = _sign_block(
            "I declare all the information contained in this invoice to be true and correct.")
    elif ship_layout == "delivery_note":
        # The delivery note keeps a light strip and prints only the logistics that
        # are actually set - it is a receiving document, not a customs form.
        _gross = str(doc.get("gross_weight") or "").strip() or _weight_total("gross_weight", "gross_weight_unit")
        _set = [(l, v) for l, v in (
            ("Carrier", doc.get("carrier") or ""),
            ("Tracking", doc.get("tracking") or ""),
            ("Incoterms", doc.get("incoterms") or ""),
            ("Packages", str(doc.get("package_count") or "")),
            ("Gross Weight", _gross),
        ) if v]
        if _set:
            shipmeta_section = Div(*[
                Div(P(l, cls="dp-shipmeta-lbl"), P(v, cls="dp-shipmeta-val"), cls="dp-shipmeta-cell")
                for l, v in _set], cls="dp-shipmeta")
        _deliver_addr = ship_to_address or contact_address
        parties_section = Div(Div(
            P("Deliver To", cls="dp-party-label"),
            P(contact_name, cls="dp-party-name") if contact_name else None,
            Div(
                P(contact_company) if contact_company and contact_company != contact_name else None,
                P(shipping_attn) if shipping_attn else None,
                P(_deliver_addr) if _deliver_addr else None,
                P(contact_email) if contact_email else None,
                cls="dp-party-sub",
            ),
        ), cls="dp-parties")
        signature_section = _sign_block("Received in good order:")
    elif is_purchasing:
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
            _pay_bar(pay_url, doc, currency),
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
                        P(Strong("Due: "), due_date) if due_date and not ship_layout else None,
                        cls="dp-doc-meta",
                    ),
                ),
                cls="dp-header",
            ),
            parties_section,
            shipmeta_section,
            Table(Thead(headers), Tbody(*rows), cls="dp-lines") if rows else P("No line items.", style="font-size:9pt;color:#888;margin-bottom:4mm;"),
            totals_section,
            Div(P("Notes", cls="dp-notes-label"), P(notes_text), cls="dp-notes") if notes_text else None,
            declaration_section,
            signature_section,
            _doc_footer(import_url),
            Script("window.onload = function() { window.print(); }") if auto_print else None,
        ),
    )
    return to_xml(page)
