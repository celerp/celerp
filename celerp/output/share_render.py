# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Official public share-view renderers (proprietary).

These produce the branded, "Powered by Celerp"-marked public HTML for shared documents and lists. They
are proprietary so the attribution is emitted by proprietary components, not by the MIT celerp-docs
module. The share routes/auth remain in celerp-docs (MIT) and pass the accept URL in as a parameter.
"""
from __future__ import annotations


def _fmt_money(v, currency: str = "USD") -> str:
    try:
        return f"{currency} {float(v):,.2f}"
    except (TypeError, ValueError):
        return "--"


def _esc(s) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _public_doc_page(doc: dict, token: str, accept_url: str) -> str:
    doc_type = doc.get("doc_type", "document")
    type_label = doc_type.replace("_", " ").title()
    ref = doc.get("ref_id") or doc.get("doc_number") or doc.get("entity_id", "")
    contact = doc.get("contact_name") or doc.get("contact_id") or ""
    status = doc.get("status", "draft")
    issue_date = (doc.get("issue_date") or doc.get("created_at") or "")[:10]
    due_date = (doc.get("due_date") or doc.get("payment_due_date") or "")[:10]
    currency = doc.get("currency", "USD")
    notes = doc.get("notes") or ""

    lines_html = ""
    for li in doc.get("line_items") or []:
        qty = li.get("quantity", 0)
        price = li.get("unit_price", 0)
        try:
            total = float(qty or 0) * float(price or 0)
            total_str = _fmt_money(total, currency)
        except (TypeError, ValueError):
            total_str = "--"
        lines_html += f"""
        <tr>
          <td>{_esc(li.get("description") or li.get("name") or li.get("sku") or "")}</td>
          <td class="num">{_esc(li.get("sku") or "")}</td>
          <td class="num">{_esc(qty)}</td>
          <td class="num">{_fmt_money(price, currency)}</td>
          <td class="num">{total_str}</td>
        </tr>"""

    subtotal = _fmt_money(doc.get("subtotal") or doc.get("subtotal_amount"), currency)
    tax = _fmt_money(doc.get("tax") or doc.get("tax_amount"), currency)
    total = _fmt_money(doc.get("total") or doc.get("total_amount"), currency)

    # Import stays a quiet footer link after the brand line: the page is the
    # sender's document, not a Celerp pitch, and it only means something to
    # recipients who also run Celerp. The accept page it leads to handles
    # signup and the bundle-download fallback, so no separate download link.
    footer_links = ""
    if doc_type in ("invoice", "purchase_order", "quotation"):
        footer_links = f'<a href="{_esc(accept_url)}">Import into Celerp</a>'

    notes_html = f'<p class="doc-notes">{_esc(notes)}</p>' if notes else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(type_label)} {_esc(ref)}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, sans-serif; color: #111; background: #f9f9f9; margin: 0; padding: 24px 16px; }}
    .doc-wrap {{ max-width: 760px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1); overflow: hidden; }}
    .doc-header {{ padding: 28px 32px 20px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; }}
    .doc-header h1 {{ margin: 0; font-size: 22px; }}
    .badge {{ display: inline-block; padding: 3px 10px; border-radius: 99px; font-size: 12px; font-weight: 600; background: #e5e7eb; color: #374151; }}
    .badge--paid {{ background: #d1fae5; color: #065f46; }}
    .badge--draft {{ background: #f3f4f6; color: #6b7280; }}
    .badge--sent {{ background: #dbeafe; color: #1e40af; }}
    .badge--void {{ background: #fee2e2; color: #991b1b; }}
    .doc-meta {{ padding: 16px 32px; display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; border-bottom: 1px solid #eee; }}
    .meta-item label {{ font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .05em; display: block; margin-bottom: 2px; }}
    .meta-item span {{ font-size: 14px; }}
    .doc-lines {{ padding: 16px 32px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid #e5e7eb; font-size: 12px; color: #6b7280; }}
    td {{ padding: 8px 6px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
    .num {{ text-align: right; }}
    .totals-row {{ font-weight: 600; border-top: 2px solid #e5e7eb; }}
    .doc-notes {{ padding: 0 32px 16px; font-size: 14px; color: #374151; white-space: pre-wrap; }}
    .doc-brand {{ padding: 14px 32px; border-top: 1px solid #eee; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; font-size: 12px; color: #9ca3af; }}
    .doc-brand a {{ color: #9ca3af; text-decoration: none; }}
    .doc-brand a:hover {{ text-decoration: underline; }}
    @media print {{
      .doc-brand {{ display: none; }}
      body {{ background: #fff; padding: 0; }}
      .doc-wrap {{ box-shadow: none; }}
    }}
  </style>
</head>
<body>
  <div class="doc-wrap">
    <div class="doc-header">
      <div>
        <h1>{_esc(type_label)} #{_esc(ref)}</h1>
        {f'<span style="font-size:14px;color:#6b7280">{_esc(contact)}</span>' if contact else ""}
      </div>
      <span class="badge badge--{_esc(status)}">{_esc(status)}</span>
    </div>
    <div class="doc-meta">
      {f'<div class="meta-item"><label>Issue Date</label><span>{_esc(issue_date)}</span></div>' if issue_date else ""}
      {f'<div class="meta-item"><label>Due Date</label><span>{_esc(due_date)}</span></div>' if due_date else ""}
      <div class="meta-item"><label>Currency</label><span>{_esc(currency)}</span></div>
    </div>
    <div class="doc-lines">
      <table>
        <thead>
          <tr>
            <th>Description</th><th>SKU</th><th class="num">Qty</th>
            <th class="num">Unit Price</th><th class="num">Total</th>
          </tr>
        </thead>
        <tbody>{lines_html}</tbody>
        <tfoot>
          <tr><td colspan="4" class="num">Subtotal</td><td class="num">{subtotal}</td></tr>
          <tr><td colspan="4" class="num">Tax</td><td class="num">{tax}</td></tr>
          <tr class="totals-row"><td colspan="4" class="num">Total</td><td class="num">{total}</td></tr>
        </tfoot>
      </table>
    </div>
    {notes_html}
    <div class="doc-brand">
      <a href="https://www.celerp.com" target="_blank" rel="noopener">Powered by Celerp - Downloadable ERP for Serious Businesses</a>{footer_links}
    </div>
  </div>
</body>
</html>"""


