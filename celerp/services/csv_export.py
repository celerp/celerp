"""Shared pieces of the CSV exports: the exported column set and the row stream.

Every export takes the same ``cols`` query parameter the screens send, so a user exports exactly
the columns they see, in the order they see them. The column set is closed per export: an unknown
name is rejected with a message naming it rather than silently dropped."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable

from fastapi import HTTPException


def resolve_export_cols(cols: str | None, default: list[str], allowed: Iterable[str]) -> list[str]:
    """The columns an export emits: ``cols`` (comma separated, screen order) when given, else
    ``default``. Names outside ``allowed`` raise 422 naming them."""
    if not cols:
        return list(default)
    wanted = [c.strip() for c in cols.split(",") if c.strip()]
    if not wanted:
        return list(default)
    allowed_set = set(allowed)
    unknown = [c for c in wanted if c not in allowed_set]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown export column(s): {', '.join(unknown)}. Choose from: {', '.join(sorted(allowed_set))}",
        )
    return wanted


def csv_safe(cell):
    """Neutralize spreadsheet formula injection: user-authored strings (memos, account and
    contact names) must never execute when the export is opened in a spreadsheet, so string
    cells starting with a formula trigger character get a leading single quote."""
    if isinstance(cell, str) and cell and cell[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + cell
    return cell


def csv_line(cols: list[str], row: dict | None = None) -> str:
    """One CSV line: the header when ``row`` is None, else ``row`` restricted to ``cols``.
    Every cell goes through `csv_safe`."""
    cells = cols if row is None else ["" if row.get(c) is None else row.get(c) for c in cols]
    buf = io.StringIO()
    csv.writer(buf).writerow([csv_safe(c) for c in cells])
    return buf.getvalue()


async def csv_stream(cols: list[str], rows: Iterable[dict] | AsyncIterator[dict]) -> AsyncIterator[str]:
    """Header then one line per row, never buffering the whole export."""
    yield csv_line(cols)
    if hasattr(rows, "__aiter__"):
        async for row in rows:
            yield csv_line(cols, row)
    else:
        for row in rows:
            yield csv_line(cols, row)
