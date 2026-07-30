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

from urllib.parse import quote_plus

from fasthtml.common import *

from celerp.services.money import EXCHANGE_RATE_DP, to_decimal
from ui.components.report_kit import (
    fname_date, fx_line_amounts, href, je_source_label, period_subtitle,
)
from ui.components.shell import toast_header
from ui.components.table import EMPTY, bulk_toolbar, fmt_money
from ui.i18n import t

# What the journal can be narrowed by: one search box. The API takes this name and
# both books read it back under it, so a filtered page, its print sheet and its
# export cannot end up narrowing to different things.
JOURNAL_FILTER_KEYS = ("q",)

# The rendered table's id, written once. The selection toolbar reads its rows
# through it and an action swaps the table it posted from, so the toolbar, the
# table and the response after a void all have to name the same element.
JOURNAL_TABLE_ID = "journal-table"


def journal_filters(request) -> dict:
    """The filters a request is asking for, blanks dropped.

    A cleared filter field submits empty, which means no filter; dropping it here
    keeps the export URLs and the API call free of parameters that say nothing.
    """
    out = {}
    for key in JOURNAL_FILTER_KEYS:
        value = (request.query_params.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def journal_filter_qs(filters: dict) -> str:
    """The filters as query-string tail, for the date bar to carry through.

    The date bar is its own GET form, so without this the act of changing the
    period would clear the filters the reader had set.
    """
    return "".join(f"&{k}={quote_plus(v)}" for k, v in filters.items() if v)


def journal_filter_words(filters: dict, lang: str | None = None) -> str:
    """The filters in words, for the page and the print sheet to state.

    Both read it from here, so a printed page says exactly what the screen it came
    from said, and neither can be mistaken for the whole book.
    """
    q = filters.get("q")
    if not q:
        return ""
    return f'{t("acct.filtered_totals", lang)}: {t("label.search", lang)} = "{q}"'


def journal_print_subtitle(d_from: str, d_to: str, filters: dict,
                           lang: str | None = None) -> str:
    """A print sheet's subtitle: the period, and what it was narrowed to."""
    words = journal_filter_words(filters, lang)
    period = period_subtitle(d_from, d_to)
    return f"{period}  |  {words}" if words else period


def journal_export_name(stem: str, d_from: str, d_to: str, filters: dict) -> str:
    """An export filename, saying when the file is not the whole book.

    The filter values themselves cannot go in it: a filename lands in a
    Content-Disposition header and raw query text must never reach one, which is
    what `fname_date` exists for. A fixed suffix carries the fact instead, and the
    header row stays the single row a spreadsheet reads as column names.
    """
    tail = "_filtered" if filters else ""
    return f"{stem}_{fname_date(d_from)}_{fname_date(d_to)}{tail}.csv"


def journal_filter_bar(base_url: str, filters: dict, carried: dict,
                       lang: str | None = None) -> FT:
    """The journal's one search box, as a GET form beside the date bar.

    One box, not three: account, figure and free text were three fields that ANDed
    together, which is three ways to ask one question. Now a comma separates terms
    that OR together and each term is tried against all three, the way the inventory
    search box reads a run of scanned codes. Inventory appends a comma on Enter so a
    barcode scanner can rattle off codes without submitting; there is no scanner here,
    so a comma is typed by hand and Enter applies the search like any other form.

    A GET form, so the search lives in the URL and a filtered journal can be linked,
    bookmarked and reloaded (GDR 2m). It carries the period as hidden fields for the
    same reason the date bar carries its non-date params: a GET form submits its own
    fields and nothing else, so applying a search would otherwise reset the period.
    """
    # Escape clears the box; Enter submits the form (applies the search) natively,
    # since there is no scanner here that needs Enter to chain terms with a comma.
    keydown = (
        "if(event.key==='Escape'){this.value='';this.blur();event.preventDefault();}"
    )
    controls: list = [
        Input(type="hidden", name=k, value=v) for k, v in carried.items() if v
    ]
    controls += [
        Input(type="search", name="q", value=filters.get("q", ""),
              placeholder=t("acct.journal_search", lang),
              title=t("acct.journal_search_hint", lang),
              cls="form-input form-input--sm journal-search", onkeydown=keydown),
        Button(t("btn.apply", lang), type="submit", cls="btn btn--secondary btn--sm"),
    ]
    if filters:
        controls.append(A(t("acct.filter_clear", lang), href=href(base_url, carried),
                          cls="btn btn--ghost btn--sm"))
    return Form(*controls, action=base_url, method="get",
                cls="journal-filter-bar flex-row gap-sm mb-md")


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
            "items_status": entry.get("items_status"),
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


def _row(cells: list, row: dict, *, data_row: bool = False) -> FT:
    """A table row, dimmed when the entry behind it was voided.

    The class attribute is written only when there is a class, so an ordinary row
    renders exactly as it did before this was shared. `data_row` marks the rows a
    selection counts: one per entry, never one per posting.
    """
    classes = " ".join(c for c in ("data-row" if data_row else "",
                                   "payment-voided" if _voided(row) else "") if c)
    return Tr(*cells, cls=classes) if classes else Tr(*cells)


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


def _select_cell(row: dict, selectable: bool) -> FT:
    """The selection box for one entry, or the empty cell under it.

    A selection is of entries, not of postings: a void acts on an entry, and one
    entry can hold a dozen postings. So the box sits on the entry's first row and
    the rows beneath it keep the column and leave it empty, which is also what
    makes the count read entries. A voided entry has nothing left to void, so it
    keeps the column and leaves it empty too.
    """
    if not selectable or row.get("status") != "posted":
        return Td("", cls="col-checkbox")
    return Td(Input(type="checkbox", cls="bulk-select", name="selected",
                    value=row.get("je_id", "")), cls="col-checkbox")


def journal_bulk_toolbar(params: dict, carried: dict, *, items: bool = False) -> FT:
    """The selection toolbar over a journal: void what is ticked, with a reason.

    The reason is a field in the bar rather than a popup, so voiding a batch is
    entered on the page like every other routine entry (GDR 2f). It stays out of
    the way until it is wanted: picking "void" from the action list reveals the
    optional reason below the bar with its own confirm button, rather than firing
    the void the instant the action is chosen. The confirm step states how many
    entries are about to be struck, because a mis-ticked box in a long journal is
    the mistake worth catching before the write, not after: a void stays on the
    record and the way back from a wrong one is another entry.

    The period and the filters travel in the URL so the answer can re-read exactly
    the journal the reader was looking at, and the preset travels with them so the
    way back to the whole book still moves with the year rather than freezing into
    the dates the preset happens to mean today. `items` says which of the two books
    is asking, because that is what the answer has to render.
    """
    esc = ("if(event.key==='Escape'){this.value='';this.blur();"
           "event.preventDefault();}")
    post_url = href("/accounting/journal/bulk-void", {
        **params,
        **({"preset": carried["preset"]} if carried.get("preset") else {}),
        **({"items": "1"} if items else {}),
    })
    return bulk_toolbar(
        JOURNAL_TABLE_ID,
        [{"value": "void", "label": t("acct.bulk_void"), "method": "post",
          "url": post_url, "confirm": t("acct.bulk_void_confirm"), "fields": True}],
        fields=[Input(type="text", name="reason",
                      cls="form-input form-input--sm bulk-field bulk-field--reason",
                      placeholder=t("acct.void_reason_optional"), onkeydown=esc)],
    )


def journal_void_toast(result: dict) -> dict:
    """The toast header to send after a void, read from the API's own answer.

    Both void paths report through here, so one entry and forty are described the
    same way. Refusals are stated with the count and the API's own words for why,
    and the refreshed table below says which entries they were: those are the ones
    still standing unstruck. The whole thing is an error the moment anything was
    refused, so a batch that half worked cannot be read as a batch that worked.

    What was voided is counted from the per-entry answers rather than read from the
    count beside them, so the sentence and the rows it describes come from the same
    facts.
    """
    results = result.get("results") or []
    refused = [r for r in results if r.get("status") != "void"]
    message = t("acct.bulk_void_result", n=len(results) - len(refused))
    if refused:
        reasons = " ".join(dict.fromkeys(
            str(r.get("detail") or "").strip() for r in refused if r.get("detail")))
        message = f"{message} {t('acct.bulk_void_refused', n=len(refused), reasons=reasons)}"
    return toast_header(message.strip(), "error" if refused else "success")


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


# The two facts that leave a posting without item detail a reader can act on. A
# `no_items` posting (a payment, a fulfilment cost movement, a manual entry) simply
# never had items and gets no marker: an entry that never had items is not one whose
# items were withheld. `expanded` shows its items on the line.
_WITHHELD_NOTES = {
    "untied": "acct.items_untied",
    "no_document": "acct.items_no_document",
}


def _item_or_note(row: dict) -> FT:
    """The item this posting was for, or a marked `--` saying why it is not shown.

    A posting with an item names it. One without either never had an item, in which
    case the cell is a plain `--`, or its item detail was withheld for a reason the
    reader can act on, in which case the `--` carries that reason as a tooltip on the
    row itself rather than in a note piled at the top of the page.
    """
    item = row.get("item")
    if item:
        return item
    note = _WITHHELD_NOTES.get(row.get("items_status") or "")
    if note:
        return Span(EMPTY, cls="item-note", title=t(note))
    return EMPTY


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
        Td(_item_or_note(row)),
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


def _headers(*, items: bool, has_fx: bool, select: bool) -> list:
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
    if select:
        headers.insert(0, Th(Input(type="checkbox", cls="bulk-select-all",
                                   title=t("label.select_all")), cls="col-checkbox"))
    return headers


def journal_table(rows: list[dict], *, items: bool, currency: str | None = None,
                  params: dict | None = None,
                  filtered: bool = False, select: bool = False) -> FT:
    """The journal as a table, in whichever of its two shapes the caller asked for.

    `params` is the view the reader is looking at, carried into the selection
    toolbar so an action taken on a selection returns them to the same view.
    `select` adds the selection column the bulk toolbar reads, which the print
    sheets do without: there is nothing to tick on paper. `filtered` is what the
    payload said about itself, so an empty answer says which kind of empty it is: a
    period with no entries in it, or filters that matched none of the entries there
    are.
    """
    if not rows:
        return P(t("acct.no_matches") if filtered else t("acct.no_journal_entries"),
                 cls="empty-state")
    # The foreign columns appear only when the period actually holds a foreign
    # transaction, so a single-currency journal stays as narrow as it has always been.
    has_fx = any(r.get("fx_currency") for r in rows)
    params = params or {}

    body = []
    seen_entries: set[str] = set()
    for row in rows:
        # In the extended journal every row is a posting, so the entry's first row
        # stands in for it and carries the box; in the classical journal the entry
        # has a heading row of its own.
        first_of_entry = row.get("je_id", "") not in seen_entries
        seen_entries.add(row.get("je_id", ""))
        if items:
            cells = _item_cells(row, currency, has_fx)
            if select:
                cells.insert(0, _select_cell(row, first_of_entry))
            body.append(_row(cells, row, data_row=select and first_of_entry))
            continue
        if row["kind"] == "entry":
            cells = _entry_cells(row, has_fx)
            if select:
                cells.insert(0, _select_cell(row, True))
            body.append(_row(cells, row, data_row=select))
            continue
        cells = _line_cells(row, currency, has_fx)
        if select:
            cells.insert(0, _select_cell(row, False))
        body.append(_row(cells, row))

    return Table(
        Thead(Tr(*_headers(items=items, has_fx=has_fx, select=select))),
        Tbody(*body),
        cls="data-table", id=JOURNAL_TABLE_ID,
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
