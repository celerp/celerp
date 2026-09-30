# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tabular reader: CSV parity with the old UI importer plus xlsx parsing."""
from __future__ import annotations

import io
import re
import zipfile

import openpyxl
import pytest

from celerp.importers import tabular


def _xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    """Build an .xlsx workbook from {sheet_name: [[row cells], ...]}."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(title=name)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_csv_parity_bom_quoting_and_identifier_strings():
    """A leading BOM is stripped from the first header, quoted fields keep their
    embedded commas, and identifier-like values stay strings (never coerced)."""
    text = '﻿sku,name,note\r\n007,"Acme, Inc",hi\r\n'
    cols, rows = tabular.read_csv(text)
    assert cols == ["sku", "name", "note"]
    assert rows == [{"sku": "007", "name": "Acme, Inc", "note": "hi"}]


def test_xlsx_single_sheet_autoselect():
    data = _xlsx_bytes({"Only": [["sku", "qty"], ["A1", 3], ["B2", 4]]})
    cols, rows = tabular.read_xlsx(data, sheet=None)
    assert cols == ["sku", "qty"]
    assert rows == [{"sku": "A1", "qty": "3"}, {"sku": "B2", "qty": "4"}]


def test_xlsx_multiple_sheets_requires_choice():
    data = _xlsx_bytes({
        "First": [["sku"], ["A1"]],
        "Second": [["name"], ["Widget"]],
    })
    with pytest.raises(tabular.TabularError) as excinfo:
        tabular.read_xlsx(data, sheet=None)
    assert excinfo.value.sheets == ["First", "Second"]

    cols, rows = tabular.read_xlsx(data, sheet="Second")
    assert cols == ["name"]
    assert rows == [{"name": "Widget"}]


def test_xlsx_formula_cell_rejected_with_position():
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Calc"
    worksheet.append(["sku", "total"])
    worksheet["A2"] = "Widget"
    worksheet["B2"] = "=A2&A2"
    buffer = io.BytesIO()
    workbook.save(buffer)

    with pytest.raises(tabular.TabularError) as excinfo:
        tabular.read_xlsx(buffer.getvalue(), sheet=None)
    assert excinfo.value.row == 2
    assert excinfo.value.column == "B"


def test_xlsm_xls_rejected():
    data = _xlsx_bytes({"Only": [["sku"], ["A1"]]})
    for filename in ("book.xlsm", "book.xls"):
        with pytest.raises(tabular.TabularError):
            tabular.read_table(data, filename)


def test_zip_bomb_rejected_before_parse():
    """A workbook whose entries expand past the uncompressed cap is rejected by
    the pre-check, never handed to openpyxl."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"\x00" * (tabular.MAX_XLSX_UNCOMPRESSED + 1))
    data = buffer.getvalue()
    assert len(data) < tabular.MAX_TABLE_BYTES

    with pytest.raises(tabular.TabularError) as excinfo:
        tabular.read_xlsx(data, sheet=None)
    assert "uncompressed" in str(excinfo.value).lower()


@pytest.mark.parametrize("csv_text,cells", [
    ("Title,sell_by,quantity,Notes\nRuby,piece,1\n",
     [["Title", "sell_by", "quantity", "Notes"], ["Ruby", "piece", 1]]),
    ("Title,sell_by,quantity\nRuby,piece,1\n\nOpal,piece,2\n",
     [["Title", "sell_by", "quantity"], ["Ruby", "piece", 1], [None, None, None], ["Opal", "piece", 2]]),
    ("Title,sell_by,quantity\nRuby,piece,1\n,,\nOpal,piece,2\n",
     [["Title", "sell_by", "quantity"], ["Ruby", "piece", 1], [None, None, None], ["Opal", "piece", 2]]),
], ids=["short_row", "blank_row", "empty_cells_row"])
def test_ragged_rows_normalize_the_same_for_csv_and_xlsx(csv_text, cells):
    from_csv = tabular.read_table(csv_text.encode(), "items.csv")
    from_xlsx = tabular.read_table(_xlsx_bytes({"Sheet": cells}), "items.xlsx")
    assert from_csv == from_xlsx


def _both_formats(cells: list[list]) -> list[tuple[str, bytes]]:
    text = "\n".join(",".join("" if c is None else str(c) for c in row) for row in cells) + "\n"
    return [("items.csv", text.encode()), ("items.xlsx", _xlsx_bytes({"Sheet": cells}))]


@pytest.mark.parametrize("cells,code,column", [
    ([["sku", "name", "sku"], ["A1", "Ruby", "B2"]], "duplicate_header", "C"),
    ([["sku", "name", "sku"], ["A1", "Ruby", None]], "duplicate_header", "C"),
    ([["sku", None, "name", None], ["A1", "x", "Ruby", "y"]], "no_header", "B"),
    ([["sku", None, None, "name"], ["A1", None, "y", "Ruby"]], "no_header", "C"),
    ([["sku", "name"], ["A1", "Ruby", "EXTRA", "MORE"]], "extra_columns", "C"),
    ([["sku", "name"], ["A1", "Ruby", "EXTRA"]], "extra_columns", "C"),
], ids=["duplicate_named", "duplicate_named_empty", "two_blank_headers_with_values",
        "blank_header_with_values", "two_columns_past_header", "one_column_past_header"])
