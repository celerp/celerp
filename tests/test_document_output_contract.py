# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Customer-facing output semantics without normal-path layout additions."""
from __future__ import annotations

import io

import pypdf

from celerp.output.document_context import prepare_document_output
from celerp.output.doc_print import render_doc_print_html
from celerp.output.pdf import generate_document_pdf


def _pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(io.BytesIO(data)).pages)


def test_output_identity_prefers_business_name_and_preserves_stored_fields():
    out = prepare_document_output(
        {"company_name": "Historic Seller Ltd", "company_address": "Document-specific branch"},
        company={"name": "Fallback Company"},
        self_contact={
            "name": "Owner Person",
            "company_name": "Seller Business Ltd",
            "billing_address": "Head office",
            "phone": "+66 2 555",
        },
    )
    assert out["company_name"] == "Historic Seller Ltd"
    assert out["company_address"] == "Document-specific branch"
    assert out["company_phone"] == "+66 2 555"


def test_output_context_derives_primary_address_attention():
    out = prepare_document_output({}, contact={
        "name": "Jane Buyer",
        "company_name": "Buyer Co",
        "addresses": [
            {
                "address_type": "billing", "is_default": True,
                "line1": "123 Main Street", "city": "Bangkok",
                "attn": "Accounts Payable",
            },
            {
                "address_type": "shipping", "is_default": True,
                "line1": "Warehouse 9", "attn": "Receiving",
            },
        ],
    })
    assert out["contact_billing_attn"] == "Accounts Payable"
    assert out["shipping_attn"] == "Receiving"
    assert "123 Main Street" in out["contact_billing_address"]
    assert "Warehouse 9" in out["contact_shipping_address"]


def test_output_identity_has_legacy_person_only_fallback():
    out = prepare_document_output({}, company={}, self_contact={"name": "Sole Trader"})
    assert out["company_name"] == "Sole Trader"


def test_html_is_organization_first_and_never_prints_internal_notes():
    html = render_doc_print_html({
        "doc_type": "invoice",
        "ref_id": "INV-1",
        "reference": "PO-55",
        "company_name": "Seller Co",
        "contact_company_name": "Buyer Co",
        "contact_name": "Jane Buyer",
        "terms_text": "PUBLIC TERMS",
        "customer_note": "PUBLIC CUSTOMER NOTE",
        "notes": "SECRET INTERNAL NOTE",
        "line_items": [],
    })
    assert "Seller Co" in html
    assert "Buyer Co" in html and "Jane Buyer" in html
    assert html.index("Buyer Co") < html.index("Jane Buyer")
    assert "Reference:" in html and "PO-55" in html
    assert "PUBLIC TERMS" in html
    assert "PUBLIC CUSTOMER NOTE" in html
    assert "SECRET INTERNAL NOTE" not in html


def test_html_explicit_billing_attention_replaces_secondary_person():
    html = render_doc_print_html({
        "doc_type": "invoice",
        "ref_id": "INV-ATTN",
        "contact_company_name": "Buyer Co",
        "contact_name": "Jane Buyer",
        "contact_billing_attn": "Accounts Payable",
        "contact_billing_address": "123 Main Street",
        "line_items": [],
    })
    assert "Buyer Co" in html
    assert "Attn: Accounts Payable" in html
    assert "Jane Buyer" not in html


def test_html_company_only_customer_and_quotation_expiry():
    html = render_doc_print_html({
        "doc_type": "list",
        "list_type": "quotation",
        "ref_id": "Q-1",
        "contact_company_name": "Company Only Ltd",
        "valid_until": "2026-10-31",
        "due_date": "2026-10-01",
        "line_items": [],
    })
    assert "Bill To" in html
    assert "Company Only Ltd" in html
    assert "Valid until:" in html
    assert "2026-10-31" in html
    assert "Due:" not in html


def test_blank_optional_output_adds_no_new_sections():
    html = render_doc_print_html({
        "doc_type": "invoice",
        "ref_id": "INV-PLAIN",
        "contact_name": "Plain Customer",
        "line_items": [],
    })
    assert "Reference:" not in html
    assert "Valid until:" not in html
    assert "Terms & Conditions" not in html
    assert "Note to Customer" not in html


def test_legacy_customer_terms_remain_public_but_internal_notes_do_not():
    html = render_doc_print_html({
        "doc_type": "invoice",
        "ref_id": "INV-LEGACY",
        "terms": "LEGACY PUBLIC TERMS",
        "notes": "SECRET LEGACY INTERNAL NOTE",
        "line_items": [],
    })
    assert "LEGACY PUBLIC TERMS" in html
    assert "SECRET LEGACY INTERNAL NOTE" not in html


def test_pdf_prints_customer_fields_but_not_internal_notes():
    data = generate_document_pdf({
        "doc_type": "invoice",
        "ref_id": "INV-2",
        "reference": "PO-77",
        "company_name": "Seller Business Ltd",
        "contact_company_name": "Buyer Org",
        "contact_name": "Jane Buyer",
        "contact_billing_attn": "Accounts Payable",
        "terms_text": "PUBLIC PDF TERMS",
        "customer_note": "PUBLIC PDF CUSTOMER NOTE",
        "notes": "SECRET PDF INTERNAL NOTE",
        "line_items": [],
    })
    text = _pdf_text(data)
    assert "Seller Business Ltd" in text
    assert "Buyer Org" in text
    assert "Accounts Payable" in text
    assert "Jane Buyer" not in text
    assert "PO-77" in text
    assert "PUBLIC PDF TERMS" in text
    assert "PUBLIC PDF CUSTOMER NOTE" in text
    assert "SECRET PDF INTERNAL NOTE" not in text


