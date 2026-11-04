"""A list or report that opens on a date window and finds nothing there names the window
and offers All time, so books imported from earlier years never read as empty."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.routes.documents import _doc_table, _list_table
from ui.routes.financial_reports import _general_ledger_view, _trial_balance_table
from ui.components.journal import journal_table


def _assert_note(html: str, href: str) -> None:
    assert "2025-11-02" in html and "2026-11-02" in html
    assert f'href="{href}"' in html
    assert "All time" in html


def test_empty_document_list_names_its_window():
    html = to_xml(_doc_table([], doc_type="invoice", lang="en",
                             period=("2025-11-02", "2026-11-02", "/docs?preset=all&type=invoice")))
    assert "No documents yet." not in html
    _assert_note(html, "/docs?preset=all&amp;type=invoice")


def test_empty_list_page_names_its_window():
    html = to_xml(_list_table([], lang="en", period=("2025-11-02", "2026-11-02", "/lists?preset=all")))
    _assert_note(html, "/lists?preset=all")


def test_empty_reports_name_their_window():
    period = ("2025-11-02", "2026-11-02", "/reports/trial-balance?preset=all")
    _assert_note(to_xml(_trial_balance_table({"lines": []}, period=period)), period[2])
    _assert_note(to_xml(_general_ledger_view({"rows": []}, period=period)), period[2])
    _assert_note(to_xml(journal_table([], items=False, period=period)), period[2])


def test_unwindowed_empty_list_keeps_its_first_run_message():
    html = to_xml(_doc_table([], doc_type="invoice", lang="en"))
    assert "No documents yet." in html and "All time" not in html


def test_empty_pnl_names_its_window():
    from ui.routes.financial_reports import _pnl_view
    period = ("2025-11-02", "2026-11-02", "/reports/pnl?preset=all")
    _assert_note(to_xml(_pnl_view({}, period=period)), period[2])
    busy = {"revenue": {"lines": [{"account_code": "4000", "name": "Sales", "amount": 5}], "total": 5}}
    assert "All time" not in to_xml(_pnl_view(busy, period=period))


def test_filtered_empty_journal_says_no_matches():
    html = to_xml(journal_table([], items=False, filtered=True, period=("2025-11-02", "2026-11-02", "/accounting?preset=all")))
    assert "All time" not in html
