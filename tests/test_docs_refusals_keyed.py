"""Every refusal the document module raises says what to do next in the user's language.

Initial conditions per test: a fresh company with the seeded chart (API tests), or the
source tree and the 12 ui/locales catalogs (static test). Each refusal below carries a
message key with its params, and the key is in every catalog with the same params, so
the UI renders it translated (ui.i18n.refusal_text) instead of the English sentence:
a credit note in another currency or rate, an invoice voided or reverted under an issued
credit note, received goods no line prices, and a receipt whose goods moved on since.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from pathlib import Path

import pytest

from test_cost_restatement import _state
from test_credit_note_settlement_owned import _cn, _cn_body, _post, _svc_invoice
from test_undo_receipt_moved_on import _received_parcel, _split_one

_ROOT = Path(__file__).resolve().parents[1]
_LANGS = ("am", "ar", "de", "en", "es", "fr", "id", "it", "ja", "pt", "th", "vi")
_CATALOGS = {lang: json.loads((_ROOT / "ui" / "locales" / f"{lang}.json").read_text()) for lang in _LANGS}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Keys built at the call site (f-strings), listed with what they expand to.
_BUILT = {"docs.void_under_credit_note", "docs.revert_under_credit_note"}


def _keys_raised(paths) -> set[str]:
    """Every literal first argument of refusal(...) under ``paths``."""
    keys: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "refusal"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
    return keys


def _module_sources():
    docs = _ROOT / "default_modules" / "celerp-docs" / "celerp_docs"
    return [*docs.glob("*.py"), _ROOT / "celerp" / "services" / "lot_origin.py"]


def test_every_document_refusal_key_is_in_all_twelve_catalogs_with_its_params():
    keys = _keys_raised(_module_sources()) | _BUILT
    missing = {lang: sorted(k for k in keys if k not in cat) for lang, cat in _CATALOGS.items()}
    assert not any(missing.values()), missing
    for key in keys:
        want = set(_PLACEHOLDER.findall(_CATALOGS["en"][key]))
        for lang in _LANGS:
            assert set(_PLACEHOLDER.findall(_CATALOGS[lang][key])) == want, (key, lang)


def _keyed(r, key: str) -> dict:
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == key, r.text
    return detail


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, key", [({"currency": "USD"}, "credit_note.currency_differs_rate"),
                                        ({"conversion_rate": 1.3}, "credit_note.currency_differs_rate")])
async def test_a_credit_note_in_another_currency_is_refused_with_a_key(client, session, auth, extra, key):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    r = await client.post("/docs", headers=auth["headers"], json=_cn_body(inv, 50.0, **extra))
    assert r.status_code == 422, r.text
    assert _keyed(r, key)["params"]["currency"] == "EUR"


@pytest.mark.asyncio
@pytest.mark.parametrize("action, key", [("void", "docs.void_under_credit_note"),
                                         ("revert-to-draft", "docs.revert_under_credit_note")])
async def test_undoing_an_invoice_under_a_credit_note_is_refused_with_a_key(client, session, auth, action, key):
    inv = await _svc_invoice(client, auth, 80.0)
    cn = await _cn(client, auth, inv, 40.0)
    r = await _post(client, auth, f"/docs/{inv}/{action}")
    assert r.status_code == 409, r.text
    s = await _state(session, auth, cn)
    number = s.get("doc_number") or s.get("ref_id")
    assert _keyed(r, key)["params"] == {"numbers": number}


@pytest.mark.asyncio
async def test_undoing_a_receipt_whose_goods_moved_on_names_each_reason_with_a_key(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    await _split_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    detail = _keyed(r, "docs.undo_receipt_moved_on")
    [reason] = detail["params"]["reasons"]
    assert reason["message_key"] == "docs.undo_lot_holds"
    assert reason["params"] == {"sku": "UNDO-G", "held": "3", "came_in": "4"}


@pytest.mark.asyncio
async def test_the_ui_reads_a_moved_on_receipt_in_the_users_language(client, session, auth):
    from ui.i18n import refusal_text, set_lang

    bill, parcel = await _received_parcel(client, session, auth)
    await _split_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    set_lang("de")
    try:
        text = refusal_text(r.json()["detail"])
    finally:
        set_lang("en")
    assert "Wareneingang" in text and "UNDO-G" in text and "holds" not in text, text


# Neighbouring rules: a credit note in its invoice's currency and rate is still created, and
# a receipt whose goods are as they came in is still undone.


@pytest.mark.asyncio
async def test_a_credit_note_in_its_invoices_currency_is_still_created(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    r = await client.post("/docs", headers=auth["headers"], json=_cn_body(inv, 50.0))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_receipt_as_it_came_in_is_still_undone(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
