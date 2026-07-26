# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The journal, rendered once for both books that read it.

The classical journal under /accounting and the extended journal under /reports are
two books of account and stay two: the first has one row per posting and ties line
for line to the ledger, the second splits a posting into a row per item sold and
cannot split a payment at all. What they share is everything around the rows, and it
used to be written twice: whether the foreign columns appear, how a money cell is
formatted, how the source document is linked, how a voided entry is styled, and what
the CSV export carries. That lives here, and the two shapes are the only thing the
callers still choose between.
"""

from __future__ import annotations

from fasthtml.common import *

from celerp.services.money import EXCHANGE_RATE_DP, to_decimal
from ui.components.report_kit import fx_line_amounts, je_source_label
from ui.components.table import EMPTY, fmt_money
from ui.i18n import t


def journal_rows(data: dict, *, items: bool, newest_first: bool = True) -> list[dict]:
    """A journal payload flattened to render-ready rows.

    With `items=False` an entry row of kind `entry` is followed by its postings of
    kind `line`, indented under it, which is how a book of account reads. With
    `items=True` every posting stands on its own row of kind `line`, carrying the
    date, source and memo of the entry it came from, because a report meant to be
    pulled apart by item cannot rely on a heading row above it.

    Entries come newest first, the order every journal screen shows them in;
    `newest_first=False` keeps the order the API returned, which is what the
    classical CSV export replays. Postings keep the order they were posted in either
    way: a reversed entry is still read top to bottom.

    Every row carries the entry's `status` and its source both ways: `source` for the
    screen, in the reader's language, and `source_csv` in English, so an export is
    the same file whoever downloads it.
    """
    out: list[dict] = []
    entries = list(data.get("entries", []))
    for entry in reversed(entries) if newest_first else entries:
        source = entry.get("source_doc") or {}
        shared = {
            "ts": str(entry.get("ts") or "")[:10],
            "je_id": entry.get("je_id", ""),
            "doc_id": source.get("doc_id", ""),
            "source": source.get("doc_ref") or source.get("doc_id") or je_source_label(entry),
            "source_csv": (source.get("doc_ref") or source.get("doc_id")
                           or je_source_label(entry, csv_export=True)),
            "memo": entry.get("memo", ""),
            "status": entry.get("status"),
        }
        if not items:
            out.append({
                **shared,
                "kind": "entry",
                "void_reason": str(entry.get("void_reason") or entry.get("reason") or "").strip(),
            })
        for line in entry.get("lines", []):
            out.append({**line, **shared, "kind": "line"})
    return out


def _voided(row: dict) -> bool:
    return row.get("status") == "void"


def _row(cells: list, row: dict) -> FT:
    """A table row, dimmed when the entry behind it was voided.

    The class attribute is written only when there is a class, so an ordinary row
    renders exactly as it did before this was shared.
    """
    return Tr(*cells, cls="payment-voided") if _voided(row) else Tr(*cells)


def _source_cell(row: dict):
    """The source document, linked when the books still hold it."""
    if row.get("doc_id"):
        return A(row["source"], href=f"/docs/{row['doc_id']}", cls="drilldown-link")
    return row["source"]


def _money(val, currency: str | None) -> str:
    """A money cell, blank when that side of the posting is unused.

    A journal is read down one money column at a time, so the unused side is left
    empty rather than marked; the general ledger and the trial balance leave it the
    same way.
    """
    return fmt_money(val, currency) if val else ""


def _attr(val, currency: str | None) -> str:
    """An item attribute cell, `--` when the item has no such figure.

    An attribute is not a posting: a row with no unit price has none to show, and
    saying so beats a blank cell that reads as one that failed to load.
    """
    return fmt_money(val, currency) if val else EMPTY


def fmt_exchange_rate(rate) -> str:
    """An exchange rate is a ratio, not an amount: no currency symbol, and trailing
    zeros trimmed so 35 reads as 35 and 0.00001117318 keeps every digit the author
    typed."""
    try:
        s = f"{float(rate):,.{EXCHANGE_RATE_DP}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""
    return s or "0"


def _void_control(row: dict, params: dict) -> FT:
    """The void control for one entry, carrying the view it was pressed from.

    Auto-posted entries keep the control and are refused by the server with an
    explanation naming the source document. Removing the button instead would leave
    the reader guessing why an entry cannot be voided (GDR 2e: validate at the
    function level, never restrict the interface).
    """
    if row.get("status") != "posted":
        return Td("")
    return Td(Details(
        Summary(t("btn.void"), cls="btn btn--danger btn--sm"),
        Form(
            Input(type="text", name="reason", placeholder=t("acct.void_reason_optional"),
                  cls="form-input form-input--sm",
                  onkeydown="if(event.key==='Escape'){this.closest('details').removeAttribute('open');event.preventDefault();}"),
            *[Input(type="hidden", name=k, value=v) for k, v in params.items()],
            Button(t("btn.confirm_void"), type="submit", cls="btn btn--danger btn--sm",
                   style="margin-top:0.5rem;"),
            hx_post=f"/accounting/journal/{row.get('je_id', '')}/void",
            hx_swap="none", cls="inline-form",
        ),
        cls="void-section",
    ))


def _entry_cells(row: dict, has_fx: bool) -> list:
    """The heading row of an entry: what it is, with the figures left to its lines."""
    memo_bits: list = [Span(row.get("memo", ""))] if row.get("memo") else []
    if _voided(row):
        reason = row.get("void_reason") or ""
        memo_bits.append(Span(f"{t('doc.voided')}: {reason}" if reason else t("doc.voided"),
                              cls="badge badge--void"))
    cells = [
        Td(row["ts"], cls="cell--mono"),
        Td(_source_cell(row)),
        Td(*memo_bits, cls="cell--muted"),
        Td("", cls="cell--number"),
        Td("", cls="cell--number"),
    ]
    if has_fx:
        # The currency and rate belong to the line, not the entry: one entry may
        # settle an invoice in one currency with cash in another. The entry row
        # leaves the foreign columns empty and each line states its own.
        cells += [
            Td("", cls="cell--number"),
            Td("", cls="cell--number"),
            Td("", cls="cell--number"),
        ]
    return cells


def _line_cells(row: dict, currency: str | None, has_fx: bool) -> list:
    """One posting under its entry heading: the account, and the amount."""
    debit = float(row.get("debit") or 0)
    credit = float(row.get("credit") or 0)
    cells = [
        Td(""),
        Td(""),
        Td(f"{row.get('account', '')} {row.get('name', '')}".strip(),
           style="padding-left:2rem"),
        Td(_money(debit, currency), cls="cell--number"),
        Td(_money(credit, currency), cls="cell--number"),
    ]
    if has_fx:
        fx_debit, fx_credit = fx_line_amounts(debit, credit, row)
        fx_currency = row.get("fx_currency")
        # A foreign column with nothing in it reads `--` here, never blank, so a
        # base-currency line inside a mixed entry is visibly a line with no foreign
        # amount rather than one that failed to load. The extended journal leaves the
        # same cell blank: there every row is a posting of its own, so an empty
        # foreign column is read as the column being unused, not as a gap in a row.
        cells += [
            Td(fmt_money(fx_debit, fx_currency) if fx_debit else EMPTY, cls="cell--number"),
            Td(fmt_money(fx_credit, fx_currency) if fx_credit else EMPTY, cls="cell--number"),
            Td(fmt_exchange_rate(row.get("fx_rate")) if row.get("fx_rate") else EMPTY,
               cls="cell--number cell--mono"),
        ]
    return cells


def _item_cells(row: dict, currency: str | None, has_fx: bool) -> list:
    """One posting standing on its own, with what was sold on it."""
    debit = float(row.get("debit") or 0)
    credit = float(row.get("credit") or 0)
    fx_debit, fx_credit = fx_line_amounts(debit, credit, row)
    fx_currency = row.get("fx_currency")
    qty = row.get("quantity")
    cells = [
        Td(row["ts"], cls="cell--mono"),
        Td(_source_cell(row)),
        Td(row.get("item") or EMPTY),
        Td(f"{row.get('account', '')} {row.get('name', '')}".strip()),
        Td(row.get("memo", ""), cls="cell--muted"),
        Td(fx_currency or currency or EMPTY, cls="cell--mono"),
        Td(f"{to_decimal(qty).normalize():f}" if qty else EMPTY, cls="cell--number"),
        Td(_attr(row.get("unit_price"), fx_currency or currency), cls="cell--number"),
        Td(_money(debit, currency), cls="cell--number"),
        Td(_money(credit, currency), cls="cell--number"),
    ]
    if has_fx:
        cells += [
            Td(_money(fx_debit, fx_currency), cls="cell--number"),
            Td(_money(fx_credit, fx_currency), cls="cell--number"),
        ]
    return cells


def _headers(*, items: bool, has_fx: bool, void_action: bool) -> list:
    """The header row. Headers over figures are right-aligned, above the digits they
    name (HTML/CSS 4a)."""
    if items:
        headers = [
            Th(t("th.date")),
            Th(t("th.source")),
            Th(t("th.item")),
            Th(t("th.account")),
            Th(t("th.description")),
            Th(t("th.currency")),
            Th(t("th.quantity"), cls="cell--number"),
            Th(t("th.unit_price"), cls="cell--number"),
            Th(t("th.debit"), cls="cell--number"),
            Th(t("th.credit"), cls="cell--number"),
        ]
    else:
        headers = [
            Th(t("th.date")),
            Th(t("th.source")),
            Th(t("th.description")),
            Th(t("th.debit"), cls="cell--number"),
            Th(t("th.credit"), cls="cell--number"),
        ]
    if has_fx:
        headers += [Th(t("th.fx_debit"), cls="cell--number"),
                    Th(t("th.fx_credit"), cls="cell--number")]
        if not items:
            # The classical journal states the rate it posted at. The extended
            # journal does not: its rows are split by item, and a rate repeated down
            # every split of one posting reads as several rates.
            headers.append(Th(t("th.rate"), cls="cell--number"))
    if void_action:
        headers.append(Th(""))
    return headers


def journal_table(rows: list[dict], *, items: bool, currency: str | None = None,
                  params: dict | None = None, void_action: bool = False) -> FT:
    """The journal as a table, in whichever of its two shapes the caller asked for.

    `params` is the view the reader is looking at, carried into the void form so
    voiding an entry returns them to the same view. `void_action` adds the column
    holding that control, which the print sheet and the extended journal do without.
    """
    if not rows:
        return P(t("acct.no_journal_entries"), cls="empty-state")
    # The foreign columns appear only when the period actually holds a foreign
    # transaction, so a single-currency journal stays as narrow as it has always been.
    has_fx = any(r.get("fx_currency") for r in rows)
    params = params or {}

    body = []
    for row in rows:
        if items:
            body.append(_row(_item_cells(row, currency, has_fx), row))
            continue
        if row["kind"] == "entry":
            cells = _entry_cells(row, has_fx)
            if void_action:
                cells.append(_void_control(row, params))
        else:
            cells = _line_cells(row, currency, has_fx)
            if void_action:
                cells.append(Td(""))
        body.append(_row(cells, row))

    return Table(
        Thead(Tr(*_headers(items=items, has_fx=has_fx, void_action=void_action))),
        Tbody(*body),
        cls="data-table",
    )


def journal_csv_rows(data: dict, *, items: bool, currency: str = "") -> list[list]:
    """The journal as CSV rows, header first.

    The two exports carry different columns, because they are exports of two
    different books, but every figure in them is derived here so an exported column
    cannot drift from the rendered one it came from. A zero foreign amount is not a
    figure anybody typed, so it exports as a blank cell with no currency claimed for
    it and no rate.
    """
    if items:
        out = [["date", "entry_id", "source_ref", "item", "account_code", "account_name",
                "memo", "quantity", "unit_price", "debit", "credit", "fx_currency",
                "fx_debit", "fx_credit", "status"]]
    else:
        out = [["date", "entry_id", "source_ref", "memo", "account_code", "account_name",
                "debit", "credit", "currency", "fx_currency", "fx_debit", "fx_credit",
                "exchange_rate", "status"]]
    # The extended journal exports newest first, as its screen shows it: it is read
    # as the report it came from. The classical journal exports in the order the API
    # returns, oldest first, because an auditor replays a book of account forwards.
    rows = [r for r in journal_rows(data, items=items, newest_first=items)
            if r["kind"] == "line"]
    for row in rows:
        debit = float(row.get("debit") or 0)
        credit = float(row.get("credit") or 0)
        fx_d, fx_c = fx_line_amounts(debit, credit, row)
        fx_debit = fx_d if fx_d else ""
        fx_credit = fx_c if fx_c else ""
        has_amount = fx_debit != "" or fx_credit != ""
        fx_currency = (row.get("fx_currency") or "") if has_amount else ""
        if items:
            out.append([
                row["ts"], row.get("je_id", ""), row["source_csv"], row.get("item") or "",
                row.get("account", ""), row.get("name", ""), row.get("memo", ""),
                row.get("quantity") if row.get("quantity") else "",
                row.get("unit_price") if row.get("unit_price") else "",
                debit, credit, fx_currency, fx_debit, fx_credit, row.get("status") or "",
            ])
        else:
            rate = (row.get("fx_rate") or 0) if has_amount else ""
            out.append([
                row["ts"], row.get("je_id", ""), row["source_csv"], row.get("memo", ""),
                row.get("account", ""), row.get("name", ""), debit, credit, currency,
                fx_currency, fx_debit, fx_credit, rate or "", row.get("status") or "",
            ])
    return out
