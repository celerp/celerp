# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Presentation primitives shared by every financial report surface.

The report pages live under /reports and the journal stays under /accounting, but
both print, both export CSV, and both render the same balance chips. Keeping those
primitives here lets either side import them without importing the other, which is
what stops the two route modules from forming an import cycle.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date as _date
from urllib.parse import urlencode

from fasthtml.common import *
from starlette.responses import PlainTextResponse, RedirectResponse, StreamingResponse

from celerp.services.money import round_money, to_decimal
from ui.api_client import APIError
from ui.components.table import fmt_money
from ui.i18n import t

# SVG icons (matching documents.py style)
ICON_CSV_EXPORT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="12" y1="18" x2="12" y2="12"/>'
    '<polyline points="9 15 12 18 15 15"/></svg>'
)
ICON_PRINT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="6 9 6 2 18 2 18 9"/>'
    '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
    '<rect x="6" y="14" width="12" height="8"/></svg>'
)


def href(path: str, params) -> str:
    """path?k=v joining only non-empty params, values URL-encoded; bare path
    when none. Encoded values also keep header-borne redirects (HX-Redirect,
    Location) free of raw user text.

    params accepts a mapping or a sequence of (key, value) pairs. Pairs are what
    a repeated key needs: a statement covering several accounts sends account=
    once per account, which a mapping cannot express.
    """
    pairs = params.items() if hasattr(params, "items") else params
    qs = urlencode([(k, v) for k, v in pairs if v])
    return f"{path}?{qs}" if qs else path


def action_bar(print_path: str, csv_path: str, params) -> FT:
    """CSV + print icon buttons shown in the top-right corner of a report.

    Export before print, matching the document-list action order (creative,
    destructive, import/export, print).

    Takes both paths rather than deriving them from a shared key: the pages no
    longer share a URL stem, so a key would have to be translated back into a
    path here and the caller already knows it.
    """
    return Div(
        A(
            NotStr(ICON_CSV_EXPORT),
            href=href(csv_path, params),
            cls="btn btn--ghost btn--icon",
            title=t("btn.export_csv"),
        ),
        A(
            NotStr(ICON_PRINT),
            href=href(print_path, params),
            target="_blank",
            cls="btn btn--ghost btn--icon",
            title=t("btn.print"),
        ),
        cls="page-actions flex-row gap-sm ml-auto",
    )