def test_a_value_that_a_row_would_lose_is_refused_in_both_formats(cells, code, column):
    """Rows are keyed by header, so a value under a repeated or blank header
    would be overwritten or unnamed. The reader refuses the file, naming the
    column, before any row is built, identically for CSV and workbook."""
    for filename, data in _both_formats(cells):
        with pytest.raises(tabular.TabularError) as err:
            tabular.read_table(data, filename)
        assert (err.value.code, err.value.column) == (code, column), filename


def test_blank_header_over_an_empty_column_still_reads():
    """A blank spacer column loses nothing, so the reader keeps it."""
    for filename, data in _both_formats([["sku", None, "name"], ["A1", None, "Ruby"]]):
        cols, rows = tabular.read_table(data, filename)
        assert cols == ["sku", "", "name"] and rows == [{"sku": "A1", "": "", "name": "Ruby"}]


def _understate_dimension(data: bytes) -> bytes:
    """Rewrite each sheet's stored dimension to A1:A1, as a hand-built or
    third-party file may carry, while its rows stay wide."""
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename.startswith("xl/worksheets/"):
                body = re.sub(rb'<dimension ref="[^"]*"\s*/>', b'<dimension ref="A1:A1"/>', body)
            target.writestr(info, body)
    return out.getvalue()


def test_xlsx_reads_every_cell_whatever_the_stored_dimension_says():
    """A value past the header is refused, never dropped, even when the
    workbook's stored dimension claims the sheet is one column wide."""
    data = _understate_dimension(_xlsx_bytes({"Sheet": [["sku"], ["A1", "EXTRA"]]}))
    with pytest.raises(tabular.TabularError) as err:
        tabular.read_table(data, "items.xlsx")
    assert (err.value.code, err.value.column) == ("extra_columns", "B")


def test_xlsx_cell_bound_counts_the_width_of_rows_wider_than_the_header():
    """A one-column header over wide rows is bounded by the cells actually read,
    as CSV is, not by the header's width or the stored dimension."""
    n_cols, n_rows = 250, 900
    assert n_cols * n_rows > tabular.MAX_CELLS and n_rows <= tabular.MAX_ROWS
    cells = [["sku"]] + [["v"] * n_cols for _ in range(n_rows)]
    csv_file, (xlsx_name, xlsx_data) = _both_formats(cells)
    for filename, data in (csv_file, (xlsx_name, _understate_dimension(xlsx_data))):
        with pytest.raises(tabular.TabularError) as err:
            tabular.read_table(data, filename)
        assert "cells" in str(err.value).lower(), filename


class _Chunked:
    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)
        self.reads: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        chunk = self._data.read(size)
        self.reads.append(len(chunk))
        return chunk


@pytest.mark.asyncio
async def test_upload_over_the_byte_limit_is_refused_before_it_is_read_whole():
    """A one-cell CSV bigger than the byte limit passes the row and cell bounds,
    so the upload read itself is bounded: it stops at the first chunk past the
    limit and never holds the whole file."""
    upload = _Chunked(b"note\n" + b"x" * (tabular.MAX_TABLE_BYTES + 5 * 1024 * 1024))
    with pytest.raises(tabular.TabularError) as err:
        await tabular.read_upload_bytes(upload)
    assert "too large" in str(err.value).lower()
    assert sum(upload.reads) <= tabular.MAX_TABLE_BYTES + 1024 * 1024
    with pytest.raises(tabular.TabularError):
        tabular.read_table(b"note\n" + b"x" * tabular.MAX_TABLE_BYTES, "items.csv")


@pytest.mark.asyncio
async def test_upload_within_the_limit_reads_whole():
    data = b"sku\n" + b"A1\n" * 1000
    assert await tabular.read_upload_bytes(_Chunked(data)) == data


def test_row_and_cell_bounds():
    too_many_rows = "col\n" + "x\n" * (tabular.MAX_ROWS + 1)
    with pytest.raises(tabular.TabularError) as rows_err:
        tabular.read_csv(too_many_rows)
    assert "rows" in str(rows_err.value).lower()

    n_cols = 210
    n_rows = 1000  # under MAX_ROWS, but n_cols * n_rows exceeds MAX_CELLS
    assert n_rows <= tabular.MAX_ROWS
    assert n_cols * n_rows > tabular.MAX_CELLS
    header = ",".join(f"c{i}" for i in range(n_cols))
    row = ",".join("v" for _ in range(n_cols))
    too_many_cells = header + "\n" + "\n".join(row for _ in range(n_rows)) + "\n"
    with pytest.raises(tabular.TabularError) as cells_err:
        tabular.read_csv(too_many_cells)
    assert "cells" in str(cells_err.value).lower()


def test_number_and_date_stringification():
    import datetime

    data = _xlsx_bytes({"Nums": [
        ["int", "float", "whole", "date", "stamp"],
        [5, 2.5, 3.0, datetime.date(2026, 1, 2), datetime.datetime(2026, 3, 4, 9, 30, 0)],
    ]})
    cols, rows = tabular.read_xlsx(data, sheet=None)
    assert rows == [{
        "int": "5",
        "float": "2.5",
        "whole": "3",
        "date": "2026-01-02",
        "stamp": "2026-03-04T09:30:00",
    }]
