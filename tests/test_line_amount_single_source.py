"""A line's amount is worked out in one place, celerp.services.money.document_line_amount,
so the printed page, the books and the reports charge a line what the document stores."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from celerp.output.doc_print import render_doc_print_html
from celerp.services.auto_je import bill_line_charges

_ROOT = Path(__file__).resolve().parents[1]

# Lines whose stored amount needs rounding, a stored half-cent that float formatting
# rounds down, and a discounted line with no stored amount.
_LINES = [
    {"sku": "A", "description": "Half off", "quantity": 1, "unit_price": 1.15, "discount_pct": 50.0,
     "line_total": 0.575},
    {"sku": "B", "description": "Half cent", "quantity": 1, "unit_price": 2.675, "line_total": 2.675},
    {"sku": "C", "description": "Unstored", "quantity": 2, "unit_price": 1.15, "discount_pct": 50.0},
]


def _cells(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)]


@pytest.mark.parametrize("doc", [
    {"doc_type": "invoice", "ref_id": "INV-1"},
    {"doc_type": "list", "list_type": "quotation", "ref_id": "QUO-1"},
    {"doc_type": "purchase_order", "ref_id": "PO-1"},
])
def test_print_line_amounts_match_the_stored_rounding(doc):
    cells = _cells(render_doc_print_html({**doc, "currency": "USD", "line_items": _LINES}))
    assert "$0.58" in cells and "$2.68" in cells and "$1.15" in cells
    assert "$0.57" not in cells and "$2.67" not in cells
    assert "50%" in cells and "50.0%" not in cells


def test_commercial_invoice_values_and_declared_total_match_the_stored_rounding():
    html = render_doc_print_html({"doc_type": "list", "list_type": "shipping_doc", "ref_id": "SH-1",
                                  "currency": "USD", "line_items": _LINES}, layout="commercial_invoice")
    cells = _cells(html)
    assert "$0.58" in cells and "$2.68" in cells and "$1.15" in cells
    assert "$4.41" in html, "declared value is the sum of the printed line values"


def test_bill_line_charges_keep_a_free_line_free():
    bill = {"currency": "USD", "total": 100, "tax": 0, "line_items": [
        {"quantity": 1, "unit_price": 10, "discount_pct": 100, "line_total": 0},
        {"quantity": 1, "unit_price": 100, "line_total": 100},
    ]}
    (free_gross, free_disc, _), (paid_gross, paid_disc, _) = bill_line_charges(bill)
    assert (free_gross, free_disc) == (0, 0)
    assert (paid_gross, paid_disc) == (100, 0), "a free line's list price is not a discount on the others"


# Every read of a stored line amount outside money.py, with why it is not an amount
# being worked out. A new read fails here: call money.document_line_amount instead.
_ALLOWED_READS = {
    "ui/routes/docs_import.py": (1, "shows the amount the received file states, as received"),
}
_READ = re.compile(r"""(\.get\(\s*|\[)["']line_total["'](?!\]\s*=[^=])""")
_SCANNED = ("celerp", "ui", "default_modules")


def test_no_module_works_out_a_line_amount_itself():
    found: dict[str, int] = {}
    for top in _SCANNED:
        for path in (_ROOT / top).rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            if "/tests/" in rel or rel == "celerp/services/money.py":
                continue
            n = len(_READ.findall(path.read_text(encoding="utf-8")))
            if n:
                found[rel] = n
    assert found == {k: n for k, (n, _why) in _ALLOWED_READS.items()}
