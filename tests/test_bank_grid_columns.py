# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The bank page's grids: every heading row lays out on the same columns as the rows under
it, and every cell under a heading carries that heading, so a phone, where the cells stack,
still says what each value is."""

from __future__ import annotations

import re

from fasthtml.common import to_xml

from ui.components import bank_groups as BG

_LINE = {"id": "L1", "status": "posted", "amount": "-50.00", "line_date": "2026-08-01", "description": "Fee",
         "raw_balance": None, "suggestion": None, "client_query": None, "attachments": [],
         "posted_to": [{"code": "6100", "name": "Bank fees"}]}
_TABLE = {"groups": [{"key": "FEE", "count": 1, "total": "-50.00", "suggestion": None, "lines": [_LINE]}]}
_PREVIEW = {"statements": [{"header": {"currency": "USD"}, "tie": {"ok": True},
                            "lines": [{"line_date": "2026-08-01", "description": "Fee", "amount": "-50.00",
                                       "raw_balance": "950.00"}]}]}


def _rows(html: str) -> list[tuple[str, list[tuple[str | None, str]]]]:
    """Each grid row as (its classes, [(cell data-label or None, cell text)])."""
    return [(m.group(1), _top_cells(m.group(2)))
            for m in re.finditer(r'<div\b[^>]*\bclass="(bank-row [^"]*)"[^>]*>(.*?)</div>', html, re.S)]


def _top_cells(inner: str) -> list[tuple[str | None, str]]:
    cells, depth = [], 0
    for tag in re.finditer(r'<(/?)(span|input|button)\b([^>]*)>', inner):
        closing, name, attrs = tag.group(1), tag.group(2), tag.group(3)
        if name == "input" and depth == 0:
            cells.append((None, ""))
        elif name == "span" and not closing:
            if depth == 0:
                label = re.search(r'data-label="([^"]*)"', attrs)
                cells.append((label.group(1) if label else None, ""))
                start = tag.end()
            depth += 1
        elif name == "span" and closing:
            depth -= 1
            if depth == 0:
                cells[-1] = (cells[-1][0], re.sub(r"<[^>]+>", "", inner[start:tag.start()]).strip())
    return cells


def _grids(html: str) -> dict[str, dict]:
    grids: dict[str, dict] = {}
    for cls, cells in _rows(html):
        grid = next((c for c in cls.split() if c in ("bank-row--group", "bank-row--line", "bank-row--preview")), None)
        assert grid, f"a bank row with no column layout: {cls}"
        g = grids.setdefault(grid, {"head": None, "rows": []})
        if "bank-row--head" in cls:
            g["head"] = [text for _, text in cells]
        else:
            g["rows"].append(cells)
    return grids


def _check(html: str, want: set[str]) -> None:
    grids = _grids(html)
    assert set(grids) == want, grids.keys()
    for name, g in grids.items():
        assert g["head"] is not None, f"{name} has no heading row"
        for row in g["rows"]:
            assert len(row) == len(g["head"]), (name, g["head"], row)
            for head, (label, _) in zip(g["head"], row):
                assert label == (head or None), (name, head, label)


def test_the_group_table_headings_sit_over_their_columns_and_label_each_cell():
    _check(to_xml(BG.group_table(_TABLE, "B1", "USD")), {"bank-row--group", "bank-row--line"})


def test_the_statement_preview_has_headings_that_label_each_cell():
    _check(to_xml(BG.statement_preview("B1", "J1", _PREVIEW)), {"bank-row--preview"})


def test_a_posted_line_says_which_accounts_it_was_posted_to():
    html = to_xml(BG.line_row(_LINE, "B1", "USD"))
    assert "Posted to 6100 Bank fees" in html
    assert "Posted to" not in to_xml(BG.line_row({**_LINE, "posted_to": []}, "B1", "USD"))