def csv_safe(cell):
    """Neutralize spreadsheet formula injection: user-authored strings (memos, account and
    contact names) must never execute when the export is opened in a spreadsheet, so string
    cells starting with a formula trigger character get a leading single quote."""
    if isinstance(cell, str) and cell and cell[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + cell
    return cell


def csv_row(writer, cells: list) -> None:
    writer.writerow([csv_safe(c) for c in cells])


def csv_response(rows: list[list], filename: str) -> StreamingResponse:
    """Rows as a CSV download, every cell put through `csv_safe` on the way out.

    One writer for every export, so no route can grow its own and lose the formula
    guard or the download header with it.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        csv_row(writer, row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def plain_error_response(e: APIError):
    """Error mapping for print/export routes (non-shell responses).

    A 4xx keeps its own status: a report asked for with an amount filter that is
    not a number is an input error the reader can correct, and reporting it as a
    500 turns a typo into what reads as an outage.
    """
    if e.status == 401:
        return RedirectResponse("/login", status_code=302)
    if e.status == 403:
        return PlainTextResponse(t("acct.not_authorized"), status_code=403)
    status = e.status if 400 <= e.status < 500 else 500
    return PlainTextResponse(f"Error: {e.detail}", status_code=status)


def date_params(d_from: str, d_to: str) -> dict:
    return {k: v for k, v in {"date_from": d_from, "date_to": d_to}.items() if v}


def fname_date(v: str) -> str:
    """A date for a download filename: digits and dashes only, else 'all'.

    Filenames land in a Content-Disposition header, so raw query text must
    never reach them (header parameter injection, latin-1 encoding errors).
    """
    v = (v or "")[:10]
    return v if re.fullmatch(r"[0-9-]+", v) else "all"


def period_subtitle(d_from: str, d_to: str) -> str:
    parts = []
    if d_from:
        parts.append(f"From: {d_from}")
    if d_to:
        parts.append(f"To: {d_to}")
    return "  |  ".join(parts) if parts else "All periods"


def totals_chips(total_debit, total_credit, balanced: bool, currency: str | None) -> FT:
    return Div(
        Span(f"{t('acct.total_debit')}: {fmt_money(total_debit, currency)}", cls="val-chip"),
        Span(f"{t('acct.total_credit')}: {fmt_money(total_credit, currency)}", cls="val-chip"),
        Span(t("acct.balanced") if balanced else t("acct.out_of_balance"),
             cls="val-chip" if balanced else "val-chip val-chip--alert"),
        cls="valuation-bar",
    )


def journal_totals(data: dict, currency: str | None = None) -> FT:
    """The debit, credit and balance chips over a journal.

    The classical journal and the extended one are the same postings, so they read
    their totals through one function and can never disagree about them.
    """
    total_debit = float(data.get("total_debit", 0) or 0)
    total_credit = float(data.get("total_credit", 0) or 0)
    return totals_chips(total_debit, total_credit, abs(total_debit - total_credit) < 0.01, currency)


def je_source_label(entry: dict, csv_export: bool = False) -> str:
    """Source label for a journal entry without a source document, keyed off je_type.

    CSV exports always use the English label so the exported data is locale-stable.
    """
    je_type = str(entry.get("je_type") or "")
    if je_type == "manual":
        key = "acct.source_manual"
    elif je_type == "transfer":
        key = "acct.source_transfer"
    elif je_type.startswith("recon"):
        key = "acct.source_reconciliation"
    else:
        key = "acct.source_system"
    return t(key, "en") if csv_export else t(key)


def fx_line_amounts(debit: float, credit: float, line: dict) -> tuple[float | None, float | None]:
    """A journal line's foreign-currency amounts, or (None, None) when the line
    carries no rate.

    A manually entered foreign line stores the figures the author actually
    typed, and those win: they are what the source document says, and dividing
    the local amount back out can land a cent away from it. Only a
    document-linked line, which stores no foreign amounts of its own, is
    derived from the recorded rate. Without a rate nothing is shown: a guessed
    figure on an audited journal is worse than a blank.
    """
    fx_debit = line.get("fx_debit")
    fx_credit = line.get("fx_credit")
    if fx_debit is not None or fx_credit is not None:
        return (fx_debit, fx_credit)
    rate = line.get("fx_rate") or 0
    fx_currency = line.get("fx_currency")
    # A rate with no currency cannot be formatted: the amount would be rounded
    # at a defaulted precision and shown under a currency nobody recorded.
    if not rate or not fx_currency:
        return (None, None)
    rate_d = to_decimal(rate)
    fx_debit = float(round_money(to_decimal(debit) / rate_d, fx_currency)) if debit else None
    fx_credit = float(round_money(to_decimal(credit) / rate_d, fx_currency)) if credit else None
    return (fx_debit, fx_credit)


def report_header(company: dict, title: str, subtitle: str = "") -> FT:
    """Professional report header for print views."""
    return Div(
        Div(
            H1(company.get("name", ""), cls="report-company-name"),
            P(company.get("address", ""), cls="report-company-address") if company.get("address") else None,
            P(f"Tax ID: {company['tax_id']}", cls="report-company-taxid") if company.get("tax_id") else None,
        ),
        Div(
            H2(title, cls="report-title"),
            P(subtitle, cls="report-subtitle") if subtitle else None,
            P(f"Printed: {_date.today().isoformat()}", cls="report-print-date"),
        ),
        cls="report-header",
    )


def print_shell(company: dict, title: str, subtitle: str, body: FT,
                header: bool = True) -> FT:
    """Minimal printable page: auto-triggers window.print() on load.

    header=False for the statement batch run, where every page carries its own
    report header instead of the document carrying one shared header."""
    return Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(f"{title} - {company.get('name', 'Celerp')}"),
            Style(PRINT_CSS),
        ),
        Body(
            report_header(company, title, subtitle) if header else None,
            body,
            Div(NotStr('Powered by <a href="https://celerp.com">celerp.com</a>'), cls="report-footer"),
            Script("window.onload = function() { window.print(); }"),
        ),
    )


PRINT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Arial', sans-serif; font-size: 10pt; color: #111; background: white; padding: 20mm; }

/* Report header */
.report-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8mm; padding-bottom: 4mm; border-bottom: 2px solid #111; }
.report-company-name { font-size: 14pt; font-weight: 700; margin-bottom: 2mm; }
.report-company-address, .report-company-taxid { font-size: 9pt; color: #555; line-height: 1.4; }
.report-title { font-size: 14pt; font-weight: 700; text-align: right; margin-bottom: 2mm; }
.report-subtitle { font-size: 9pt; color: #555; text-align: right; line-height: 1.4; }
.report-print-date { font-size: 8pt; color: #888; text-align: right; margin-top: 1mm; }

/* Sections */
.report-section { margin-bottom: 6mm; }
.report-section-title { font-size: 10pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: #333; border-bottom: 1px solid #ccc; padding-bottom: 1mm; margin-bottom: 2mm; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
thead th { background: #f5f5f5; font-weight: 700; text-align: center; padding: 1.5mm 2mm; border-bottom: 1px solid #ccc; }
/* A header over a right-aligned column is right-aligned too, matching the screen rule. */
thead th.cell--number, thead th.cell--right { text-align: right; }
tbody td { padding: 1.2mm 2mm; border-bottom: 1px solid #eee; }
td.cell--number, td.cell--right { text-align: right; }
td.cell--muted { color: #888; font-size: 8pt; }
td.cell--mono { font-family: 'Courier New', monospace; font-size: 8.5pt; }

/* Totals */
.section-total { font-weight: 700; text-align: right; padding: 1.5mm 2mm; border-top: 1px solid #999; font-size: 9.5pt; margin-top: 1mm; }
.report-subtotal, .report-total { text-align: right; padding: 2mm 2mm; border-top: 2px solid #333; font-size: 10pt; margin-bottom: 4mm; }
.net-profit--positive { color: #1a7a3c; }
.net-profit--negative { color: #b91c1c; }

/* Balance status */
.valuation-bar { margin-top: 4mm; display: flex; gap: 4mm; font-size: 9pt; }
.val-chip { background: #f5f5f5; padding: 1mm 3mm; border: 1px solid #ccc; border-radius: 2px; }
.val-chip--alert { background: #fef2f2; border-color: #f87171; color: #b91c1c; }

/* Trial balance summary */
.trial-summary { display: flex; gap: 6mm; margin-bottom: 4mm; font-size: 9.5pt; }

/* Page breaks */
.report-section { page-break-inside: avoid; }
/* Statement batch run: one subject per printed page */
.soa-page { break-after: page; }
.soa-page:last-child { break-after: auto; }

.report-footer { position: fixed; bottom: 0; left: 0; right: 0; padding: 2mm 20mm; border-top: 1px solid #ddd; font-size: 8pt; color: #aaa; text-align: center; background: white; }
.report-footer a { color: #aaa; text-decoration: none; }
@page { margin: 0; size: A4 portrait; }
@media print {
  body { padding: 15mm; }
  .report-section { page-break-inside: avoid; }
}
"""
