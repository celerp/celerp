"""One document state reads as one label, and every badge is drawn as a pill."""
import re
from pathlib import Path

from fasthtml.common import to_xml

from ui.components.table import doc_status_badge


def test_unpaid_issued_invoice_reads_as_awaiting_payment_like_a_bill():
    invoice = to_xml(doc_status_badge("invoice", "final"))
    bill = to_xml(doc_status_badge("bill", "awaiting_payment"))
    assert invoice == bill
    assert "Awaiting Payment" in invoice and "badge--awaiting-payment" in invoice


def test_final_keeps_its_meaning_on_other_documents():
    assert "badge--final" in to_xml(doc_status_badge("memo", "final"))
    assert "badge--partial" in to_xml(doc_status_badge("invoice", "partial"))


def test_base_badge_style_is_a_pill_so_no_value_renders_as_bare_text():
    css = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.css").read_text()
    base = re.search(r"^\.badge \{([^}]*)\}", css, re.M).group(1)
    assert "background" in base and "border" in base