def test_explicit_blank_output_fields_override_live_fallback():
    """Stored blanks are deliberate overrides, not invitations to resurrect live/legacy data."""
    from celerp.output.document_context import prepare_document_output

    out = prepare_document_output(
        {
            "company_name": "",
            "contact_name": "",
            "terms_text": "",
            "terms": "Legacy terms that must stay suppressed.",
        },
        company={"name": "Live Seller"},
        contact={"name": "Live Buyer"},
    )
    assert out["company_name"] == ""
    assert out["contact_name"] == ""
    assert out["terms_text"] == ""


def test_route_letterhead_fallback_does_not_overwrite_explicit_blanks():
    """Every UI output door shares one key-presence merge invariant."""
    from pathlib import Path

    ui_source = Path("ui/routes/documents.py").read_text()
    share_source = Path("default_modules/celerp-docs/celerp_docs/routes_share.py").read_text()
    helper_start = ui_source.index("async def _merge_company_letterhead")
    helper = ui_source[helper_start:helper_start + 900]
    assert "if key not in state and value:" in helper
    assert ui_source.count("await _merge_company_letterhead(token,") == 4
    assert "for _key, _value in (await _company_letterhead(token)).items():" not in ui_source
    assert "if not state.get(key) and value:" not in share_source
    assert "if not doc.get(key) and value:" not in share_source


def test_document_detail_reuses_output_contact_fallback():
    """The editable detail page must not reconstruct customer snapshots separately."""
    from pathlib import Path

    source = Path("ui/routes/documents.py").read_text()
    start = source.index('async def doc_detail(request: Request, entity_id: str):')
    end = source.index("# Fetch locations for receive-goods dropdown", start)
    snippet = source[start:end]
    assert "prepare_document_output(doc, contact=_resolved_contact)" in snippet
    assert 'doc["contact_name"] = _resolved_contact.get("name")' not in snippet
    assert 'doc["contact_email"] = _c.get("email")' not in snippet
    assert 'if "contact_billing_address" not in doc' not in snippet

    subscription_source = Path("ui/routes/subscriptions.py").read_text()
    assert "_merge_company_letterhead(token, doc)" in subscription_source
    assert "prepare_document_output(doc, contact=await api.get_contact(token, cid))" in subscription_source
    assert 'if not doc.get("company_name")' not in subscription_source

def test_legacy_billing_alias_respects_canonical_key_presence():
    absent = prepare_document_output({"contact_address": "Legacy billing"})
    assert absent["contact_billing_address"] == "Legacy billing"

    blank = prepare_document_output({
        "contact_billing_address": "",
        "contact_address": "Legacy billing that must stay suppressed",
    })
    assert blank["contact_billing_address"] == ""


def test_renderers_do_not_resurrect_explicit_blank_identity_fields():
    doc = {
        "doc_type": "invoice",
        "ref_id": "INV-BLANK",
        "company_name": "",
        "company_address": "",
        "company_phone": "",
        "company_tax_id": "",
        "company_email": "",
        "contact_name": "",
        "contact_id": "contact:must-not-render",
        "contact_billing_address": "",
        "contact_address": "Legacy billing that must not render",
        "line_items": [],
    }
    company = {
        "name": "Live Seller That Must Not Render",
        "address": "Live Address That Must Not Render",
        "phone": "+66 999",
        "tax_id": "LIVE-TAX",
        "email": "live@example.test",
    }

    pdf_text = _pdf_text(generate_document_pdf(doc, company=company))
    html = render_doc_print_html(doc)
    for forbidden in (
        "Live Seller That Must Not Render", "Live Address That Must Not Render",
        "+66 999", "LIVE-TAX", "live@example.test",
        "contact:must-not-render", "Legacy billing that must not render",
    ):
        assert forbidden not in pdf_text
        assert forbidden not in html


def test_company_website_uses_canonical_output_contract():
    live = prepare_document_output({}, self_contact={"website": "https://live.example"})
    assert live["company_website"] == "https://live.example"

    stored = prepare_document_output(
        {"company_website": "https://historic.example"},
        self_contact={"website": "https://live.example"},
    )
    assert stored["company_website"] == "https://historic.example"

    blank = prepare_document_output(
        {"company_website": ""},
        self_contact={"website": "https://must-not-return.example"},
    )
    assert blank["company_website"] == ""

    doc = {
        "doc_type": "invoice",
        "ref_id": "INV-WEB",
        "company_name": "Seller Co",
        "company_website": "https://seller.example/?a=1&b=2",
        "line_items": [],
    }
    assert "https://seller.example/?a=1&b=2" in render_doc_print_html(doc)
    assert "https://seller.example/?a=1&b=2" in _pdf_text(generate_document_pdf(doc))