def _public_list_page(lst: dict, token: str, accept_url: str) -> str:
    """Render a shared list (quotation-style) for a customer to view in-browser."""
    list_type = lst.get("list_type", "list")
    type_label = list_type.replace("_", " ").title()
    ref = lst.get("ref_id") or lst.get("entity_id", "")
    contact = lst.get("contact_name") or lst.get("contact_id") or ""
    status = lst.get("status", "draft")
    notes = lst.get("notes") or ""
    currency = lst.get("currency", "USD")
    valid_until = (lst.get("valid_until") or "")[:10]

    lines_html = ""
    for li in lst.get("line_items") or []:
        qty = li.get("quantity", 0)
        price = li.get("unit_price") or li.get("price", 0)
        line_total = li.get("line_total") or (float(qty or 0) * float(price or 0))
        lines_html += f"""
        <tr>
          <td>{_esc(li.get("name") or li.get("description") or li.get("sku") or "")}</td>
          <td>{_esc(li.get("sku") or "")}</td>
          <td class="num">{_esc(qty)}</td>
          <td class="num">{_fmt_money(price, currency)}</td>
          <td class="num">{_fmt_money(line_total, currency)}</td>
        </tr>"""

    subtotal = _fmt_money(lst.get("subtotal"), currency)
    discount = lst.get("discount", 0) or 0
    discount_type = lst.get("discount_type", "flat")
    discount_amount = _fmt_money(lst.get("discount_amount", discount), currency)
    tax = _fmt_money(lst.get("tax_amount"), currency)
    total = _fmt_money(lst.get("total"), currency)

    discount_row = ""
    if float(discount or 0) > 0:
        label = f"Discount ({discount}%)" if discount_type == "percentage" else "Discount"
        discount_row = f'<tr><td colspan="4" class="num">{_esc(label)}</td><td class="num">- {discount_amount}</td></tr>'

    # Same quiet-footer treatment as documents: import is a small utility link
    # for recipients who run Celerp, not a pitch on the sender's page.
    footer_links = f'<a href="{_esc(accept_url)}">Import into Celerp</a>'

    notes_html = f'<p class="doc-notes">{_esc(notes)}</p>' if notes else ""
    valid_until_html = (
        f'<div class="meta-item"><label>Valid Until</label><span>{_esc(valid_until)}</span></div>'
        if valid_until else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(type_label)} {_esc(ref)}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, sans-serif; color: #111; background: #f9f9f9; margin: 0; padding: 24px 16px; }}
    .doc-wrap {{ max-width: 760px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1); overflow: hidden; }}
    .doc-header {{ padding: 28px 32px 20px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; }}
    .doc-header h1 {{ margin: 0; font-size: 22px; }}
    .badge {{ display: inline-block; padding: 3px 10px; border-radius: 99px; font-size: 12px; font-weight: 600; background: #e5e7eb; color: #374151; }}
    .badge--sent {{ background: #dbeafe; color: #1e40af; }}
    .badge--accepted {{ background: #d1fae5; color: #065f46; }}
    .badge--draft {{ background: #f3f4f6; color: #6b7280; }}
    .badge--void {{ background: #fee2e2; color: #991b1b; }}
    .doc-meta {{ padding: 16px 32px; display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; border-bottom: 1px solid #eee; }}
    .meta-item label {{ font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .05em; display: block; margin-bottom: 2px; }}
    .meta-item span {{ font-size: 14px; }}
    .doc-lines {{ padding: 16px 32px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid #e5e7eb; font-size: 12px; color: #6b7280; }}
    td {{ padding: 8px 6px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
    .num {{ text-align: right; }}
    .totals-row {{ font-weight: 600; border-top: 2px solid #e5e7eb; }}
    .doc-notes {{ padding: 0 32px 16px; font-size: 14px; color: #374151; white-space: pre-wrap; }}
    .doc-brand {{ padding: 14px 32px; border-top: 1px solid #eee; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; font-size: 12px; color: #9ca3af; }}
    .doc-brand a {{ color: #9ca3af; text-decoration: none; }}
    .doc-brand a:hover {{ text-decoration: underline; }}
    @media print {{
      .doc-brand {{ display: none; }}
      body {{ background: #fff; padding: 0; }}
      .doc-wrap {{ box-shadow: none; }}
    }}
  </style>
</head>
<body>
  <div class="doc-wrap">
    <div class="doc-header">
      <div>
        <h1>{_esc(type_label)} #{_esc(ref)}</h1>
        {f'<span style="font-size:14px;color:#6b7280">{_esc(contact)}</span>' if contact else ""}
      </div>
      <span class="badge badge--{_esc(status)}">{_esc(status)}</span>
    </div>
    <div class="doc-meta">
      <div class="meta-item"><label>Currency</label><span>{_esc(currency)}</span></div>
      {valid_until_html}
    </div>
    <div class="doc-lines">
      <table>
        <thead>
          <tr>
            <th>Item</th><th>SKU</th><th class="num">Qty</th>
            <th class="num">Unit Price</th><th class="num">Total</th>
          </tr>
        </thead>
        <tbody>{lines_html}</tbody>
        <tfoot>
          <tr><td colspan="4" class="num">Subtotal</td><td class="num">{subtotal}</td></tr>
          {discount_row}
          <tr><td colspan="4" class="num">Tax</td><td class="num">{tax}</td></tr>
          <tr class="totals-row"><td colspan="4" class="num">Total</td><td class="num">{total}</td></tr>
        </tfoot>
      </table>
    </div>
    {notes_html}
    <div class="doc-brand">
      <a href="https://www.celerp.com" target="_blank" rel="noopener">Powered by Celerp - Downloadable ERP for Serious Businesses</a>{footer_links}
    </div>
  </div>
</body>
</html>"""


def _not_found_page(reason: str = "not-found") -> str:
    messages = {
        "link-expired": ("Link no longer active", "This share link has been revoked or has expired."),
        "doc-missing": ("Document unavailable", "The document this link pointed to no longer exists on the sender's instance."),
        "not-found": ("Document not found", "This link may be incorrect or the sender's instance may be offline."),
    }
    title, body = messages.get(reason, messages["not-found"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{_esc(title)}</title>
  <style>
    body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f9f9f9;}}
    .box{{text-align:center;padding:40px;max-width:420px;}}
    .box h1{{font-size:20px;margin-bottom:8px;}}
    .box p{{color:#6b7280;margin-bottom:24px;}}
    .box a.btn{{display:inline-block;padding:10px 22px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none;font-weight:600;}}
    .box a.btn:hover{{background:#1d4ed8;}}
  </style>
</head>
<body>
  <div class="box">
    <h1>{_esc(title)}</h1>
    <p>{_esc(body)} Contact the sender for a current copy.</p>
    <a class="btn" href="https://www.celerp.com">Learn about Celerp →</a>
  </div>
</body>
</html>"""
