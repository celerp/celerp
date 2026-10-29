# A supplier credit note is called that everywhere a user meets it: the document list,
# the list's count chip, a contact's documents tab, and the email that sends it. The
# stored doc_type stays debit_note; only its label is shown.
from __future__ import annotations

import asyncio

from fasthtml.common import to_xml

from test_receipt_accounting import _doc, _finalize
from test_share import _capture_send

SCN = {"entity_id": "doc:SCN-1", "doc_number": "SCN-1", "doc_type": "debit_note",
       "status": "final", "total_amount": 50.0, "issue_date": "2026-10-12"}


def _shows_supplier_credit_note(html: str) -> None:
    assert "Supplier Credit Note" in html
    assert "Debit Note" not in html


def test_document_list_names_the_type():
    from ui.routes.documents import _doc_table
    _shows_supplier_credit_note(to_xml(_doc_table([SCN], doc_type="debit_note")))


def test_count_summary_names_the_type():
    from ui.routes.documents import _doc_summary_line
    _shows_supplier_credit_note(to_xml(_doc_summary_line({"debit_note_count": 1}, "debit_note")))


def test_list_cards_count_issued_notes():
    """An issued note is counted under All issued, like a customer credit note; the invoice
    cards (awaiting payment, overdue, paid) and the restock card do not apply to it."""
    from ui.i18n import t
    from ui.routes.documents import _doc_status_cards
    summary = {"count_by_status": {"final": 1, "void": 1}, "all_issued_count": 1}
    html = to_xml(_doc_status_cards([SCN], "", summary, doc_type="debit_note"))
    assert t("status.all_issued") in html and "all_issued=1" in html
    for absent in ("status.awaiting_payment", "status.overdue", "label.paid", "documents.not_restocked"):
        assert t(absent) not in html, absent


def test_contact_documents_tab_names_the_type():
    from ui.routes.contacts import _documents_tab
    _shows_supplier_credit_note(to_xml(_documents_tab([SCN], show_files=False)))


async def test_emailed_note_is_named_in_the_subject(client, auth, monkeypatch):
    captured, done = await _capture_send(monkeypatch)
    doc = await _doc(client, auth, "debit_note", [{"name": "Handling", "quantity": 1, "unit_price": 50.0}])
    await _finalize(client, auth, doc)
    r = await client.post(f"/docs/{doc}/send", headers=auth["headers"], json={"sent_to": "ap@supplier.example"})
    assert r.status_code == 200, r.text
    await asyncio.wait_for(done.wait(), timeout=2)
    _shows_supplier_credit_note(captured["subject"])
    _shows_supplier_credit_note(captured["html"])
