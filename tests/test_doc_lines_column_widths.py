"""Every column a document's line table renders has an explicit width in app.css.

The line table is table-layout:fixed. A column without a width gets only the space the
others leave, which is none once their widths reach the table's, so its cells collapse to
zero width and a user cannot reach them (a write-off's qty out was unreachable this way)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _line_columns() -> set[str]:
    src = (ROOT / "ui/routes/documents.py").read_text()
    cols: set[str] = set()
    for line in src.splitlines():
        if "_thead_base" in line or "_line_headers" in line:
            cols.update(re.findall(r"\b(col-[a-z-]+)", line))
    return cols


def test_every_line_column_has_a_width():
    css = (ROOT / "ui/static/app.css").read_text()
    cols = _line_columns()
    assert "col-qtyout" in cols and "col-comment" in cols
    missing = [c for c in sorted(cols) if not re.search(
        r"\.doc-lines(?:--[a-z-]+)? td\." + re.escape(c) + r"\b[^{]*\{[^}]*\bwidth:", css)]
    assert not missing, f"line columns without a width: {missing}"
