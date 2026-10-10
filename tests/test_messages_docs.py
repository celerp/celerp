# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Document and invoicing messages say what went wrong and what to do, in the user's language."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from ui.i18n import t

_ROOT = Path(__file__).resolve().parents[1]
_LOCALES = _ROOT / "ui" / "locales"
_LANGS = ("en", "th", "de", "fr", "es", "it", "pt", "id", "vi", "ja", "ar", "am")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _catalog(lang: str) -> dict:
    return json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


async def _headers(client, **extra) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "DocMsgCo", "email": f"doc-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}", **extra}


async def _invoice(client, h, *, finalize: bool = False) -> str:
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0}],
        "subtotal": 100.0, "total": 100.0, "currency": "USD"})
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    if finalize:
        assert (await client.post(f"/docs/{eid}/finalize", headers=h)).status_code == 200
    return eid


@pytest.mark.asyncio
async def test_missing_document_says_where_to_go(client):
    h = await _headers(client)
    r = await client.get("/docs/doc:nope", headers=h)
    assert r.status_code == 404
    assert r.json()["detail"] == t("documents.document_not_found", "en")
    assert r.json()["detail"].startswith("Celerp couldn't find this document.")


@pytest.mark.asyncio
async def test_lifecycle_refusals_say_what_to_do(client):
    h = await _headers(client)
    eid = await _invoice(client, h, finalize=True)
    r = await client.delete(f"/docs/{eid}", headers=h)
    assert r.status_code == 409
    assert r.json()["detail"] == t("documents.err_delete_not_draft", "en")
    r = await client.post(f"/docs/{eid}/unvoid", headers=h, json={})
    assert r.status_code == 409
    assert r.json()["detail"]["message_key"] == "docs.unvoid_not_void"


@pytest.mark.asyncio
async def test_empty_note_says_type_one(client):
    h = await _headers(client)
    eid = await _invoice(client, h)
    r = await client.post(f"/docs/{eid}/notes", headers=h, json={"note": "   "})
    assert r.status_code == 422
    assert r.json()["detail"] == t("documents.err_note_empty", "en")


@pytest.mark.asyncio
async def test_document_messages_follow_the_users_language(client):
    h = await _headers(client, **{"Accept-Language": "th"})
    r = await client.get("/docs/doc:nope", headers=h)
    assert r.json()["detail"] == t("documents.document_not_found", "th")
    assert t("documents.document_not_found", "th") != t("documents.document_not_found", "en")


def test_shipment_values_name_the_bad_value():
    from celerp_docs.routes import _validate_shipment_values

    with pytest.raises(ValueError, match=re.escape(t("documents.err_incoterms_invalid", "en", value="XYZ"))):
        _validate_shipment_values({"incoterms": "XYZ"})
    with pytest.raises(ValueError, match=re.escape(t("documents.err_export_reason_invalid", "en", value="nap"))):
        _validate_shipment_values({"reason_for_export": "nap"})


def test_damaged_tax_says_where_to_fix_it():
    from celerp_docs.doc_money import _recompute_tax_applications

    with pytest.raises(HTTPException) as exc:
        _recompute_tax_applications(["bad"], 100, "USD")
    assert exc.value.detail == t("documents.err_tax_invalid", "en")


def test_online_payment_pause_is_translated():
    from celerp.services import payments

    assert not hasattr(payments, "PAUSED")
    assert "{" not in t("documents.err_pay_paused", "de")


_REWRITTEN = [
    "doc.could_not_load_selected_documents", "doc.csv_decode_error", "doc.csv_missing_sku_or_description",
    "doc.share_failed", "documents.allow_split_warn",
    "documents.could_not_set_available", "documents.could_not_set_reserved", "documents.docs_skipped",
    "documents.document_not_found", "documents.duplicate_item_on_document",
    "documents.import_failed", "documents.invalid_returned_quantity", "documents.line_item_not_found",
    "documents.list_not_found", "documents.lookup_error", "documents.reprice_failed",
    "documents.reprice_missing_item_tip", "documents.reprice_partial_many", "documents.reprice_partial_one",
    "documents.save_failed", "documents.scan_error", "documents.scan_not_found",
    "ai.memory_clear_failed", "label.loading_templates", "error.cannot_divide_by_zero_qty",
]


def _spec_keys() -> list[str]:
    return sorted(k for k in _catalog("en") if k.startswith("documents.err_"))


@pytest.mark.parametrize("key", [*_REWRITTEN, "documents.err_draft_edited", "documents.err_pay_paused"])
def test_rewritten_document_messages_exist_in_every_language(key):
    en = _catalog("en")[key]
    for lang in _LANGS:
        value = _catalog(lang).get(key)
        assert value, (lang, key)
        assert set(_PLACEHOLDER.findall(value)) == set(_PLACEHOLDER.findall(en)), (lang, key)
        assert "—" not in value, (lang, key)


def test_every_new_document_error_key_is_in_every_language():
    keys = _spec_keys()
    assert len(keys) >= 200
    for lang in _LANGS:
        cat = _catalog(lang)
        assert [k for k in keys if not cat.get(k)] == [], lang


def test_rewritten_messages_say_what_to_do():
    assert t("documents.err_delete_not_draft", "en").startswith("Only a draft can be deleted.")
    assert t("documents.scan_not_found", "en", code="X1") == "Celerp couldn't find X1. Check the code and try again."
    assert t("label.loading_templates", "en") == "Loading templates..."


_LITERAL_KEY = re.compile(r"""\bt\(\s*["']([a-z_]\w*\.[\w.]+)["']\s*[,)]""")


def test_every_literal_translation_key_exists_in_a_catalog():
    known = set(_catalog("en"))
    for module_en in (_ROOT / "default_modules").glob("*/*/locales/en.json"):
        known |= set(json.loads(module_en.read_text(encoding="utf-8")))
    missing = []
    for root in ("ui", "celerp", "default_modules"):
        for path in (_ROOT / root).rglob("*.py"):
            if "tests" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                missing += [f"{path.relative_to(_ROOT)}:{n} {k}" for k in _LITERAL_KEY.findall(line) if k not in known]
    assert missing == []


def test_unit_price_error_is_not_passed_a_language():
    source = (_ROOT / "ui" / "routes" / "inventory.py").read_text(encoding="utf-8")
    assert 't("error.cannot_divide_by_zero_qty")' in source


@pytest.mark.parametrize("key", [
    "documents.error_detail", "documents.session_expired", "documents.not_found_prefix",
    "doc._all_selected_documents_must_be_from_the_same_cont",
])
def test_replaced_document_messages_are_gone(key):
    for lang in _LANGS:
        assert key not in _catalog(lang), (lang, key)


@pytest.mark.parametrize("lang", _LANGS)
def test_patch_refusal_names_the_buttons_as_the_user_sees_them(lang):
    text = t("documents.err_fields_not_patchable", lang, fields="x")
    for label in ("btn.finalize", "btn.void", "doc.revert_to_draft"):
        assert t(label, lang) in text, (lang, label)


@pytest.mark.parametrize("lang", _LANGS)
def test_deposit_refusal_names_the_option_as_the_user_sees_it(lang):
    text = t("documents.err_deposit_account_refused", lang, code="4000", default="1000")
    assert t("connectors.deposit_default", lang) in text


@pytest.mark.parametrize("lang", [lang for lang in _LANGS if lang != "en"])
def test_payment_void_advice_has_no_english_left_in(lang):
    for key in ("documents.err_refund_credit_application", "documents.err_payment_partly_refunded"):
        assert "Void" not in t(key, lang), (lang, key)
